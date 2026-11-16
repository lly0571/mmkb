"""Shared storage contract, usable against local files and a real S3 service."""
import hashlib

import pytest

from mmkb.errors import ObjectNotFoundError


@pytest.fixture
def storage_contract():
    def check(storage, prefix):
        key = f"{prefix}/original.bin"
        try:
            for operation in (storage.open, storage.stat):
                with pytest.raises(ObjectNotFoundError):
                    operation(key)
            storage.delete(key)
            for data in (b"\x00\xffbinary\n" * 1000, b"replacement", b""):
                result = storage.put(key, data, content_type="application/octet-stream")
                assert result.size == len(data)
                assert result.sha256 == hashlib.sha256(data).hexdigest()
                assert storage.stat(key).size == len(data)
                with storage.open(key) as stream:
                    assert stream.read(3) + stream.read() == data
                assert stream.closed
            with pytest.raises(RuntimeError):
                with storage.open(key) as stream:
                    raise RuntimeError("consumer failed")
            assert stream.closed
            storage.delete(key)
            storage.delete(key)
            for operation in (storage.open, storage.stat):
                with pytest.raises(ObjectNotFoundError):
                    operation(key)
        finally:
            storage.delete(key)
    return check
