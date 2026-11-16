"""Processing lifecycle checks on an explicitly selected disposable database."""
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from PIL import Image
import pytest
import sqlalchemy as sa

from mmkb.api import create_app
from mmkb.config import AppConfig
from mmkb.db.models import Resource, ResourceIndex, StorageProfile
from mmkb.db.session import create_session_factory
from mmkb.errors import ResourceError, StorageError
from mmkb.processing import Processor
from mmkb.providers import ImageInput, ProviderError, Providers
from mmkb.resources import ResourceService
from mmkb.search import SearchService
from mmkb.storage import StorageResolver, initialize_profile
from mmkb.worker import Worker, WorkerLock

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def worker_sessions():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable mmkb_test_* database")
    if not (sa.engine.make_url(url).database or "").startswith("mmkb_test_"):
        pytest.fail("TEST_DATABASE_URL must name a disposable mmkb_test_* database")
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    sessions = create_session_factory(url)
    yield sessions
    sessions.kw["bind"].dispose()


@pytest.fixture
def setup(worker_sessions, tmp_path):
    sessions = worker_sessions
    with sessions.begin() as session:
        session.execute(sa.delete(Resource))
        session.execute(sa.delete(StorageProfile))
    config = AppConfig(database_url="postgresql://unused", local_storage_root=str(tmp_path / "assets"))
    resolver = StorageResolver(config)
    initialize_profile(sessions, config, resolver)
    embedding, vision = Mock(), Mock()
    embedding.embed.return_value = [1.0] + [0.0] * 2047
    vision.classify.return_value = "visual_image"
    vision.caption.return_value = "图片描述"
    vision.ocr.return_value = "图片文字"
    processor = Processor(Providers(embedding, vision))
    worker = Worker(sessions, config, resolver, processor)
    service = ResourceService(sessions, config, resolver)
    image = BytesIO()
    Image.new("RGB", (8, 6), "blue").save(image, format="PNG")
    try:
        yield sessions, config, resolver, embedding, worker, service, image.getvalue()
    finally:
        resolver.close()


def compute(worker, claimed, image_bytes=None):
    image = ImageInput(image_bytes, "image/png") if image_bytes else None
    return worker.processor.process(claimed.snapshot, image=image)


@pytest.mark.parametrize("kind,route", [("image", "structured_image"), ("image", "visual_image"),
    ("image", "visdoc"), ("text_image", "text_image"), ("text", "text")])
def test_import_process_search_and_delete_end_to_end(setup, kind, route):
    sessions, config, resolver, model, worker, _, image = setup
    query = SearchService(sessions, config, client_factory=lambda: model)
    try:
        with TestClient(create_app(config, sessions, resolver, query)) as client:
            data = {"title": "资料标题", "tags": "标签", "workflow": route}
            if kind != "image":
                data["user_text"] = "用户文字"
            files = {"file": ("sample.png", image)} if kind != "text" else None
            created = client.post("/api/resources", data=data, files=files).json()
            resource_id = created["id"]
            assert created["status"] == "pending"
            assert worker.run_once()["outcome"] == "ready"
            detail = client.get(f"/api/resources/{resource_id}").json()
            assert detail["status"] == "ready" and detail["workflow"] == route
            assert detail["extra"]["processing"]["warnings"] == []
            for inputs in ({"data": {"text": "query", "tags": "标签"}},
                           {"files": {"file": ("query.png", image)}},
                           {"data": {"text": "query"}, "files": {"file": ("query.png", image)}}):
                response = client.post("/api/search", **inputs)
                assert response.status_code == 200, response.text
                assert [item["resource_id"] for item in response.json()["items"]] == [resource_id]
            assert client.get(f"/api/resources/{resource_id}/original").status_code == (404 if kind == "text" else 200)
            assert client.delete(f"/api/resources/{resource_id}").status_code == 204
            assert client.post("/api/search", data={"text": "query"}).json()["items"] == []
            assert client.get(f"/api/resources/{resource_id}/original").status_code == 404
    finally:
        query.close()


def test_edit_fences_both_old_success_and_old_failure(setup):
    sessions, _, _, _, worker, service, image = setup
    created = service.create(source=BytesIO(image), workflow="structured_image")
    claimed = worker.claim()
    result = compute(worker, claimed, image)
    edited = service.update(created["id"], created["revision"], {"caption": "人工修正", "ocr_text": ""})
    assert not worker.complete(result)
    assert not worker.fail(claimed.snapshot, RuntimeError("old failure"))
    assert service.get(created["id"])["status"] == "pending"
    assert worker.run_once()["outcome"] == "ready"
    detail = service.get(created["id"])
    assert (detail["revision"], detail["caption"], detail["ocr_text"]) == (edited["revision"], "人工修正", "")
    with sessions() as session:
        index = session.get(ResourceIndex, created["id"])
        assert "人工" in index.segmented_text and index.embedding is not None


def test_deletion_fences_late_model_results(setup):
    sessions, _, _, _, worker, service, _ = setup
    created = service.create(user_text="text")
    claimed = worker.claim()
    result = compute(worker, claimed)
    service.delete(created["id"])
    assert not worker.complete(result)
    assert not worker.fail(claimed.snapshot, RuntimeError("late failure"))
    with sessions() as session:
        assert session.get(Resource, created["id"]) is None
        assert session.get(ResourceIndex, created["id"]) is None


def test_model_work_holds_no_business_transaction_or_row_lock(setup):
    sessions, _, _, model, worker, service, _ = setup
    created = service.create(user_text="text")
    vector = model.embed.return_value

    def encode(**kwargs):
        assert sessions.kw["bind"].pool.checkedout() == 0
        with sessions.begin() as session:
            resource = session.scalar(sa.select(Resource).where(Resource.id == created["id"]).with_for_update(nowait=True))
            assert resource.status == "processing"
        return vector

    model.embed.side_effect = encode
    assert worker.run_once()["outcome"] == "ready"


def test_embedding_failure_can_be_retried_without_leaking_service_details(setup):
    sessions, _, _, model, worker, service, _ = setup
    created = service.create(user_text="text")
    model.embed.side_effect = ProviderError("embedding", "timeout", "private deployment details")
    assert worker.run_once()["outcome"] == "failed"
    detail = service.get(created["id"])
    assert "timeout" in detail["error"] and "private" not in detail["error"]
    with sessions() as session:
        assert session.get(ResourceIndex, created["id"]) is None
    service.retry(created["id"], detail["revision"])
    model.embed.side_effect = None
    assert worker.run_once()["outcome"] == "ready"


def test_index_failure_rolls_back_ready_state_and_parsed_fields(setup):
    sessions, _, _, _, worker, service, image = setup
    created = service.create(source=BytesIO(image), workflow="structured_image")
    result = compute(worker, worker.claim(), image)

    def reject(*args):
        raise sa.exc.SQLAlchemyError("simulated index failure")

    sa.event.listen(ResourceIndex, "before_insert", reject)
    try:
        with pytest.raises(sa.exc.SQLAlchemyError):
            worker.complete(result)
    finally:
        sa.event.remove(ResourceIndex, "before_insert", reject)
    with sessions() as session:
        resource = session.get(Resource, created["id"])
        assert resource.status == "processing" and resource.caption is None
        assert session.get(ResourceIndex, created["id"]) is None
    assert worker.complete(result)


def test_live_lock_excludes_second_worker_and_recovery_fences_old_revision(setup):
    sessions, _, _, _, worker, service, _ = setup
    created = service.create(user_text="text")
    old = worker.claim()
    result = compute(worker, old)
    with WorkerLock(sessions.kw["bind"]) as guard:
        worker.ensure_owner = guard.check
        with pytest.raises(ResourceError, match="Another Worker"):
            with WorkerLock(sessions.kw["bind"]):
                pytest.fail("A second owner must be rejected")
        assert worker.recover_processing() == 1
        recovered = service.get(created["id"])
        assert recovered["status"] == "pending" and recovered["revision"] == old.snapshot.revision + 1
        new = worker.claim()
        assert not worker.complete(result)
        assert not worker.fail(old.snapshot, RuntimeError("old failure"))
        assert worker.complete(compute(worker, new))
    with WorkerLock(sessions.kw["bind"]) as guard:
        guard.check()


def test_terminated_lock_session_aborts_and_requires_explicit_recovery(setup):
    sessions, _, _, _, worker, service, _ = setup
    service.create(user_text="text")
    with WorkerLock(sessions.kw["bind"]) as guard:
        worker.ensure_owner = guard.check
        old = worker.claim()
        result = compute(worker, old)
        with sessions.begin() as session:
            session.execute(sa.text("SELECT pg_terminate_backend(:pid)"), {"pid": guard.backend_pid})
        with pytest.raises(sa.exc.SQLAlchemyError):
            worker.complete(result)
    with WorkerLock(sessions.kw["bind"]) as guard:
        worker.ensure_owner = guard.check
        assert worker.recover_processing() == 1
        new = worker.claim()
        assert not worker.complete(result)
        assert worker.complete(compute(worker, new))


def test_worker_retries_deleted_file_cleanup(setup, monkeypatch):
    sessions, _, resolver, _, worker, service, image = setup
    created = service.create(source=BytesIO(image))
    with sessions() as session:
        storage = resolver(session.get(Resource, created["id"]).storage_profile)
    delete = storage.delete
    monkeypatch.setattr(storage, "delete", Mock(side_effect=StorageError()))
    service.delete(created["id"])
    with sessions() as session:
        assert session.get(Resource, created["id"]).deleted_at is not None
    monkeypatch.setattr(storage, "delete", delete)
    result = worker.run_once()
    assert result["outcome"] == "idle" and result["cleanup"]["cleaned"] == 1
    with sessions() as session:
        assert session.get(Resource, created["id"]) is None


def test_cli_idle_run_and_recovery_do_not_make_model_requests(setup):
    sessions, config, _, _, worker, service, _ = setup
    environment = dict(os.environ, DATABASE_URL=os.environ["TEST_DATABASE_URL"],
                       STORAGE_BACKEND="local", STORAGE_PROFILE_NAME="local-default",
                       LOCAL_STORAGE_ROOT=config.local_storage_root,
                       EMBEDDING_BASE_URL="http://127.0.0.1:1/v1", EMBEDDING_MODEL="unused",
                       CAPTION_BASE_URL="", CAPTION_MODEL="")
    idle = subprocess.run([sys.executable, "-m", "mmkb.worker", "run", "--once"],
                          env=environment, capture_output=True, text=True, timeout=15)
    assert idle.returncode == 0, idle.stderr
    assert json.loads(idle.stdout)["outcome"] == "idle"
    created = service.create(user_text="text")
    old = worker.claim()
    # Recovery must not even require a valid model configuration.
    environment.update(EMBEDDING_BASE_URL="", EMBEDDING_MODEL="")
    with WorkerLock(sessions.kw["bind"]):
        blocked = subprocess.run([sys.executable, "-m", "mmkb.worker", "recover-processing"],
                                 env=environment, capture_output=True, text=True, timeout=15)
        assert blocked.returncode == 1 and "worker_running" in blocked.stderr
    recovered = subprocess.run([sys.executable, "-m", "mmkb.worker", "recover-processing"],
                               env=environment, capture_output=True, text=True, timeout=15)
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout) == {"requeued": 1}
    assert service.get(created["id"])["revision"] == old.snapshot.revision + 1
