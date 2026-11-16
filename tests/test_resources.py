from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from uuid import UUID, uuid4

from PIL import Image
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
import sqlalchemy as sa

from mmkb.db.models import Resource, ResourceIndex, StorageProfile
from mmkb.api import create_app
from mmkb.config import AppConfig
from mmkb.db.session import create_session_factory
from mmkb.errors import ResourceError, StorageError
from mmkb.resources import ResourceService
from mmkb.storage import LocalStorage, StorageResolver, initialize_profile

pytestmark = pytest.mark.integration


@pytest.fixture
def image_bytes():
    output = BytesIO()
    Image.new("RGB", (8, 6), "blue").save(output, format="PNG")
    return output.getvalue()


@pytest.fixture(scope="module")
def database_sessions():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated mmkb_test_* PostgreSQL database")
    if not (sa.engine.make_url(url).database or "").startswith("mmkb_test_"):
        pytest.fail("TEST_DATABASE_URL must name a disposable mmkb_test_* database")
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
    sessions = create_session_factory(url)
    yield sessions
    sessions.kw["bind"].dispose()


@pytest.fixture
def sessions(database_sessions):
    with database_sessions.begin() as session:
        session.execute(sa.delete(Resource))
        session.execute(sa.delete(StorageProfile))
    return database_sessions


@pytest.fixture
def local_config(sessions, tmp_path):
    config = AppConfig(database_url=sessions.kw["bind"].url.render_as_string(hide_password=False),
                       storage_backend="local", local_storage_root=str(tmp_path / "assets"))
    resolver = StorageResolver(config)
    initialize_profile(sessions, config, resolver)
    return config


@pytest.fixture
def client(sessions, local_config):
    with TestClient(create_app(local_config, sessions)) as client:
        yield client


def test_three_inputs_and_original(client, image_bytes):
    text = client.post("/api/resources", data={"title": "note", "user_text": "hello", "tags": [" blue ", "blue", "red"]})
    assert text.status_code == 201, text.text
    assert text.json()["input_kind"] == text.json()["workflow"] == "text"
    assert text.json()["tags"] == ["blue", "red"]
    assert client.get(f"/api/resources/{text.json()['id']}/original").status_code == 404
    image = client.post("/api/resources", files={"file": ("untrusted.jpg", image_bytes, "text/plain")})
    assert image.status_code == 201, image.text
    detail = image.json()
    assert detail["workflow"] is None
    assert detail["extra"]["mime_type"] == "image/png"
    original = client.get(detail["original_url"])
    assert original.content == image_bytes
    assert original.headers["content-type"] == "image/png"
    both = client.post("/api/resources", data={"user_text": "caption"}, files={"file": ("image.png", image_bytes)})
    assert both.status_code == 201
    assert both.json()["input_kind"] == both.json()["workflow"] == "text_image"
    assert both.json()["duplicate"] is True
    assert both.json()["id"] != detail["id"]
    assert not {"object_key", "storage_profile_id", "credential_ref"} & detail.keys()
    filtered = client.get("/api/resources", params=[("tags", "blue"), ("tags", "red")]).json()
    assert filtered["total"] == 1
    assert client.get("/api/resources", params={"input_kind": "image", "limit": 1}).json()["total"] == 1


@pytest.mark.parametrize("data,expected", [({}, 422), ({"user_text": "  "}, 422),
    ({"user_text": "hello", "workflow": "visdoc"}, 422)])
def test_invalid_inputs(client, data, expected):
    response = client.post("/api/resources", data=data)
    assert response.status_code == expected
    assert "code" in response.json()


def test_invalid_files_and_limits(client, image_bytes):
    assert client.post("/api/resources", files={"file": ("empty.png", b"")}).status_code == 422
    assert client.post("/api/resources", files={"file": ("fake.png", b"not an image")}).status_code == 415
    output = BytesIO()
    Image.new("RGB", (3, 3)).save(output, format="GIF")
    assert client.post("/api/resources", files={"file": ("file.png", output.getvalue())}).status_code == 415
    assert client.post("/api/resources", files={"file": ("broken.png", image_bytes[:-8])}).status_code == 422
    service = client.app.state.resources
    service.config = replace(service.config, max_upload_bytes=10, max_text_chars=10)
    assert client.post("/api/resources", files={"file": ("big.png", image_bytes)}).status_code == 413
    assert client.post("/api/resources", data={"user_text": "x" * 11}).status_code == 413
    assert client.get("/api/resources").json()["total"] == 0


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_supported_formats(client, fmt, mime):
    output = BytesIO()
    Image.new("RGB", (3, 3)).save(output, format=fmt)
    response = client.post("/api/resources", files={"file": ("file", output.getvalue())})
    assert response.status_code == 201
    assert response.json()["extra"]["mime_type"] == mime


def add_index(sessions, resource_id, status="ready"):
    with sessions.begin() as session:
        resource = session.get(Resource, UUID(resource_id))
        resource.status = status
        resource.caption = "user correction"
        resource.ocr_text = "OCR correction"
        session.add(ResourceIndex(resource_id=resource.id, embedding=[1.0] + [0.0] * 2047))


def test_processing_edit_invalidates_index_and_revision(client, sessions, image_bytes):
    created = client.post("/api/resources", files={"file": ("file.png", image_bytes)}).json()
    path = f"/api/resources/{created['id']}"
    add_index(sessions, created["id"], status="processing")
    edited = client.patch(path, json={"revision": 1, "title": "updated", "user_text": "new caption"})
    assert edited.status_code == 200, edited.text
    detail = edited.json()
    assert (detail["revision"], detail["status"], detail["input_kind"], detail["workflow"]) == (2, "pending", "text_image", "text_image")
    assert detail["caption"] == "user correction"
    assert detail["ocr_text"] == "OCR correction"
    with sessions() as session:
        assert session.get(ResourceIndex, UUID(created["id"])) is None
        # Future Worker conditional commits for the old revision cannot match.
        assert session.scalar(sa.select(Resource.id).where(Resource.id == UUID(created["id"]), Resource.revision == 1, Resource.status == "processing")) is None
    assert client.patch(path, json={"revision": 1, "title": "stale"}).status_code == 409
    cleared = client.patch(path, json={"revision": 2, "caption": None, "ocr_text": "", "user_text": None}).json()
    assert cleared["caption"] is None and cleared["ocr_text"] == ""
    assert cleared["input_kind"] == "image" and cleared["workflow"] is None
    assert client.patch(path, json={"revision": 3, "tags": None}).status_code == 422
    assert client.patch(path, json={"revision": 3, "title": None}).status_code == 422
    assert client.patch(path, json={"revision": 3, "status": "ready"}).status_code == 422


def test_concurrent_edits(sessions, local_config):
    service = ResourceService(sessions, local_config)
    resource_id = service.create(user_text="original")["id"]
    barrier = Barrier(2)

    def edit(title):
        barrier.wait(timeout=5)
        try:
            service.update(resource_id, 1, {"title": title})
            return 200
        except ResourceError as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["first", "second"]))
    assert sorted(results) == [200, 409]
    assert service.get(resource_id)["revision"] == 2


def test_retry(client, sessions):
    created = client.post("/api/resources", data={"user_text": "hello"}).json()
    path = f"/api/resources/{created['id']}/retry"
    assert client.post(path, json={"revision": 1}).status_code == 409
    with sessions.begin() as session:
        resource = session.get(Resource, UUID(created["id"]))
        resource.status, resource.error, resource.caption = "failed", "provider timeout", "correction"
    assert client.post(path, json={"revision": 2}).status_code == 409
    response = client.post(path, json={"revision": 1})
    assert response.status_code == 200
    assert response.json()["revision"] == 2 and response.json()["status"] == "pending"
    assert response.json()["error"] is None and response.json()["caption"] == "correction"


def test_delete_cleanup_failure_and_retry(client, sessions, image_bytes, monkeypatch):
    created = client.post("/api/resources", files={"file": ("file.png", image_bytes)}).json()
    resource_id = UUID(created["id"])
    add_index(sessions, created["id"], status="processing")
    path = f"/api/resources/{resource_id}"
    original_delete = LocalStorage.delete

    def fail_delete(*args):
        raise StorageError()

    monkeypatch.setattr(LocalStorage, "delete", fail_delete)
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404
    assert client.get(created["original_url"]).status_code == 404
    assert client.get("/api/resources").json()["total"] == 0
    assert client.patch(path, json={"revision": 1, "title": "late edit"}).status_code == 404
    with sessions() as session:
        resource = session.get(Resource, resource_id)
        assert resource.deleted_at is not None and resource.revision == 2
        assert "Cleanup failed" in resource.error
        assert session.get(ResourceIndex, resource_id) is None
    monkeypatch.setattr(LocalStorage, "delete", original_delete)
    assert client.app.state.resources.cleanup_batch() == {"scanned": 1, "cleaned": 1, "failed": 0}
    with sessions() as session:
        assert session.get(Resource, resource_id) is None
    assert client.delete(path).status_code == 204


def test_text_delete(client, sessions):
    created = client.post("/api/resources", data={"user_text": "hello"}).json()
    assert client.delete(f"/api/resources/{created['id']}").status_code == 204
    with sessions() as session:
        assert session.get(Resource, UUID(created["id"])) is None


def test_import_compensation(sessions, local_config, image_bytes):
    class FailingSessions:
        def __call__(self):
            return sessions()

        @contextmanager
        def begin(self):
            with sessions() as session:
                with session.begin():
                    yield session
                    raise sa.exc.SQLAlchemyError("simulated commit failure")

    service = ResourceService(FailingSessions(), local_config)
    with pytest.raises(sa.exc.SQLAlchemyError):
        service.create(source=BytesIO(image_bytes))
    assert list(Path(local_config.local_storage_root).rglob("*.png")) == []
    assert ResourceService(sessions, local_config).list()["total"] == 0


def test_storage_failure_leaves_no_record(sessions, local_config, image_bytes):
    class FailingStorage:
        deleted = False

        def put(self, *args):
            raise StorageError()

        def delete(self, *args):
            self.deleted = True

    storage = FailingStorage()
    service = ResourceService(sessions, local_config, lambda profile: storage)
    with pytest.raises(StorageError):
        service.create(source=BytesIO(image_bytes))
    assert storage.deleted
    assert service.list()["total"] == 0


def test_profile_initialization_and_switch(sessions, local_config, image_bytes, tmp_path):
    resolver = StorageResolver(local_config)
    assert initialize_profile(sessions, local_config, resolver).name == local_config.storage_profile_name
    with pytest.raises(ResourceError):
        initialize_profile(sessions, replace(local_config, local_storage_root=str(tmp_path / "other")), resolver)
    service = ResourceService(sessions, local_config)
    resource = service.create(source=BytesIO(image_bytes))
    second = replace(local_config, storage_profile_name="second", local_storage_root=str(tmp_path / "second"))
    initialize_profile(sessions, second, StorageResolver(second))
    stream, mime = ResourceService(sessions, second).original(resource["id"])
    with stream:
        assert stream.read() == image_bytes


def test_original_response_closes_stream(client, image_bytes, monkeypatch):
    stream = BytesIO(image_bytes)
    monkeypatch.setattr(client.app.state.resources, "original", lambda resource_id: (stream, "image/png"))
    response = client.get(f"/api/resources/{uuid4()}/original")
    assert response.content == image_bytes
    assert stream.closed


def test_s3_http_lifecycle(sessions, local_config, image_bytes):
    import boto3
    from botocore.response import StreamingBody
    from botocore.stub import ANY, Stubber
    from mmkb.storage import S3Storage

    config = replace(local_config, storage_backend="s3", storage_profile_name="rustfs-test", s3_prefix="assets/")
    with sessions.begin() as session:
        session.add(StorageProfile(id=uuid4(), name=config.storage_profile_name, backend="s3",
                                   bucket="test-assets", endpoint_url="http://localhost:9000"))
    sdk = boto3.client("s3", endpoint_url="http://localhost:9000", region_name="us-east-1",
                       aws_access_key_id="test", aws_secret_access_key="test")
    body = StreamingBody(BytesIO(image_bytes), len(image_bytes))

    def resolver(profile):
        # Credential resolution and storage requests must not hold a DB transaction.
        assert sessions.kw["bind"].pool.checkedout() == 0
        assert profile.name == "rustfs-test"
        return S3Storage(profile.bucket, sdk)

    try:
        with Stubber(sdk) as stub, TestClient(create_app(config, sessions, resolver)) as http:
            stub.add_response("put_object", {}, {"Bucket": "test-assets", "Key": ANY,
                              "Body": image_bytes, "ContentType": "image/png"})
            created = http.post("/api/resources", files={"file": ("file.png", image_bytes)}).json()
            with sessions() as session:
                key = session.get(Resource, UUID(created["id"])).object_key
                assert key == f"assets/{created['id']}/original.png"
            stub.add_response("get_object", {"Body": body}, {"Bucket": "test-assets", "Key": key})
            assert http.get(created["original_url"]).content == image_bytes
            assert body._raw_stream.closed
            stub.add_response("delete_object", {}, {"Bucket": "test-assets", "Key": key})
            assert http.delete(f"/api/resources/{created['id']}").status_code == 204
            assert http.get(created["original_url"]).status_code == 404
            stub.assert_no_pending_responses()
    finally:
        sdk.close()


def test_cleanup_command(sessions, local_config, image_bytes):
    service = ResourceService(sessions, local_config)
    resource = service.create(source=BytesIO(image_bytes))
    with sessions.begin() as session:
        session.execute(sa.update(Resource).where(Resource.id == resource["id"]).values(deleted_at=sa.func.now()))
    result = subprocess.run([sys.executable, "-m", "mmkb.resources", "cleanup-deleted"],
                            env=dict(os.environ, DATABASE_URL=local_config.database_url),
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "'cleaned': 1" in result.stdout and "'failed': 0" in result.stdout
    with sessions() as session:
        assert session.get(Resource, resource["id"]) is None


def test_database_errors_are_sanitized(client, monkeypatch):
    def fail(*args):
        raise sa.exc.SQLAlchemyError("private database statement and user content")
    monkeypatch.setattr(client.app.state.resources, "get", fail)
    response = client.get(f"/api/resources/{uuid4()}")
    assert response.status_code == 503
    assert response.json()["code"] == "database_unavailable"
    assert "private" not in response.text
