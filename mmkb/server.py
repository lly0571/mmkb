"""Start the HTTP API and singleton Worker together: python -m mmkb.server."""
from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import socket
import subprocess
import sys
from threading import Event
import time

from mmkb.config import load_app_config, load_model_config

logger = logging.getLogger(__name__)


def stop_children(children, timeout: float, force: Event) -> bool:
    """Signal every child before waiting, with one shared shutdown deadline."""
    for _, child in children:
        if child.poll() is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + timeout
    graceful = True
    while any(child.poll() is None for _, child in children):
        if force.is_set() or time.monotonic() >= deadline:
            graceful = False
            for name, child in children:
                if child.poll() is None:
                    logger.error("Forcing %s to stop; recover interrupted processing explicitly", name)
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
            break
        force.wait(min(0.05, max(0, deadline - time.monotonic())))
    for _, child in children:
        child.wait()
    return graceful


def run_backend(listener: socket.socket, stop: Event, force: Event, *,
                poll_seconds: float = 2.0, shutdown_timeout: float = 300.0) -> int:
    """Own two independent children; any unexpected exit stops the whole bundle."""
    env = dict(os.environ)
    # A user's Uvicorn environment must not silently create reload/multiprocess supervisors.
    for key in ("UVICORN_RELOAD", "UVICORN_UDS"):
        env.pop(key, None)
    commands = [
        ("API", [sys.executable, "-m", "uvicorn", "mmkb.api:create_app", "--factory",
                 "--fd", str(listener.fileno()), "--workers", "1",
                 "--timeout-graceful-shutdown", str(math.ceil(shutdown_timeout))]),
        ("Worker", [sys.executable, "-m", "mmkb.worker", "run", "--poll-seconds", str(poll_seconds)]),
    ]
    children = []
    failed = False
    try:
        for name, command in commands:
            if stop.is_set():
                break
            kwargs = {"env": env, "start_new_session": True}
            if name == "API":
                kwargs["pass_fds"] = (listener.fileno(),)
            child = subprocess.Popen(command, **kwargs)
            children.append((name, child))
            # The API now owns its listening descriptor. A stopped API must not leave the
            # supervisor accepting connections while the Worker finishes its current item.
            if name == "API":
                listener.close()
            logger.info("Launched %s pid=%s", name, child.pid)
        while not stop.is_set():
            for name, child in children:
                code = child.poll()
                if code is not None:
                    logger.error("%s exited unexpectedly (code=%s); stopping backend", name, code)
                    failed = True
                    break
            if failed:
                break
            stop.wait(0.1)
    except OSError as exc:
        logger.error("Cannot launch backend child: %s", type(exc).__name__)
        failed = True
    finally:
        listener.close()
        if not stop_children(children, shutdown_timeout, force):
            failed = True
    return int(failed)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--shutdown-timeout", type=float, default=300.0,
                        help="shared API/Worker shutdown budget in seconds; second signal forces exit")
    args = parser.parse_args(argv)
    if os.name != "posix":
        parser.error("The bundled launcher requires POSIX; use the separate API and Worker commands")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be 0..65535")
    if any(not math.isfinite(v) or v <= 0 for v in (args.poll_seconds, args.shutdown_timeout)):
        parser.error("poll interval and shutdown timeout must be finite positive numbers")
    try:
        # Configuration only: never migrate, initialize storage, or recover old jobs implicitly.
        # dotenv loading here also gives both children the same environment snapshot.
        load_app_config()
        load_model_config()
    except ValueError:
        logger.error("backend_config: Check database, storage and model configuration")
        return 1
    try:
        family = socket.AF_INET6 if ":" in args.host else socket.AF_INET
        listener = socket.create_server((args.host, args.port), family=family)
    except OSError as exc:
        logger.error("backend_bind: Cannot bind API host/port (%s)", type(exc).__name__)
        return 1

    stop, force = Event(), Event()

    def request_stop(signum, frame):
        if stop.is_set():
            force.set()
        stop.set()

    handlers = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, request_stop)
        logger.info("Starting bundled backend on %s:%s", args.host, listener.getsockname()[1])
        return run_backend(listener, stop, force, poll_seconds=args.poll_seconds,
                           shutdown_timeout=args.shutdown_timeout)
    finally:
        listener.close()
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
