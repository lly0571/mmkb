"""Offline directory planning and lost-response recovery over the HTTP contract."""
import hashlib
from io import BytesIO
import json
from pathlib import Path
from threading import Event
from uuid import UUID

import httpx
from PIL import Image
import pytest

from mmkb.importer import (build_manifest, exit_code, load_manifest, manifest_lock,
                           run_manifest, save_manifest, summary)

LIMITS = dict(max_upload_bytes=1024 * 1024, max_text_chars=100, schema_version=1)


def png(path, color="blue"):
    output = BytesIO()
    Image.new("RGB", (3, 2), color).save(output, format="PNG")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(output.getvalue())
    return output.getvalue()


def test_directory_preview_uses_bytes_stable_order_and_explicit_recursion(tmp_path):
    png(tmp_path / "b.jpg")
    png(tmp_path / "nested/中文.png")
    (tmp_path / "a.txt").write_text("not an image")
    (tmp_path / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    before = (tmp_path / "b.jpg").read_bytes()
    manifest = build_manifest(tmp_path, "http://localhost", LIMITS)
    assert [item["relative_path"] for item in manifest["items"]] == ["a.txt", "b.jpg", "broken.png", "loop"]
    by_path = {item["relative_path"]: item for item in manifest["items"]}
    assert by_path["b.jpg"]["mime_type"] == "image/png"
    assert by_path["a.txt"]["status"] == "skipped" and by_path["loop"]["status"] == "skipped"
    recursive = build_manifest(tmp_path, "http://localhost", LIMITS, recursive=True, title_mode="relative-path")
    assert recursive["items"][-1]["title"] == "nested/中文.png"
    assert (tmp_path / "b.jpg").read_bytes() == before


def test_manifest_lock_and_atomic_round_trip(tmp_path):
    png(tmp_path / "image.png")
    manifest = build_manifest(tmp_path, "http://localhost", LIMITS)
    path = tmp_path / "state.json"
    save_manifest(path, manifest)
    assert load_manifest(path) == manifest
    with manifest_lock(path):
        with pytest.raises(ValueError, match="Another process"):
            with manifest_lock(path):
                pass
    manifest["items"][0]["relative_path"] = "../outside.png"
    save_manifest(path, manifest)
    with pytest.raises(ValueError, match="relative path"):
        load_manifest(path)


def test_lost_response_is_resolved_and_resume_never_reposts_success(tmp_path):
    image = png(tmp_path / "image.png")
    manifest = build_manifest(tmp_path, "http://localhost", LIMITS)
    path = tmp_path / "state.json"
    posts = []
    committed = False
    key = manifest["items"][0]["key"]
    resource_id = "e7a2ed15-661e-498d-878f-77f708028b75"

    def handler(request):
        nonlocal committed
        if request.url.path.endswith("resolve"):
            return httpx.Response(200, json={"items": [dict(
                key=key, state="completed" if committed else "missing", outcome="created" if committed else None,
                resource_id=resource_id if committed else None, resource_status="pending" if committed else None,
                duplicate=False, error=None,
            )]})
        posts.append(request)
        assert load_manifest(path)["items"][0]["key"] == request.headers["Idempotency-Key"]
        assert load_manifest(path)["items"][0]["status"] == "uploading"
        assert hashlib.sha256(image).hexdigest().encode() in request.read()
        committed = True
        raise httpx.ReadTimeout("response lost", request=request)

    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        assert run_manifest(client, path, manifest) == 0
        assert manifest["items"][0]["resource_id"] == resource_id
        assert run_manifest(client, path, manifest) == 0
    assert len(posts) == 1
    assert summary(manifest)["resources"] == {"pending": 1}


def test_unknown_reuses_key_and_repairs_only_failed_files(tmp_path):
    png(tmp_path / "good.png")
    broken = png(tmp_path / "bad.png", "red")
    (tmp_path / "bad.png").write_bytes(broken[:-8])
    manifest = build_manifest(tmp_path, "http://localhost", LIMITS)
    path = tmp_path / "state.json"
    keys = {item["relative_path"]: item["key"] for item in manifest["items"]}
    calls = []
    first = True

    def handler(request):
        nonlocal first
        if request.url.path.endswith("resolve"):
            return httpx.Response(200, json={"items": [dict(key=key, state="missing")
                for key in json.loads(request.content)["keys"]]})
        key = request.headers["Idempotency-Key"]
        calls.append(key)
        if first:
            first = False
            raise httpx.ConnectError("lost", request=request)
        return httpx.Response(201, json=dict(id="e7a2ed15-661e-498d-878f-77f708028b75", import_outcome="created",
                                              status="pending", duplicate=False))

    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        assert run_manifest(client, path, manifest) == 3
        assert run_manifest(client, path, manifest) == 1  # corrupt file remains a known failure
        assert calls == [keys["good.png"], keys["good.png"]]
        png(tmp_path / "bad.png", "red")
        assert run_manifest(client, path, manifest, retry_failed=True) == 0
    assert len(calls) == 3 and calls[-1] != keys["bad.png"]
    UUID(calls[-1])
    assert manifest["items"][0]["attempts"][0]["key"] == keys["bad.png"]


def test_interrupt_stops_queue_and_failure_before_sending_prevents_post(tmp_path):
    png(tmp_path / "a.png")
    png(tmp_path / "b.png", "red")
    manifest = build_manifest(tmp_path, "http://localhost", LIMITS)
    stop = Event()
    calls = []

    def handler(request):
        if request.url.path.endswith("resolve"):
            return httpx.Response(200, json={"items": [dict(key=key, state="missing")
                for key in json.loads(request.content)["keys"]]})
        calls.append(request)
        stop.set()
        return httpx.Response(201, json=dict(id="e7a2ed15-661e-498d-878f-77f708028b75", import_outcome="created",
                                              status="pending", duplicate=False))

    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        assert run_manifest(client, tmp_path / "state.json", manifest, concurrency=1, stop=stop) == 130
    assert len(calls) == 1 and manifest["items"][1]["status"] == "pending"


def test_processing_failure_is_counted_separately_from_upload_failure(tmp_path):
    png(tmp_path / "image.png")
    manifest = build_manifest(tmp_path, "http://localhost", LIMITS)
    item = manifest["items"][0]
    item.update(status="created", resource_id="same", resource_status="failed")
    manifest["items"].append(dict(item, status="skipped"))
    assert exit_code(manifest) == 0
    assert summary(manifest)["resources"] == {"failed": 1}


def test_local_persistence_failure_prevents_upload(tmp_path, monkeypatch):
    png(tmp_path / "image.png")
    manifest = build_manifest(tmp_path, "http://localhost", LIMITS)
    posts = []

    def handler(request):
        posts.append(request.url.path)
        return httpx.Response(200, json={"items": [dict(key=manifest["items"][0]["key"], state="missing")]})

    def fail_save(*args):
        raise OSError("disk full")

    monkeypatch.setattr("mmkb.importer.save_manifest", fail_save)
    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OSError):
            run_manifest(client, tmp_path / "state.json", manifest)
    assert posts == ["/api/imports/resolve"]
