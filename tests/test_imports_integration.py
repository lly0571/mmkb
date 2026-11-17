"""Import receipts, real transactions, contention and storage crash recovery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import hashlib
from io import BytesIO
import os
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from PIL import Image
import pytest
import sqlalchemy as sa

from mmkb.api import create_app
from mmkb.config import AppConfig
from mmkb.db.models import ImportItem, Resource, StorageProfile
from mmkb.db.session import create_session_factory
from mmkb.errors import ResourceError, StorageError
from mmkb.imports import ImportService
from mmkb.resources import IMAGE_WORKFLOWS, ResourceService
from mmkb.storage import StorageResolver, initialize_profile

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def factory():
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
def setup(factory, tmp_path):
    with factory.begin() as session:
        session.execute(sa.delete(ImportItem))
        session.execute(sa.delete(Resource))
        session.execute(sa.delete(StorageProfile))
    config = AppConfig(database_url="postgresql://unused", local_storage_root=str(tmp_path / "assets"))
    resolver = StorageResolver(config)
    initialize_profile(factory, config, resolver)
    resources = ResourceService(factory, config, resolver)
    output = BytesIO()
    Image.new("RGB", (9, 7), "blue").save(output, format="PNG")
    try:
        yield factory, config, resolver, resources, ImportService(resources), output.getvalue()
    finally:
        resolver.close()


def admit(imports, key, image, **fields):
    return imports.create(key, source=BytesIO(image), filename="sample.png", **fields)


def test_http_legacy_compatibility_and_keyed_imports(setup):
    sessions, config, resolver, _, _, image = setup
    with TestClient(create_app(config, sessions, resolver)) as client:
        options = client.get("/api/imports/options")
        assert options.status_code == 200 and options.headers["cache-control"] == "no-store"
        assert set(options.json()["image_workflows"]) == set(IMAGE_WORKFLOWS)
        original = client.post("/api/resources", files={"file": ("sample.png", image)})
        assert original.status_code == 201
        assert "replayed" not in original.json() and "import_outcome" not in original.json()
        key = str(uuid4())
        fields = dict(duplicate_policy="skip", title="do not merge", tags=["new"],
                      expected_sha256=hashlib.sha256(image).hexdigest())
        skipped = client.post("/api/resources", headers={"Idempotency-Key": key}, data=fields,
                              files={"file": ("sample.png", image)})
        assert skipped.status_code == 200, skipped.text
        assert skipped.json()["id"] == original.json()["id"]
        assert skipped.json()["import_outcome"] == "skipped"
        assert skipped.json()["title"] == "" and skipped.json()["tags"] == []
        replay = client.post("/api/resources", headers={"Idempotency-Key": key}, data=fields,
                             files={"file": ("sample.png", image)})
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        resolved = client.post("/api/imports/resolve", json={"keys": [key, str(uuid4())]})
        assert [item["state"] for item in resolved.json()["items"]] == ["completed", "missing"]
        assert not {"object_key", "storage_profile", "request_hash"} & resolved.json()["items"][0].keys()
        assert client.post("/api/imports/resolve", json={"keys": [key] * 101}).status_code == 422
        assert client.post("/api/resources", data={"duplicate_policy": "skip"},
                           files={"file": ("sample.png", image)}).json()["code"] == "import_key_required"


def test_content_and_field_conflicts_and_failed_duplicate(setup):
    sessions, _, _, resources, imports, image = setup
    key = uuid4()
    first = admit(imports, key, image, tags=[" blue ", "blue"], title="title")
    assert admit(imports, key, image, tags=["blue"], title="title")["id"] == first["id"]
    for fields in (dict(title="changed"), dict(duplicate_policy="skip")):
        with pytest.raises(ResourceError, match="different content"):
            admit(imports, key, image, **fields)
    with pytest.raises(ResourceError) as error:
        admit(imports, uuid4(), image, expected_sha256="0" * 64)
    assert error.value.code == "source_changed"
    with sessions.begin() as session:
        session.get(Resource, first["id"]).status = "failed"
    skipped = admit(imports, uuid4(), image, duplicate_policy="skip", workflow="visdoc")
    assert skipped["id"] == first["id"] and skipped["status"] == "failed"
    assert skipped["workflow"] is None and skipped["tags"] == ["blue"]
    created = admit(imports, uuid4(), image, duplicate_policy="create")
    assert created["id"] != first["id"] and created["duplicate"] is True
    assert resources.list()["total"] == 2


def test_receipt_survives_delete_and_cannot_recreate(setup):
    _, _, _, resources, imports, image = setup
    key = uuid4()
    first = admit(imports, key, image)
    resources.delete(first["id"])
    assert imports.resolve([key])["items"][0]["resource_status"] == "deleted"
    with pytest.raises(ResourceError) as error:
        admit(imports, key, image)
    assert error.value.status_code == 410
    assert admit(imports, uuid4(), image, duplicate_policy="skip")["import_outcome"] == "created"


def test_fresh_duplicate_skip_does_not_require_storage_access(setup, monkeypatch):
    _, config, _, resources, imports, image = setup
    existing = resources.create(source=BytesIO(image))
    resources.config = replace(config, storage_profile_name="not-initialized")
    monkeypatch.setattr(resources, "storage_resolver", lambda *_: pytest.fail("fresh skip accessed storage"))
    assert admit(imports, uuid4(), image, duplicate_policy="skip")["id"] == existing["id"]


def test_concurrent_skip_and_legacy_writer_share_content_lock(setup, monkeypatch):
    _, _, resolver, resources, imports, image = setup
    entered, release = Event(), Event()
    profile, _ = resources.plan_storage(uuid4(), "png")
    storage = resolver(profile)
    original_put = storage.put

    def gated(*args):
        entered.set()
        assert release.wait(10)
        return original_put(*args)

    monkeypatch.setattr(storage, "put", gated)
    first_key, second_key = uuid4(), uuid4()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(admit, imports, first_key, image, duplicate_policy="skip")
        assert entered.wait(5)
        try:
            assert imports.resolve([first_key])["items"][0]["state"] == "reserved"
            for operation in (lambda: admit(imports, first_key, image, duplicate_policy="skip"),
                              lambda: admit(imports, second_key, image, duplicate_policy="skip"),
                              lambda: resources.create(source=BytesIO(image))):
                with pytest.raises(ResourceError) as error:
                    operation()
                assert error.value.code == "import_busy"
        finally:
            release.set()
        first = future.result()
    assert admit(imports, second_key, image, duplicate_policy="skip")["id"] == first["id"]
    assert resources.list()["total"] == 1


def test_crash_after_object_write_recovers_same_location_after_profile_change(setup, monkeypatch):
    sessions, config, resolver, resources, imports, image = setup
    profile, _ = resources.plan_storage(uuid4(), "png")
    storage = resolver(profile)
    original_put = storage.put

    class Crash(BaseException):
        pass

    def crash(*args):
        original_put(*args)
        raise Crash()

    monkeypatch.setattr(storage, "put", crash)
    key = uuid4()
    with pytest.raises(Crash):
        admit(imports, key, image)
    with sessions() as session:
        receipt = session.get(ImportItem, key)
        planned, object_key = receipt.planned_resource_id, receipt.object_key
        assert receipt.state == "reserved" and session.get(Resource, planned) is None
    assert (Path(config.local_storage_root) / object_key).read_bytes() == image
    monkeypatch.setattr(storage, "put", original_put)
    resources.config = replace(config, storage_profile_name="not-initialized")
    result = admit(imports, key, image)
    assert result["id"] == planned
    assert len(list(Path(config.local_storage_root).rglob("*.png"))) == 1


def test_storage_failure_keeps_identifiable_object_and_retry_cleans_it_before_skip(setup, monkeypatch):
    sessions, config, resolver, resources, imports, image = setup
    profile, _ = resources.plan_storage(uuid4(), "png")
    storage = resolver(profile)
    original_put = storage.put

    def failed_put(*args):
        original_put(*args)
        raise StorageError()

    monkeypatch.setattr(storage, "put", failed_put)
    key = uuid4()
    with pytest.raises(StorageError):
        admit(imports, key, image, duplicate_policy="skip")
    with sessions() as session:
        receipt = session.get(ImportItem, key)
        assert receipt.state == "failed" and receipt.object_key
    monkeypatch.setattr(storage, "put", original_put)
    other = resources.create(source=BytesIO(image))
    skipped = admit(imports, key, image, duplicate_policy="skip")
    assert skipped["id"] == other["id"]
    assert len(list(Path(config.local_storage_root).rglob("*.png"))) == 1


@pytest.mark.parametrize("lookup_available", [True, False])
def test_commit_acknowledgement_loss_never_deletes_committed_original(setup, lookup_available):
    sessions, config, resolver, _, _, image = setup

    class LostAcknowledgement:
        commits = 0
        lost = False

        def __call__(self):
            if self.lost and not lookup_available:
                raise sa.exc.SQLAlchemyError("database unreachable")
            return sessions()

        @contextmanager
        def begin(self):
            self.commits += 1
            count = self.commits
            with sessions.begin() as session:
                yield session
            if count == 3:
                self.lost = True
                raise sa.exc.SQLAlchemyError("COMMIT acknowledgement lost")

    imports = ImportService(ResourceService(LostAcknowledgement(), config, resolver))
    key = uuid4()
    if lookup_available:
        assert admit(imports, key, image)["replayed"] is True
    else:
        with pytest.raises(sa.exc.SQLAlchemyError):
            admit(imports, key, image)
    with sessions() as session:
        receipt = session.get(ImportItem, key)
        resource = session.get(Resource, receipt.resource_id)
        assert receipt.state == "completed" and resource is not None
        assert (Path(config.local_storage_root) / resource.object_key).read_bytes() == image


def test_status_resolution_distinguishes_processing_failure_from_import_failure(setup):
    sessions, _, _, _, imports, image = setup
    key = uuid4()
    created = admit(imports, key, image)
    with sessions.begin() as session:
        resource = session.get(Resource, created["id"])
        resource.status, resource.error = "failed", "embedding_timeout"
        resource.workflow = "structured_image"
    receipt = imports.resolve([key])["items"][0]
    assert receipt["state"] == "completed" and receipt["outcome"] == "created"
    assert receipt["error"] is None and receipt["resource_error"] == "embedding_timeout"
    assert receipt["resource_workflow"] == "structured_image"


def test_resolution_tracks_automatic_classification_and_existing_duplicate_route(setup):
    sessions, _, _, resources, imports, image = setup
    key = uuid4()
    created = admit(imports, key, image)
    assert imports.resolve([key])["items"][0]["resource_workflow"] is None
    with sessions.begin() as session:
        resource = session.get(Resource, created["id"])
        resource.status, resource.workflow = "ready", "structured_image"
    duplicate_key = uuid4()
    admit(imports, duplicate_key, image, duplicate_policy="skip", workflow="visdoc")
    assert [item["resource_workflow"] for item in imports.resolve([key, duplicate_key])["items"]] == [
        "structured_image", "structured_image"]
    resources.delete(created["id"])
    assert imports.resolve([key])["items"][0]["resource_workflow"] is None
