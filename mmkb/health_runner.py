"""Run one probe round in a disposable child process and honour the report deadline.

Synchronous code inside SQLAlchemy, Botocore and local file access cannot be interrupted from a
thread, so the only way to promise a bounded ``GET /api/health`` is to run the probes elsewhere and
kill that process. The child is started with a separate interpreter (never ``fork``), receives its
configuration on stdin, and streams one whitelisted result per line so completed components survive
a later timeout. Child stderr is drained and discarded: only byte counts are logged.
"""
from __future__ import annotations

import asyncio
import asyncio.subprocess
from collections.abc import Mapping
import json
import logging
import os
import sys
import time
from typing import Any

from mmkb.health import Check, HealthBudgets, ProbeRound, parse_check_payload
from mmkb.health_contract import DEPENDENCY_CHECKS

logger = logging.getLogger(__name__)
CHILD_MODULE = "mmkb.health_probes"
MAX_CAPTURED_STDERR_BYTES = 64 * 1024


class SubprocessProbeRunner:
    """Own at most one probe process; refuse a second round while a previous child is unconfirmed."""

    def __init__(self, *, argv: list[str] | None = None, env: Mapping[str, str] | None = None,
                 frame_limit_bytes: int | None = None, cleanup_seconds: float | None = None):
        defaults = HealthBudgets()
        self._argv = list(argv) if argv else [sys.executable, "-u", "-m", CHILD_MODULE]
        self._env = dict(os.environ if env is None else env)
        self._frame_limit = defaults.ipc_frame_limit_bytes if frame_limit_bytes is None else frame_limit_bytes
        self._cleanup_seconds = defaults.cleanup_seconds if cleanup_seconds is None else cleanup_seconds
        self._child: asyncio.subprocess.Process | None = None

    @property
    def child_pid(self) -> int | None:
        return None if self._child is None else self._child.pid

    async def run(self, request: Mapping[str, Any], *, timeout: float) -> ProbeRound:
        if self._child is not None and self._child.returncode is None:
            # One lingering probe is a fault by itself: never start a second process.
            return ProbeRound(checks={}, outcome="occupied", unreaped_pid=self._child.pid)
        deadline = time.monotonic() + timeout
        try:
            async with asyncio.timeout(timeout):
                child = await asyncio.create_subprocess_exec(
                    *self._argv,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env=self._env, limit=self._frame_limit,
                )
        except TimeoutError:
            return ProbeRound(checks={}, outcome="deadline")
        except OSError as error:
            logger.error("health_probe stage=start error_type=%s", type(error).__name__)
            return ProbeRound(checks={}, outcome="start_failed")
        self._child = child
        return await self._drive(child, request, deadline)

    async def _drive(self, child, request: Mapping[str, Any], deadline: float) -> ProbeRound:
        results: dict[str, Check] = {}
        outcome = "complete"
        stderr = asyncio.ensure_future(_drain_stderr(child.stderr))
        try:
            if not await self._send_config(child, request, deadline):
                outcome = "deadline" if time.monotonic() >= deadline else "protocol_error"
            outcome = await self._collect(child, results, deadline, outcome)
        except asyncio.CancelledError:
            await self._stop(child, deadline=time.monotonic() + self._cleanup_seconds)
            raise
        finally:
            stderr.cancel()
        # A child that answered the whole contract should exit by itself; a wedged one is killed.
        reaped = await self._stop(child, deadline=time.monotonic() + self._cleanup_seconds,
                                  graceful=outcome != "deadline")
        if not reaped:
            logger.error("health_probe stage=cleanup outcome=unreaped pid=%s", child.pid)
            return ProbeRound(checks=results, outcome=outcome, unreaped_pid=child.pid)
        if child.returncode not in (0, None) and outcome == "complete":
            # The child exited early or gave up on its own budget without completing the contract.
            outcome = "protocol_error"
        missing = set(DEPENDENCY_CHECKS) - set(results)
        if missing and outcome == "complete":
            outcome = "protocol_error"
        return ProbeRound(checks=results, outcome=outcome)

    async def _send_config(self, child, request: Mapping[str, Any], deadline: float) -> bool:
        payload = json.dumps({"v": 1, **dict(request)}, ensure_ascii=False).encode()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            async with asyncio.timeout(remaining):
                child.stdin.write(payload)
                await child.stdin.drain()
                child.stdin.close()
                await child.stdin.wait_closed()
        except (TimeoutError, OSError, ValueError) as error:
            logger.error("health_probe stage=config_write error_type=%s", type(error).__name__)
            return False
        return True

    async def _collect(self, child, results: dict[str, Check], deadline: float, outcome: str) -> str:
        stream = child.stdout
        while len(results) < len(DEPENDENCY_CHECKS) and outcome not in {"deadline", "protocol_error"}:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "deadline"
            try:
                async with asyncio.timeout(remaining):
                    line = await stream.readline()
            except (TimeoutError, asyncio.TimeoutError):
                return "deadline"
            except (ValueError, asyncio.LimitOverrunError):
                # A frame larger than the contract limit is a protocol breach, not a slow probe.
                logger.error("health_probe stage=ipc outcome=frame_too_large")
                return "protocol_error"
            if not line:
                return outcome
            try:
                frame = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                logger.error("health_probe stage=ipc outcome=frame_invalid")
                return "protocol_error"
            check = parse_check_payload(frame)
            if check is None:
                logger.error("health_probe stage=ipc outcome=frame_rejected")
                return "protocol_error"
            results[frame["check"]] = check
        return outcome

    async def _stop(self, child, *, deadline: float, graceful: bool = True) -> bool:
        """Let a finished child exit on its own, then terminate and force-kill; confirm it is gone."""
        if child.returncode is not None:
            return True
        steps: list = ([("exit", None)] if graceful else []) + [("terminate", child.terminate),
                                                                ("kill", child.kill)]
        for index, (signal_name, send) in enumerate(steps):
            if child.returncode is not None:
                return True
            if send is not None:
                try:
                    send()
                except ProcessLookupError:
                    return True
            try:
                # Reserve a share of the same cleanup budget for each escalation, including kill.
                remaining = max(0.0, deadline - time.monotonic())
                async with asyncio.timeout(remaining / (len(steps) - index)):
                    await child.wait()
                return True
            except (TimeoutError, asyncio.TimeoutError):
                logger.warning("health_probe stage=cleanup step=%s pid=%s", signal_name, child.pid)
            except ProcessLookupError:
                return True
        # A returncode set during the last wait confirms exit. Otherwise keep ownership so a later
        # reap can confirm it, without extending this cleanup budget.
        if child.returncode is not None:
            return True
        return False

    async def reap(self) -> bool:
        """Best-effort recovery of a child that outlived its cleanup budget."""
        child = self._child
        if child is None:
            return True
        if child.returncode is not None:
            self._child = None
            return True
        reaped = await self._stop(child, deadline=time.monotonic() + self._cleanup_seconds)
        if reaped:
            self._child = None
        return reaped

    async def aclose(self) -> None:
        await self.reap()


async def _drain_stderr(stream) -> int:
    """Read child stderr so the pipe never fills; the content is deliberately never retained."""
    if stream is None:
        return 0
    total = 0
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return total
            total = min(MAX_CAPTURED_STDERR_BYTES, total + len(chunk))
    except (OSError, ValueError, asyncio.CancelledError):
        return total
