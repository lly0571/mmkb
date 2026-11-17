"""Preview and upload a directory using durable, resumable per-file HTTP imports."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path, PurePosixPath
import signal
import tempfile
from threading import Event
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from mmkb.errors import ResourceError
from mmkb.images import inspect_image
from mmkb.resources import IMAGE_WORKFLOWS, normalize_tags

SCHEMA_VERSION = 1
DEFAULT_API = "http://127.0.0.1:8010"
FINISHED = {"created", "skipped"}
UNCERTAIN = {"uploading", "unknown"}


def api_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("API URL must be an http(s) origin without credentials, path, query or fragment")
    return value.rstrip("/")


def save_manifest(path: Path, manifest):
    """Only the coordinator writes; fsync before publishing a submission key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".mmkb-import-", delete=False) as output:
            temporary = Path(output.name)
            json.dump(manifest, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


@contextmanager
def manifest_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another process is using this manifest") from None
        yield


def load_manifest(path):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION or not isinstance(manifest.get("items"), list):
        raise ValueError("Unsupported import manifest")
    api_url(manifest["api_url"])
    if not manifest.get("root") or not Path(manifest["root"]).is_absolute():
        raise ValueError("CLI manifest must contain an absolute root directory")
    keys = set()
    for item in manifest["items"]:
        relative = PurePosixPath(item["relative_path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("Invalid relative path in manifest")
        UUID(item["key"])
        if item["key"] in keys:
            raise ValueError("Repeated submission key in manifest")
        keys.add(item["key"])
        if item["status"] not in FINISHED | UNCERTAIN | {"pending", "failed"}:
            raise ValueError("Invalid item status in manifest")
    return manifest


def _paths(root, recursive):
    def walk(directory):
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except OSError:
            if directory == root:
                raise
            yield directory, "read_failed"
            return
        for entry in children:
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    yield path, "symlink_skipped"
                elif entry.is_dir(follow_symlinks=False):
                    if recursive:
                        yield from walk(path)
                elif entry.is_file(follow_symlinks=False):
                    yield path, None
                else:
                    yield path, "special_file_skipped"
            except OSError:
                yield path, "read_failed"
    yield from sorted(walk(root), key=lambda value: value[0].relative_to(root).as_posix())


def inspect_path(root, relative, limit):
    path = root.joinpath(*PurePosixPath(relative).parts)
    cursor = root
    for part in PurePosixPath(relative).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ResourceError("source_changed", "Source became a symbolic link", 409)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Source path escapes root")
    with path.open("rb") as source:
        digest, _, metadata = inspect_image(source, path.name, max_upload_bytes=limit)
        stat = os.fstat(source.fileno())
    return dict(sha256=digest, size_bytes=metadata["size_bytes"],
                modified_ms=stat.st_mtime_ns // 1_000_000, mime_type=metadata["mime_type"])


def build_manifest(root, origin, limits, *, recursive=False, tags=None, title_mode="stem",
                   workflow="auto", duplicates="skip"):
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Source must be a directory")
    tags = normalize_tags(tags or [])
    items = []
    for path, problem in _paths(root, recursive):
        relative = path.relative_to(root).as_posix()
        title = path.stem if title_mode == "stem" else relative if title_mode == "relative-path" else ""
        item = dict(relative_path=relative, key=str(uuid4()), title=title, tags=tags,
                    workflow=None if workflow == "auto" else workflow, duplicate_policy=duplicates,
                    status="pending", sha256=None, size_bytes=0, modified_ms=0, mime_type=None,
                    error=None, resource_id=None, resource_status=None, attempts=[])
        try:
            if problem:
                raise ResourceError(problem, problem)
            item.update(inspect_path(root, relative, limits["max_upload_bytes"]))
            if len(title) > limits["max_text_chars"]:
                raise ResourceError("text_too_large", "Title exceeds server limit", 413)
        except (ResourceError, OSError) as error:
            code = error.code if isinstance(error, ResourceError) else "read_failed"
            item.update(status="skipped" if code in {"unsupported_format", "symlink_skipped", "special_file_skipped"}
                        else "failed", error=code)
        items.append(item)
    return dict(schema_version=SCHEMA_VERSION, api_url=api_url(origin), root=str(root),
                recursive=recursive, created_at=time.time(), import_options=limits,
                options=dict(tags=tags, title_mode=title_mode, workflow=workflow, duplicates=duplicates),
                items=items)


def _safe_error(response):
    try:
        payload = response.json()
        code = payload.get("code")
        return code if isinstance(code, str) else f"http_{response.status_code}"
    except (ValueError, AttributeError):
        return f"http_{response.status_code}"


def apply_receipt(item, receipt):
    if receipt["state"] == "completed":
        item.update(status=receipt["outcome"], resource_id=receipt["resource_id"],
                    resource_status=receipt["resource_status"], resource_error=receipt.get("resource_error"),
                    resource_workflow=receipt.get("resource_workflow"),
                    duplicate=receipt["duplicate"], outcome=receipt["outcome"], error=None)
    elif receipt["state"] == "failed":
        item.update(status="failed", error=receipt["error"] or "import_failed")


def resolve_items(client, manifest, items):
    for start in range(0, len(items), 100):
        batch = items[start:start + 100]
        response = client.post("/api/imports/resolve", json={"keys": [item["key"] for item in batch]})
        response.raise_for_status()
        receipts = response.json()["items"]
        by_key = {receipt["key"]: receipt for receipt in receipts}
        if any(item["key"] not in by_key for item in batch):
            raise ValueError("Incomplete import resolution")
        for item in batch:
            receipt = by_key[item["key"]]
            item["receipt_state"] = receipt["state"]
            apply_receipt(item, receipt)


def _upload(client, root, item, limit):
    try:
        fresh = inspect_path(root, item["relative_path"], limit)
        if fresh["sha256"] != item["sha256"]:
            return dict(status="failed", error="source_changed")
        path = root.joinpath(*PurePosixPath(item["relative_path"]).parts)
        # expected_sha256 also catches edits made after local preflight but during upload.
        fields = dict(title=item["title"], tags=item["tags"], duplicate_policy=item["duplicate_policy"],
                      expected_sha256=item["sha256"])
        if item["workflow"]:
            fields["workflow"] = item["workflow"]
        with path.open("rb") as source:
            response = client.post("/api/resources", data=fields,
                                   headers={"Idempotency-Key": item["key"]},
                                   files={"file": (path.name, source, item["mime_type"])})
        if response.status_code in {200, 201}:
            body = response.json()
            UUID(body["id"])
            if body["import_outcome"] not in FINISHED:
                raise ValueError("Invalid import outcome")
            return dict(status=body["import_outcome"], outcome=body["import_outcome"], resource_id=body["id"],
                        resource_status=body["status"], resource_workflow=body.get("workflow"),
                        duplicate=body["duplicate"], error=None)
        code = _safe_error(response)
        return dict(status="unknown" if response.status_code >= 500 or code == "import_busy" else "failed", error=code)
    except ResourceError as error:
        return dict(status="failed", error=error.code)
    except OSError:
        return dict(status="failed", error="read_failed")
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return dict(status="unknown", error="request_outcome_unknown")


def retry_preflight(manifest, item):
    """An explicit retry can repair bad input, but cannot reuse a key for new bytes."""
    fresh = inspect_path(Path(manifest["root"]), item["relative_path"],
                         manifest["import_options"]["max_upload_bytes"])
    if fresh["sha256"] != item["sha256"]:
        if item.get("receipt_state") == "reserved":
            raise ResourceError("previous_request_unresolved", "Resolve the earlier request before changing content", 409)
        item["attempts"].append(dict(key=item["key"], sha256=item["sha256"], error=item["error"]))
        item["key"] = str(uuid4())
    item.update(fresh, status="pending", error=None)


def run_manifest(client, path, manifest, *, concurrency=2, retry_failed=False, stop=None):
    stop = stop or Event()
    selected = [item for item in manifest["items"] if item["status"] in
                ({"failed"} if retry_failed else {"pending", "unknown", "uploading"})]
    # A missing receipt permits a retry with the SAME key; an in-flight first request can
    # still arrive later, so absence must never cause generation of a replacement key.
    try:
        resolve_items(client, manifest, selected)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return 3
    for item in selected:
        if item["status"] in FINISHED:
            continue
        if retry_failed:
            try:
                retry_preflight(manifest, item)
            except (ResourceError, OSError) as error:
                code = error.code if isinstance(error, ResourceError) else "read_failed"
                item.update(status="unknown" if code == "previous_request_unresolved" else "failed", error=code)
        elif item["status"] in UNCERTAIN:
            item["status"] = "pending"
    save_manifest(path, manifest)
    queue = [item for item in selected if item["status"] == "pending"]
    futures = {}
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        while queue or futures:
            active_hashes = {item["sha256"] for item in futures.values()}
            for item in list(queue):
                if stop.is_set() or len(futures) >= concurrency:
                    break
                if item["sha256"] in active_hashes:
                    continue
                item["status"] = "uploading"
                save_manifest(path, manifest)
                future = executor.submit(_upload, client, Path(manifest["root"]), dict(item),
                                         manifest["import_options"]["max_upload_bytes"])
                futures[future] = item
                active_hashes.add(item["sha256"])
                queue.remove(item)
            if not futures:
                break
            done, _ = wait(futures, timeout=0.25, return_when=FIRST_COMPLETED)
            for future in done:
                item = futures.pop(future)
                item.update(future.result())
                save_manifest(path, manifest)
    # Read back uncertain writes once; unresolved reservations remain explicit unknowns.
    uncertain = [item for item in selected if item["status"] in UNCERTAIN]
    try:
        resolve_items(client, manifest, uncertain)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        pass
    manifest["last_upload_seconds"] = round(time.monotonic() - started, 3)
    save_manifest(path, manifest)
    return 130 if stop.is_set() else exit_code(manifest)


def summary(manifest):
    states = Counter(item["status"] for item in manifest["items"])
    resources = {item["resource_id"]: item.get("resource_status") for item in manifest["items"] if item.get("resource_id")}
    return dict(discovered=len(manifest["items"]), valid=sum(bool(item.get("sha256")) for item in manifest["items"]),
                skipped=states["skipped"], imported=states["created"], failed=states["failed"],
                pending=states["pending"], unknown=states["unknown"] + states["uploading"],
                resources=dict(Counter(resources.values())), upload_seconds=manifest.get("last_upload_seconds"))


def exit_code(manifest):
    statuses = {item["status"] for item in manifest["items"]}
    return 3 if statuses & UNCERTAIN else 1 if "failed" in statuses else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="read-only preview; does not create resources")
    plan.add_argument("directory", type=Path)
    plan.add_argument("--manifest", type=Path, required=True)
    plan.add_argument("--api-url", default=os.environ.get("MMKB_API_URL", DEFAULT_API))
    plan.add_argument("--recursive", action="store_true")
    plan.add_argument("--tag", action="append", default=[])
    plan.add_argument("--title-mode", choices=["stem", "relative-path", "empty"], default="stem")
    plan.add_argument("--workflow", choices=["auto", *sorted(IMAGE_WORKFLOWS)], default="auto")
    plan.add_argument("--duplicates", choices=["skip", "create"], default="skip")
    for command in ("run", "resume", "retry-failed", "status"):
        child = commands.add_parser(command)
        child.add_argument("manifest", type=Path)
        child.add_argument("--api-url")
        if command == "status":
            child.add_argument("--watch", action="store_true")
            child.add_argument("--window", type=float, default=120)
        else:
            child.add_argument("--concurrency", type=int, choices=range(1, 9), default=2)
    args = parser.parse_args(argv)
    stop = Event()
    previous = signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        with manifest_lock(args.manifest):
            if args.command == "plan":
                if args.manifest.exists():
                    raise ValueError("Manifest already exists; choose a new path")
                origin = api_url(args.api_url)
                with httpx.Client(base_url=origin, timeout=30) as client:
                    response = client.get("/api/imports/options")
                    response.raise_for_status()
                    limits = response.json()
                manifest = build_manifest(args.directory, origin, limits, recursive=args.recursive,
                                          tags=args.tag, title_mode=args.title_mode,
                                          workflow=args.workflow, duplicates=args.duplicates)
                save_manifest(args.manifest, manifest)
                for item in manifest["items"]:
                    print(json.dumps({name: item[name] for name in ("relative_path", "status", "error", "size_bytes", "sha256")}, ensure_ascii=False))
                code = exit_code(manifest)
            else:
                manifest = load_manifest(args.manifest)
                if args.api_url and api_url(args.api_url) != manifest["api_url"]:
                    raise ValueError("Resume must use the original API origin")
                with httpx.Client(base_url=manifest["api_url"], timeout=httpx.Timeout(180, connect=5)) as client:
                    if args.command == "status":
                        if not math.isfinite(args.window) or args.window <= 0:
                            raise ValueError("--window must be positive")
                        deadline = time.monotonic() + args.window
                        while True:
                            resolve_items(client, manifest, [item for item in manifest["items"]
                                                            if item.get("sha256")])
                            save_manifest(args.manifest, manifest)
                            active = any(item.get("resource_status") in {"pending", "processing"}
                                         or item["status"] in UNCERTAIN for item in manifest["items"])
                            if stop.is_set() or not args.watch or not active:
                                code = 130 if stop.is_set() else exit_code(manifest)
                                break
                            if time.monotonic() >= deadline:
                                code = 3
                                break
                            print(json.dumps(summary(manifest), ensure_ascii=False), flush=True)
                            stop.wait(2)
                    else:
                        code = run_manifest(client, args.manifest, manifest, concurrency=args.concurrency,
                                            retry_failed=args.command == "retry-failed", stop=stop)
            print(json.dumps(summary(manifest), ensure_ascii=False))
            return code
    except KeyboardInterrupt:
        return 130
    except (ValueError, KeyError, TypeError, OSError, httpx.HTTPError) as error:
        # Do not print arbitrary upstream response bodies or credential-bearing exceptions.
        print(f"Importer error: {type(error).__name__}; check API, source directory and manifest", flush=True)
        return 2
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
