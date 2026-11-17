"""Read-only probes against a real PostgreSQL and, when configured, a real S3 endpoint.

These cases exercise what the offline fakes cannot: driver error classes, the real Alembic version
table, catalog introspection, permissions and HeadBucket. Destructive structure cases run in a
disposable ``mmkb_test_*`` database, never in the configured test database.
"""
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from mmkb.health_contract import Check

from mmkb.config import resolve_health_config, resolve_storage_config
from mmkb.db.models import StorageProfile
from mmkb.health import HealthBudgets
from mmkb.health_probes import ProfileRow, probe_database, probe_storage, run_round
from mmkb.schema_contract import VERSION_COLUMN, VERSION_TABLE
from mmkb.storage import StorageResolver, initialize_profile

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {"EMBEDDING_BASE_URL": "http://127.0.0.1:1/v1", "EMBEDDING_MODEL": "embed-model"}


def rendered(url) -> str:
    """Keep the real password: ``str(URL)`` masks it, which breaks every later connection."""
    return url.render_as_string(hide_password=False)


def url_of(database: str) -> str:
    return rendered(sa.engine.make_url(os.environ["MMKB_TEST_DATABASE_URL"]).set(database=database))


def test_scratch_urls_keep_the_configured_password():
    """``str(URL)`` masks the password; a masked DSN would fail right after the copy is created."""
    sample = sa.engine.make_url("postgresql://mmkb:test%40pass@127.0.0.1:5432/mmkb_test_probe")
    assert "***" in str(sample)
    assert "test%40pass" in rendered(sample)
    assert "***" not in rendered(sample)
    configured = os.environ.get("MMKB_TEST_DATABASE_URL")
    if configured and "@" in configured:
        assert "***" not in url_of("mmkb_test_probe")


@pytest.fixture(scope="module")
def migrated_url():
    url = os.environ.get("MMKB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set MMKB_TEST_DATABASE_URL to a migrated PostgreSQL test database")
    engine = sa.create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            assert connection.execute(sa.text(
                "SELECT to_regclass('mmkb.resource_indexes')")).scalar() is not None
    finally:
        engine.dispose()
    return url


@pytest.fixture
def scratch_url(migrated_url):
    """A disposable migrated copy: structure damage here can never touch the shared test data."""
    database = f"mmkb_test_health_{uuid4().hex[:8]}"
    admin = sa.create_engine(migrated_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database}" TEMPLATE template0'))
    except sa.exc.SQLAlchemyError as error:
        pytest.skip(f"cannot create a disposable database for structure checks: {type(error).__name__}")
    finally:
        admin.dispose()
    url = url_of(database)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        # The documented setup pre-installs pgvector in public before migrations run.
        extension_engine = sa.create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
        try:
            with extension_engine.connect() as connection:
                connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        finally:
            extension_engine.dispose()
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        yield url
    finally:
        # Whatever failed above, the copy is dropped: a leaked scratch database is worse than a red test.
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        admin = sa.create_engine(migrated_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
        try:
            with admin.connect() as connection:
                connection.execute(sa.text(f'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                                           f'WHERE datname = \'{database}\' AND pid <> pg_backend_pid()'))
                connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database}"'))
        finally:
            admin.dispose()


def snapshot(url, **env):
    return resolve_health_config(dict(BASE_ENV, DATABASE_URL=url or "", **env))


def probe(url, **env):
    return probe_database(snapshot(url, **env), HealthBudgets())


def test_probe_confirms_a_migrated_database_and_reads_the_current_profile(migrated_url, tmp_path):
    check, row = probe(migrated_url, STORAGE_PROFILE_NAME="mmkb-health-absent")
    assert (check.state, check.code, check.scope) == ("connected", "database_ready",
                                                      "connection_and_schema")
    assert check.latency_ms is not None
    assert row is None, "a profile name that does not exist must not be invented"


def test_probe_reports_an_unmigrated_database(scratch_url):
    # The migration was applied by the fixture; drop the version rows to model an unmanaged schema.
    engine = sa.create_engine(scratch_url, hide_parameters=True)
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM public.mmkb_alembic_version"))
    engine.dispose()
    assert probe(scratch_url)[0].code == "schema_missing"


@pytest.mark.parametrize("revision,code", [
    # Revisions outside the packaged history are never ordered by string or numeric comparison.
    ("0000_bootstrap", "schema_incompatible"),
    ("9999_future_schema", "schema_incompatible"),
])
def test_probe_compares_revisions_against_the_packaged_contract(scratch_url, revision, code):
    engine = sa.create_engine(scratch_url, hide_parameters=True)
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM public.mmkb_alembic_version"))
        connection.execute(sa.text("INSERT INTO public.mmkb_alembic_version (version_num) "
                                   "VALUES (:revision)"), {"revision": revision})
    engine.dispose()
    assert probe(scratch_url)[0].code == code


def test_probe_calls_a_known_earlier_revision_outdated(scratch_url, monkeypatch):
    """A future release adds a revision; an older applied revision must read as behind, not incompatible."""
    import mmkb.health_probes as probes

    engine = sa.create_engine(scratch_url, hide_parameters=True)
    with engine.connect() as connection:
        applied = connection.scalar(sa.text(f"SELECT {VERSION_COLUMN} FROM {VERSION_TABLE}"))
    engine.dispose()
    # Same history the database came from, plus the revision a later release would add.
    monkeypatch.setattr(probes, "EXPECTED_REVISIONS", (applied, "9999_follow_up"))
    monkeypatch.setattr(probes, "EXPECTED_HEAD", "9999_follow_up")
    assert probe(scratch_url)[0].code == "schema_outdated"


def test_probe_reports_multiple_heads_as_incompatible(scratch_url):
    engine = sa.create_engine(scratch_url, hide_parameters=True)
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO public.mmkb_alembic_version (version_num) "
                                   "VALUES ('another_head')"))
    engine.dispose()
    assert probe(scratch_url)[0].code == "schema_incompatible"


@pytest.mark.parametrize("statement,code", [
    ("DROP TABLE mmkb.resources CASCADE", "schema_missing"),
    # The stored dimension is what retrieval depends on; a silent change must be visible.
    ("ALTER TABLE mmkb.resource_indexes ALTER COLUMN embedding TYPE halfvec(1024)", "schema_incompatible"),
    ("DROP INDEX mmkb.resource_embedding_hnsw", "database_ready"),
])
def test_probe_detects_structure_drift_without_repairing_it(scratch_url, statement, code):
    engine = sa.create_engine(scratch_url, hide_parameters=True)
    with engine.begin() as connection:
        connection.execute(sa.text(statement))
    engine.dispose()
    assert probe(scratch_url)[0].code == code


def test_probe_never_repairs_a_damaged_database(scratch_url):
    engine = sa.create_engine(scratch_url, hide_parameters=True)
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE mmkb.storage_profiles CASCADE"))
    engine.dispose()
    before = probe(scratch_url)[0].code
    again = probe(scratch_url)[0].code
    assert (before, again) == ("schema_missing", "schema_missing")


def test_probe_separates_authentication_and_connection_failures(migrated_url):
    wrong_password = rendered(sa.engine.make_url(migrated_url).set(password=f"wrong-{uuid4().hex}"))
    code = probe(wrong_password)[0].code
    if code == "database_ready":
        # A server configured for trust or peer authentication cannot demonstrate auth failures.
        pytest.skip("the test server accepts connections without a password")
    assert code == "database_auth_failed"
    assert probe("postgresql://mmkb:mmkb@127.0.0.1:1/mmkb_missing")[0].code == "database_unreachable"
    assert probe("not-a-dsn")[0].code == "invalid_config"


def test_probe_reports_missing_read_privileges(migrated_url):
    role = f"mmkb_health_ro_{uuid4().hex[:8]}"
    engine = sa.create_engine(migrated_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    password = f"probe-{uuid4().hex}"
    try:
        with engine.connect() as connection:
            if not connection.execute(sa.text(
                    "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar():
                pytest.skip("the permission case needs a superuser test connection")
            connection.execute(sa.text(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\''))
            connection.execute(sa.text(f'GRANT USAGE ON SCHEMA mmkb, public TO "{role}"'))
            connection.execute(sa.text(f'GRANT SELECT ON TABLE mmkb.storage_profiles TO "{role}"'))
        url = rendered(sa.engine.make_url(migrated_url).set(username=role, password=password))
        # storage_profiles is readable but resources are not: the probe must name the permission gap.
        assert probe(url)[0].code == "database_permission_denied"
    except sa.exc.SQLAlchemyError as error:
        pytest.skip(f"cannot create a read-only role: {type(error).__name__}")
    finally:
        with engine.connect() as connection:
            connection.execute(sa.text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                       "WHERE usename = :role"), {"role": role})
            try:
                connection.execute(sa.text(f'DROP ROLE IF EXISTS "{role}"'))
            except sa.exc.SQLAlchemyError:
                pass  # Cleanup only: the next run uses a fresh role name.
        engine.dispose()


def registered(tmp_path, url, *, name="mmkb-health", root=None):
    env = {"STORAGE_BACKEND": "local", "STORAGE_PROFILE_NAME": name,
           "LOCAL_STORAGE_ROOT": str(root or tmp_path)}
    config = resolve_storage_config(env)
    sessions = sessionmaker(sa.create_engine(url, hide_parameters=True))
    resolver = StorageResolver(config)
    try:
        initialize_profile(sessions, config, resolver)
    finally:
        resolver.close()
        sessions.kw["bind"].dispose()
    return snapshot(url, **env)


def test_storage_follows_the_registered_profile_and_flags_conflicts(scratch_url, tmp_path):
    usable = tmp_path / "usable"
    (usable / "nested").mkdir(parents=True)
    config = registered(tmp_path, scratch_url, root=usable)
    database, row = probe_database(config, HealthBudgets())
    assert (database.code, row.backend) == ("database_ready", "local")
    assert probe_storage(database, row, config, HealthBudgets()).code == "local_directory_accessible"

    moved = snapshot(scratch_url, STORAGE_BACKEND="local", STORAGE_PROFILE_NAME="mmkb-health",
                     LOCAL_STORAGE_ROOT=str(tmp_path / "elsewhere"))
    assert probe_storage(database, row, moved, HealthBudgets()).code == "profile_conflict"

    engine = sa.create_engine(scratch_url, hide_parameters=True)
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM mmkb.storage_profiles WHERE name = :name"),
                           {"name": "mmkb-health"})
    engine.dispose()
    assert probe_storage(database, None, config, HealthBudgets()).code == "profile_not_initialized"


def test_local_probe_names_denial_without_creating_anything(scratch_url, tmp_path):
    if hasattr(__import__("os"), "geteuid") and __import__("os").geteuid() == 0:
        pytest.skip("a root account ignores directory permission bits")
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o000)
    try:
        config = snapshot(scratch_url, STORAGE_BACKEND="local", STORAGE_PROFILE_NAME="locked-profile",
                          LOCAL_STORAGE_ROOT=str(locked))
        sessions = sessionmaker(sa.create_engine(scratch_url, hide_parameters=True))
        # The row is written directly: initialize_profile would create the directory it cannot use.
        with sessions.begin() as session:
            session.add(StorageProfile(id=uuid4(), name="locked-profile", backend="local",
                                       local_root=str(locked)))
        database, row = probe_database(config, HealthBudgets())
        assert probe_storage(database, row, config, HealthBudgets()).code == "storage_access_denied"
        assert list(tmp_path.iterdir()) == [locked], "probing must not create files or directories"
    finally:
        locked.chmod(0o755)


def test_a_full_probe_round_does_not_change_any_business_rows(migrated_url, tmp_path):
    config = registered(tmp_path, migrated_url, name=f"mmkb-health-{uuid4().hex[:6]}")
    engine = sa.create_engine(migrated_url, hide_parameters=True)

    def counts():
        with engine.connect() as connection:
            return tuple(connection.execute(sa.text(
                "SELECT (SELECT count(*) FROM mmkb.resources), "
                "(SELECT count(*) FROM mmkb.resource_indexes), "
                "(SELECT count(*) FROM mmkb.storage_profiles)")).one())

    def objects():
        output = subprocess.run(["find", str(tmp_path), "-type", "f"], capture_output=True, text=True,
                                timeout=20)
        return sorted(output.stdout.splitlines())

    before, files_before = counts(), objects()
    emitted = {}
    complete = run_round(config, HealthBudgets(), lambda key, check: emitted.__setitem__(key, check),
                         transport=None)
    assert complete and set(emitted) >= {"database", "storage", "embedding", "vision", "decision"}
    assert emitted["embedding"].code in ("model_unreachable", "model_timeout")
    assert counts() == before
    assert objects() == files_before, "a health round must not add or remove stored objects"

    engine.dispose()


@pytest.mark.parametrize("bucket,code", [(None, "bucket_accessible"), ("mmkb-missing-bucket",
                                                                          "bucket_unavailable")])
def test_head_bucket_probe_against_a_real_endpoint(bucket, code):
    endpoint, name = os.environ.get("MMKB_TEST_S3_ENDPOINT_URL"), os.environ.get("MMKB_TEST_S3_BUCKET")
    if not endpoint or not name:
        pytest.skip("Set MMKB_TEST_S3_ENDPOINT_URL and MMKB_TEST_S3_BUCKET for S3 checks")
    env = {"STORAGE_BACKEND": "s3", "STORAGE_PROFILE_NAME": "mmkb-health-s3", "S3_ENDPOINT_URL": endpoint,
           "S3_BUCKET": bucket or name, "S3_REGION": os.environ.get("MMKB_TEST_S3_REGION", "us-east-1")}
    if bucket:
        env["S3_CREDENTIAL_PROFILE"] = os.environ.get("MMKB_TEST_S3_CREDENTIAL_PROFILE", "")
    storage = resolve_storage_config(env)
    config = resolve_health_config(dict(BASE_ENV, **env))
    row = ProfileRow("s3", None, endpoint, storage.s3_bucket, storage.s3_credential_profile)
    report = probe_storage(Check("connected", "database_ready", 1), row, config, HealthBudgets())
    assert (report.code, report.backend) == (code, "s3")
