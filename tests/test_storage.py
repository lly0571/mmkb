import hashlib
import io
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import boto3
from botocore.exceptions import EndpointConnectionError, ProfileNotFound, ReadTimeoutError
from botocore.response import StreamingBody
from botocore.stub import Stubber
import pytest

from mmkb.config import StorageConfig
from mmkb.db.models import StorageProfile
from mmkb.errors import InvalidKeyError, ObjectNotFoundError, StorageConfigError, StorageError
from mmkb.storage import LocalStorage, S3Storage, StorageResolver, create_storage, validate_key


INVALID_KEYS = ["", "/absolute", "../outside", "a/../b", "./a", "a/./b", "a//b", "a/",
                "C:/file", "C:\\file", "a\\b", "a\x00b", "//server/file"]


@pytest.fixture
def s3_client():
    client = boto3.client(
        "s3", region_name="us-east-1", endpoint_url="https://s3.example",
        aws_access_key_id="test", aws_secret_access_key="test",
    )
    with Stubber(client) as stub:
        yield client, stub
        stub.assert_no_pending_responses()
    client.close()


def test_local_contract(tmp_path, storage_contract):
    storage_contract(LocalStorage(tmp_path), "resources/test")


@pytest.mark.parametrize("key", INVALID_KEYS)
@pytest.mark.parametrize("backend", ["local", "s3"])
def test_invalid_keys_rejected_by_every_operation(tmp_path, key, backend):
    client = Mock()
    storage = LocalStorage(tmp_path) if backend == "local" else S3Storage("bucket", client)
    for operation in (lambda k: storage.put(k, b"content", "image/png"), storage.open, storage.stat, storage.delete):
        with pytest.raises(InvalidKeyError):
            operation(key)
    assert not client.mock_calls
    assert list(tmp_path.iterdir()) == []


def test_valid_key_is_not_rewritten():
    assert validate_key("resources/中文 name/original.jpg") == "resources/中文 name/original.jpg"


@pytest.mark.parametrize("target_inside", [True, False])
@pytest.mark.parametrize("link_is_directory", [True, False])
def test_local_rejects_all_symlink_components(tmp_path, target_inside, link_is_directory):
    root = tmp_path / "root"
    root.mkdir()
    target = (root if target_inside else tmp_path) / "target"
    if link_is_directory:
        target.mkdir()
        (target / "original").write_bytes(b"unchanged")
    else:
        target.write_bytes(b"unchanged")
    (root / "link").symlink_to(target, target_is_directory=link_is_directory)
    key = "link/original" if link_is_directory else "link"
    storage = LocalStorage(root)
    for operation in (lambda k: storage.put(k, b"changed", "image/png"), storage.open, storage.stat, storage.delete):
        with pytest.raises(InvalidKeyError):
            operation(key)
    assert (target / "original" if link_is_directory else target).read_bytes() == b"unchanged"


def test_local_requires_absolute_root_and_regular_files(tmp_path):
    with pytest.raises(StorageConfigError):
        LocalStorage("relative")
    (tmp_path / "directory").mkdir()
    storage = LocalStorage(tmp_path)
    for operation in (storage.open, storage.stat, storage.delete):
        with pytest.raises(StorageError) as error:
            operation("directory")
        assert not isinstance(error.value, ObjectNotFoundError)


def test_local_atomic_replace_failure_keeps_original_and_cleans_temp(tmp_path, monkeypatch):
    storage = LocalStorage(tmp_path)
    storage.put("a/original", b"old", "image/png")

    def fail_replace(*args):
        raise PermissionError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(StorageError):
        storage.put("a/original", b"new", "image/png")
    assert (tmp_path / "a/original").read_bytes() == b"old"
    assert [path.name for path in (tmp_path / "a").iterdir()] == ["original"]


def test_local_interrupted_write_cleans_temp(tmp_path, monkeypatch):
    from mmkb import storage as module
    original = module.tempfile.NamedTemporaryFile

    def interrupted_file(**kwargs):
        output = original(**kwargs)
        output.write = Mock(side_effect=OSError("simulated disk full"))
        return output

    monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", interrupted_file)
    with pytest.raises(StorageError):
        LocalStorage(tmp_path).put("original", b"data", "image/png")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("operation", ["open", "stat", "delete"])
def test_local_permissions_are_not_object_not_found(tmp_path, monkeypatch, operation):
    (tmp_path / "original").write_bytes(b"data")
    storage = LocalStorage(tmp_path)
    method = {"open": "open", "stat": "stat", "delete": "unlink"}[operation]
    original = getattr(Path, method)

    def denied(path, *args, **kwargs):
        if path == tmp_path / "original":
            raise PermissionError("simulated permission error")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, denied)
    with pytest.raises(StorageError) as error:
        getattr(storage, operation)("original")
    assert not isinstance(error.value, ObjectNotFoundError)


@pytest.mark.parametrize("backend", ["local", "s3"])
def test_upload_stream_compatibility_uses_remaining_bytes(tmp_path, backend, s3_client):
    client, stub = s3_client
    stream = io.BytesIO(b"skip-payload")
    stream.read(5)
    if backend == "s3":
        stub.add_response("put_object", {}, {"Bucket": "bucket", "Key": "original", "Body": b"payload", "ContentType": "image/png"})
    storage = LocalStorage(tmp_path) if backend == "local" else S3Storage("bucket", client)
    result = storage.put("original", stream, "image/png")
    assert result.size == 7
    assert result.sha256 == hashlib.sha256(b"payload").hexdigest()
    assert not stream.closed


def test_s3_io_parameters_and_stream_close(s3_client):
    client, stub = s3_client
    params = {"Bucket": "bucket", "Key": "resources/original"}
    raw = io.BytesIO(b"\x00\xffdata")
    body = StreamingBody(raw, 6)
    stub.add_response("put_object", {}, {**params, "Body": b"\x00\xffdata", "ContentType": "image/png"})
    stub.add_response("get_object", {"Body": body}, params)
    stub.add_response("head_object", {"ContentLength": 6}, params)
    stub.add_response("delete_object", {}, params)
    storage = S3Storage("bucket", client)
    result = storage.put(params["Key"], b"\x00\xffdata", content_type="image/png")
    assert result.size == 6
    assert result.sha256 == hashlib.sha256(b"\x00\xffdata").hexdigest()
    with storage.open(params["Key"]) as stream:
        assert stream.read(2) == b"\x00\xff"
        assert stream.read() == b"data"
    assert stream.closed and raw.closed
    stream.close()
    with pytest.raises(ValueError):
        stream.read()
    assert storage.stat(params["Key"]).size == 6
    storage.delete(params["Key"])


@pytest.mark.parametrize("operation,sdk", [("open", "get_object"), ("stat", "head_object"), ("delete", "delete_object")])
@pytest.mark.parametrize("code", ["NoSuchKey", "404", "NotFound"])
def test_s3_missing_object_mapping(s3_client, operation, sdk, code):
    client, stub = s3_client
    stub.add_client_error(sdk, service_error_code=code, http_status_code=404,
                          expected_params={"Bucket": "bucket", "Key": "original"})
    if code != "NoSuchKey":
        stub.add_response("head_bucket", {}, {"Bucket": "bucket"})
    storage = S3Storage("bucket", client)
    if operation == "delete":
        storage.delete("original")
    else:
        with pytest.raises(ObjectNotFoundError):
            getattr(storage, operation)("original")


@pytest.mark.parametrize("operation,sdk", [("open", "get_object"), ("stat", "head_object"), ("delete", "delete_object")])
@pytest.mark.parametrize("code,status", [("NoSuchBucket", 404), ("AccessDenied", 403), ("InternalError", 500)])
def test_s3_failures_are_not_missing_objects(s3_client, operation, sdk, code, status):
    client, stub = s3_client
    stub.add_client_error(sdk, service_error_code=code, http_status_code=status,
                          expected_params={"Bucket": "bucket", "Key": "original"})
    with pytest.raises(StorageError) as error:
        getattr(S3Storage("bucket", client), operation)("original")
    assert not isinstance(error.value, ObjectNotFoundError)


@pytest.mark.parametrize("operation,sdk", [("open", "get_object"), ("stat", "head_object"), ("delete", "delete_object")])
def test_s3_generic_404_does_not_hide_missing_bucket(s3_client, operation, sdk):
    client, stub = s3_client
    stub.add_client_error(sdk, service_error_code="404", http_status_code=404,
                          expected_params={"Bucket": "bucket", "Key": "original"})
    stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404,
                          expected_params={"Bucket": "bucket"})
    with pytest.raises(StorageError) as error:
        getattr(S3Storage("bucket", client), operation)("original")
    assert not isinstance(error.value, ObjectNotFoundError)


def test_s3_network_failure_and_stream_failure_close_resources(s3_client):
    client = Mock()
    client.get_object.side_effect = EndpointConnectionError(endpoint_url="https://s3.example")
    with pytest.raises(StorageError):
        S3Storage("bucket", client).open("original")

    body = Mock()
    body.read.side_effect = ReadTimeoutError(endpoint_url="https://s3.example")
    client.get_object.side_effect = None
    client.get_object.return_value = {"Body": body}
    with pytest.raises(StorageError):
        with S3Storage("bucket", client).open("original") as stream:
            stream.read()
    body.close.assert_called_once()
    assert stream.closed


def test_factory_uses_persisted_profile_and_named_credentials(monkeypatch):
    from mmkb import storage as module
    session = Mock()
    session_factory = Mock(return_value=session)
    monkeypatch.setattr(module.boto3, "Session", session_factory)
    profile = StorageProfile(id=uuid4(), name="old", backend="s3", bucket="old-bucket",
                             endpoint_url="https://old.example", credential_ref="old-credentials")
    settings = StorageConfig(s3_endpoint_url="https://new.example", s3_bucket="new-bucket")
    storage = create_storage(profile, settings)
    assert storage.bucket == "old-bucket"
    session_factory.assert_called_once_with(profile_name="old-credentials")
    args, kwargs = session.client.call_args
    assert args == ("s3",)
    assert kwargs["endpoint_url"] == "https://old.example"
    assert kwargs["region_name"] == "us-east-1"
    config = kwargs["config"]
    assert config.s3["addressing_style"] == "path"
    assert config.signature_version == "s3v4"
    assert (config.connect_timeout, config.read_timeout) == (5, 30)
    assert config.retries == {"mode": "standard", "total_max_attempts": 3}
    storage.close()
    session.client.return_value.close.assert_called_once()


def test_missing_named_credentials_never_fall_back(monkeypatch):
    from mmkb import storage as module
    factory = Mock(side_effect=ProfileNotFound(profile="missing"))
    monkeypatch.setattr(module.boto3, "Session", factory)
    with pytest.raises(StorageConfigError):
        create_storage(StorageProfile(backend="s3", bucket="bucket", credential_ref="missing"))
    factory.assert_called_once_with(profile_name="missing")


@pytest.mark.parametrize("endpoint", ["ftp://example", "https://user:password@example", "https://example?secret=value",
                                      "https://example/#fragment", "http://[invalid", "https://example:invalid"])
def test_invalid_endpoint_is_a_configuration_error_before_credentials(monkeypatch, endpoint):
    from mmkb import storage as module
    factory = Mock()
    monkeypatch.setattr(module.boto3, "Session", factory)
    with pytest.raises(StorageConfigError):
        create_storage(StorageProfile(backend="s3", bucket="bucket", endpoint_url=endpoint))
    factory.assert_not_called()


def test_default_credentials_and_resolver_client_shutdown(monkeypatch):
    from mmkb import storage as module
    factory = Mock()
    monkeypatch.setattr(module.boto3, "Session", factory)
    resolver = StorageResolver(StorageConfig())
    profile = StorageProfile(backend="s3", bucket="bucket")
    assert resolver(profile) is resolver(profile)
    factory.assert_called_once_with(profile_name=None)
    resolver.close()
    resolver.close()
    factory.return_value.client.return_value.close.assert_called_once()


@pytest.mark.parametrize("settings", [StorageConfig(s3_region=""), StorageConfig(s3_addressing_style="wrong"),
                                     StorageConfig(s3_signature_version="s3")])
def test_invalid_s3_runtime_settings(settings):
    with pytest.raises(StorageConfigError):
        create_storage(StorageProfile(backend="s3", bucket="bucket"), settings)


def test_resolver_isolates_profiles_and_closes_clients(tmp_path):
    resolver = StorageResolver(StorageConfig())
    first = StorageProfile(backend="local", local_root=str(tmp_path / "first"))
    second = StorageProfile(backend="local", local_root=str(tmp_path / "second"))
    assert resolver(first) is resolver(first)
    resolver(first).put("same-key", b"first", "image/png")
    resolver(second).put("same-key", b"second", "image/png")
    with resolver(first).open("same-key") as stream:
        assert stream.read() == b"first"
    with resolver(second).open("same-key") as stream:
        assert stream.read() == b"second"
    resolver.close()
    resolver.close()


@pytest.mark.parametrize("profile", [None, StorageProfile(backend="unknown"),
                                     StorageProfile(backend="local"), StorageProfile(backend="s3")])
def test_invalid_profiles(profile):
    with pytest.raises(StorageConfigError):
        create_storage(profile)
