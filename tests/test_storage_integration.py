"""Opt-in checks against an explicitly selected test database and S3 bucket."""
import os
import subprocess
import sys
from uuid import uuid4

import pytest
import sqlalchemy as sa

from mmkb.config import StorageConfig
from mmkb.db.models import StorageProfile
from mmkb.storage import S3Storage, create_storage

pytestmark = pytest.mark.integration


@pytest.fixture
def real_s3():
    endpoint = os.environ.get("MMKB_TEST_S3_ENDPOINT_URL")
    bucket = os.environ.get("MMKB_TEST_S3_BUCKET")
    if not endpoint or not bucket:
        pytest.skip("Set MMKB_TEST_S3_ENDPOINT_URL and MMKB_TEST_S3_BUCKET for RustFS/S3 contract tests")
    # No implicit fallback to the application's bucket or endpoint.
    settings = StorageConfig(s3_region=os.environ.get("MMKB_TEST_S3_REGION", "us-east-1"))
    profile = StorageProfile(
        backend="s3", endpoint_url=endpoint, bucket=bucket,
        credential_ref=os.environ.get("MMKB_TEST_S3_CREDENTIAL_PROFILE") or None,
    )
    storage = create_storage(profile, settings)
    try:
        assert isinstance(storage, S3Storage)
        storage.check_bucket()
        versioning = storage.client.get_bucket_versioning(Bucket=bucket)
        if versioning.get("Status"):
            pytest.fail("Use a test bucket that has never enabled versioning")
        yield storage
    finally:
        storage.close()


def test_rustfs_storage_contract(real_s3, storage_contract):
    storage_contract(real_s3, f"mmkb-tests/{uuid4().hex}")


def test_rustfs_deleting_one_key_preserves_other_objects(real_s3):
    prefix = f"mmkb-tests/{uuid4().hex}"
    keys = [f"{prefix}/first/original", f"{prefix}/second/original"]
    try:
        for key, data in zip(keys, (b"first", b"second")):
            real_s3.put(key, data, "application/octet-stream")
        real_s3.delete(keys[0])
        with real_s3.open(keys[1]) as stream:
            assert stream.read() == b"second"
    finally:
        for key in keys:
            real_s3.delete(key)


def test_postgres_initialization_command_creates_reuses_and_rejects_conflicts(tmp_path):
    url = os.environ.get("MMKB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set MMKB_TEST_DATABASE_URL to a migrated PostgreSQL test database")
    if not url.startswith(("postgresql:", "postgresql+")):
        pytest.fail("MMKB_TEST_DATABASE_URL must select PostgreSQL")
    name = f"storage-test-{uuid4().hex}"
    env = dict(os.environ, DATABASE_URL=url, STORAGE_BACKEND="local", STORAGE_PROFILE_NAME=name,
               LOCAL_STORAGE_ROOT=str(tmp_path / "original"))
    engine = sa.create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            connection.execute(sa.select(StorageProfile.id).limit(0))
        def run():
            return subprocess.run([sys.executable, "-m", "mmkb.storage", "init"], env=env,
                                  capture_output=True, text=True, timeout=30)
        first = run()
        assert first.returncode == 0, first.stderr
        assert f"name={name}" in first.stdout and "backend=local" in first.stdout
        second = run()
        assert second.returncode == 0 and second.stdout == first.stdout
        env["LOCAL_STORAGE_ROOT"] = str(tmp_path / "different")
        conflict = run()
        assert conflict.returncode == 1 and "profile_conflict" in conflict.stderr
        with engine.connect() as connection:
            roots = connection.scalars(sa.select(StorageProfile.local_root).where(StorageProfile.name == name)).all()
            assert roots == [str(tmp_path / "original")]
    finally:
        with engine.begin() as connection:
            connection.execute(sa.delete(StorageProfile).where(StorageProfile.name == name))
        engine.dispose()
