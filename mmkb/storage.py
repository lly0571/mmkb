"""Synchronous Local/S3 storage and explicit profile initialization.

Keys are complete relative paths. Adapters never add deployment prefixes or
change resource records. Callers own input streams and must close read streams.
"""
from __future__ import annotations

import argparse
from contextlib import AbstractContextManager
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import stat
import tempfile
from threading import Lock
from typing import BinaryIO, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
import sqlalchemy as sa

from mmkb.config import PROJECT_ROOT, StorageConfig, load_storage_config
from mmkb.db.models import StorageProfile
from mmkb.db.session import create_session_factory
from mmkb.errors import (
    InvalidKeyError, ObjectNotFound, ObjectNotFoundError, ResourceError,
    StorageConfigError, StorageError,
)

logger = logging.getLogger(__name__)


class Settings(Protocol):
    """Storage settings accepted independently of a business application config."""
    storage_backend: str
    storage_profile_name: str
    local_storage_root: str
    s3_endpoint_url: str | None
    s3_bucket: str | None
    s3_region: str
    s3_credential_profile: str | None
    s3_addressing_style: str
    s3_signature_version: str


def validate_key(key: str) -> str:
    if not isinstance(key, str) or not key or any(c in key for c in ("\\", "\x00", ":")) or any(
        part in {"", ".", ".."} for part in key.split("/")
    ):
        raise InvalidKeyError()
    return key


@dataclass(frozen=True)
class PutResult:
    size: int
    sha256: str


@dataclass(frozen=True)
class FileStat:
    size: int


@dataclass(frozen=True)
class ProfileInfo:
    id: UUID
    name: str
    backend: str


class ReadStream(Protocol):
    def read(self, size: int = -1) -> bytes: ...
    def close(self) -> None: ...
    def __enter__(self) -> ReadStream: ...
    def __exit__(self, exc_type, exc_value, traceback) -> None: ...


class Storage(Protocol):
    def put(self, key: str, data: bytes | BinaryIO, content_type: str) -> PutResult: ...
    def open(self, key: str) -> ReadStream: ...
    def stat(self, key: str) -> FileStat: ...
    def delete(self, key: str) -> None: ...
    def close(self) -> None: ...


def _read_upload(data: bytes | BinaryIO) -> bytes:
    # The bytes interface is primary. Retain streams for existing resource code;
    # consume from the current position, without seeking or closing the source.
    if not isinstance(data, bytes):
        try:
            data = data.read()
        except OSError:
            raise StorageError("Cannot read upload content") from None
    if not isinstance(data, bytes):
        raise TypeError("Storage uploads must be bytes or a binary read stream")
    return data


class LocalStorage:
    def __init__(self, root: str | Path):
        root = Path(root)
        if not root.is_absolute():
            raise StorageConfigError("Local storage root must be absolute")
        try:
            self.root = root.resolve()
        except (OSError, RuntimeError):
            raise StorageConfigError("Cannot resolve local storage root") from None

    def _path(self, key: str) -> Path:
        validate_key(key)
        try:
            path = self.root
            if path.is_symlink():
                raise InvalidKeyError()
            for part in key.split("/"):
                path = path / part
                if path.is_symlink():
                    raise InvalidKeyError("Storage key must not traverse symbolic links")
            if not path.resolve().is_relative_to(self.root):
                raise InvalidKeyError()
            return path
        except (OSError, RuntimeError):
            raise StorageError() from None

    def put(self, key: str, data: bytes | BinaryIO, content_type: str) -> PutResult:
        path = self._path(key)
        data = _read_upload(data)
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".mmkb-", delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
            temporary.replace(path)
        except OSError:
            raise StorageError() from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Local upload temporary file cleanup failed")
        return PutResult(len(data), hashlib.sha256(data).hexdigest())

    def open(self, key: str) -> ReadStream:
        path = self._path(key)
        try:
            if not stat.S_ISREG(path.stat().st_mode):
                raise StorageError("Storage object must be a regular file")
            return _ReadStream(path.open("rb"))
        except FileNotFoundError:
            raise ObjectNotFound() from None
        except OSError:
            raise StorageError() from None

    def stat(self, key: str) -> FileStat:
        path = self._path(key)
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise StorageError("Storage object must be a regular file")
            return FileStat(info.st_size)
        except FileNotFoundError:
            raise ObjectNotFound() from None
        except OSError:
            raise StorageError() from None

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            raise StorageError() from None

    def close(self) -> None:
        pass


class _ReadStream(AbstractContextManager):
    """Expose read/close while translating IO failures during streaming."""
    def __init__(self, body):
        self.body = body
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("Read from a closed storage stream")
        try:
            return self.body.read() if size < 0 else self.body.read(size)
        except (BotoCoreError, OSError):
            raise StorageError("Cannot read stored object") from None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.body.close()
            except (BotoCoreError, OSError):
                raise StorageError("Cannot close stored object stream") from None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class S3Storage:
    def __init__(self, bucket: str, client):
        if not bucket or not bucket.strip():
            raise StorageConfigError("S3 storage profile requires a bucket")
        self.bucket = bucket
        self.client = client

    def check_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (BotoCoreError, ClientError, OSError):
            raise StorageError("Cannot access S3 bucket; check bucket and credentials") from None

    def _call(self, operation: str, key: str, **kwargs):
        validate_key(key)
        try:
            return getattr(self.client, operation)(Bucket=self.bucket, Key=key, **kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            missing = code == "NoSuchKey"
            if code in {"NotFound", "404"} and operation in {"get_object", "head_object", "delete_object"}:
                # HEAD may return the same generic 404 for a missing bucket.
                # Confirm the bucket exists before interpreting it as a key miss.
                self.check_bucket()
                missing = True
            if missing:
                if operation == "delete_object":
                    return None
                if operation in {"get_object", "head_object"}:
                    raise ObjectNotFound() from None
            logger.warning("S3 operation failed operation=%s error_type=%s", operation, code)
            raise StorageError() from None
        except (BotoCoreError, OSError):
            raise StorageError() from None

    def put(self, key: str, data: bytes | BinaryIO, content_type: str) -> PutResult:
        validate_key(key)
        data = _read_upload(data)
        self._call("put_object", key, Body=data, ContentType=content_type)
        return PutResult(len(data), hashlib.sha256(data).hexdigest())

    def open(self, key: str) -> ReadStream:
        return _ReadStream(self._call("get_object", key)["Body"])

    def stat(self, key: str) -> FileStat:
        return FileStat(self._call("head_object", key)["ContentLength"])

    def delete(self, key: str) -> None:
        self._call("delete_object", key)

    def close(self) -> None:
        self.client.close()


def create_storage(profile: StorageProfile, settings: Settings | None = None) -> Storage:
    """Construct an adapter from the resource's profile, without DB operations."""
    if profile is None:
        raise StorageConfigError("Resource has no storage profile")
    if profile.backend == "local":
        if not profile.local_root:
            raise StorageConfigError("Local storage profile requires an absolute root")
        return LocalStorage(profile.local_root)
    if profile.backend != "s3" or not profile.bucket or not profile.bucket.strip():
        raise StorageConfigError("Invalid storage profile; initialize a valid profile")
    settings = settings or StorageConfig()
    if not settings.s3_region or settings.s3_addressing_style not in {"auto", "path", "virtual"}:
        raise StorageConfigError("Set a valid S3_REGION and S3_ADDRESSING_STYLE")
    if settings.s3_signature_version != "s3v4":
        raise StorageConfigError("S3_SIGNATURE_VERSION must be s3v4")
    if profile.endpoint_url:
        try:
            url = urlsplit(profile.endpoint_url)
            valid = (url.scheme in {"http", "https"} and url.hostname and not url.username
                     and not url.password and not url.query and not url.fragment)
            url.port  # Validate port syntax before the SDK is constructed.
        except ValueError:
            valid = False
        if not valid:
            raise StorageConfigError("S3 endpoint must be an HTTP(S) URL without credentials, query or fragment")
    try:
        session = boto3.Session(profile_name=profile.credential_ref)
        client = session.client(
            "s3", endpoint_url=profile.endpoint_url,
            region_name=settings.s3_region,
            config=Config(
                signature_version=settings.s3_signature_version,
                s3={"addressing_style": settings.s3_addressing_style},
                connect_timeout=5, read_timeout=30,
                retries={"mode": "standard", "total_max_attempts": 3},
            ),
        )
    except (BotoCoreError, ValueError):
        raise StorageConfigError("Cannot configure S3 client; check credentials and endpoint") from None
    return S3Storage(profile.bucket, client)


class StorageResolver:
    """Reuse adapters in existing API/worker entrypoints; close on shutdown."""
    def __init__(self, config: Settings):
        self.config = config
        self._adapters: dict[tuple, Storage] = {}
        self._lock = Lock()

    def __call__(self, profile: StorageProfile) -> Storage:
        if profile is None:
            raise StorageConfigError("Resource has no storage profile")
        identity = (profile.backend, profile.local_root, profile.endpoint_url, profile.bucket, profile.credential_ref)
        with self._lock:
            if identity not in self._adapters:
                self._adapters[identity] = create_storage(profile, self.config)
            return self._adapters[identity]

    def close(self) -> None:
        with self._lock:
            for storage in self._adapters.values():
                storage.close()
            self._adapters.clear()


def initialize_profile(sessions, config: Settings, resolver: StorageResolver) -> ProfileInfo:
    if config.storage_backend not in {"local", "s3"} or not config.storage_profile_name.strip():
        raise StorageConfigError("Select a local/s3 backend and a non-empty profile name")
    root = None
    if config.storage_backend == "local":
        if not config.local_storage_root.strip():
            raise StorageConfigError("Set LOCAL_STORAGE_ROOT")
        configured = Path(config.local_storage_root)
        try:
            root = str((PROJECT_ROOT / configured).resolve())
        except (OSError, RuntimeError):
            raise StorageConfigError("Cannot resolve local storage root") from None
    fields = dict(
        backend=config.storage_backend, local_root=root,
        endpoint_url=config.s3_endpoint_url if config.storage_backend == "s3" else None,
        bucket=config.s3_bucket if config.storage_backend == "s3" else None,
        credential_ref=config.s3_credential_profile if config.storage_backend == "s3" else None,
    )
    candidate = StorageProfile(id=uuid4(), name=config.storage_profile_name, **fields)
    storage = resolver(candidate)
    # Verify connectivity before opening the DB transaction. Never create a bucket.
    if isinstance(storage, LocalStorage):
        try:
            storage.root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=storage.root) as probe:
                probe.write(b"mmkb storage probe")
                probe.flush()
        except OSError:
            raise StorageError("Cannot write local storage root; check path and permissions") from None
    else:
        storage.check_bucket()
    with sessions.begin() as session:
        existing = session.scalar(sa.select(StorageProfile).where(StorageProfile.name == candidate.name))
        if existing is not None:
            if any(getattr(existing, key) != value for key, value in fields.items()):
                raise ResourceError("profile_conflict", "Existing profile differs; use a new STORAGE_PROFILE_NAME", 409)
            candidate = existing
        else:
            session.add(candidate)
        info = ProfileInfo(candidate.id, candidate.name, candidate.backend)
    return info


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init"])
    parser.parse_args()
    sessions = resolver = None
    try:
        config = load_storage_config()
        # Storage loading itself has no database dependency.
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise StorageConfigError("Set DATABASE_URL in the environment or .env.local")
        sessions = create_session_factory(database_url)
        resolver = StorageResolver(config)
        info = initialize_profile(sessions, config, resolver)
        print(f"Storage profile ready: id={info.id} name={info.name} backend={info.backend}")
    except ResourceError as exc:
        parser.exit(1, f"{exc.code}: {exc.message}\n")
    except sa.exc.SQLAlchemyError:
        parser.exit(1, "database_unavailable: Check database connection and migrations\n")
    except ValueError:
        parser.exit(1, "storage_config: Check storage environment configuration\n")
    finally:
        if resolver is not None:
            resolver.close()
        if sessions is not None:
            sessions.kw["bind"].dispose()


if __name__ == "__main__":
    main()
