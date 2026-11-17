"""Probe behaviour: model lists, storage profiles and database structure, without live services."""
from contextlib import nullcontext
import json
import os
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from mmkb.config import resolve_health_config, resolve_service_probe, resolve_storage_config
from mmkb.health import HealthBudgets
from mmkb.health_contract import CHECK_DATABASE, Check, check_payload, check_result
from mmkb.health_probes import (
    ProfileRow, classify_model_status, classify_revisions, classify_s3_error, database_code_for,
    inspect_database, probe_database, probe_local_directory, probe_model, probe_storage, probe_s3_bucket, run_round,
)
from mmkb.config import HealthConfig
from mmkb.schema_contract import EXPECTED_HEAD
from mmkb.storage import StorageConfig

BASE = {"DATABASE_URL": "postgresql://user:secret@db.internal:5432/mmkb",
        "EMBEDDING_BASE_URL": "http://model.internal:8000/v1", "EMBEDDING_MODEL": "embed-model"}


def health_config(**overrides):
    env = dict(BASE, **{name: ("" if value is None else str(value)) for name, value in overrides.items()})
    return resolve_health_config(env)


MODELS = {"embedding": "embed-model", "vision": "vision-model", "decision": "decision-model"}
PREFIXES = {"embedding": "EMBEDDING", "vision": "CAPTION", "decision": "DECISION"}


def service_probe(key, *, configured=True, **overrides):
    """One health service probe; ``configured=False`` clears both service variables."""
    prefix = PREFIXES[key]
    env = dict(BASE, **{f"{prefix}_BASE_URL": "http://optional.internal:8001/v1" if configured else "",
                        f"{prefix}_MODEL": MODELS[key] if configured else ""})
    env.update({name: ("" if value is None else str(value)) for name, value in overrides.items()})
    return resolve_service_probe(key, prefix, required=key == "embedding", default_timeout=60.0, env=env)


class Recorder:
    def __init__(self, response):
        self.response = response
        self.requests: list[httpx.Request] = []

    def transport(self):
        def handler(request):
            self.requests.append(request)
            return self.response(request) if callable(self.response) else self.response
        return httpx.MockTransport(handler)

    @property
    def methods(self):
        return [(request.method, request.url.path) for request in self.requests]


def model_probe(key="embedding", **overrides):
    return probe_model(service_probe(key), HealthBudgets(), **overrides)


def test_model_list_requires_an_exact_id_and_never_calls_inference():
    recorder = Recorder(httpx.Response(200, json={"data": [{"id": "other-model"}, {"id": "embed-model"}]}))
    check = model_probe(transport=recorder.transport())
    assert (check.state, check.code, check.scope) == ("connected", "model_list_verified", "model_list")
    assert check.latency_ms is not None
    assert recorder.methods == [("GET", "/v1/models")], "probing must not POST embeddings or chat"


@pytest.mark.parametrize("payload,code", [
    ({"data": [{"id": "other"}]}, "model_not_found"),
    ({"data": "not-a-list"}, "model_invalid_response"),
    ({"data": [{"name": "embed-model"}]}, "model_invalid_response"),
    ({}, "model_invalid_response"),
    ({"data": [{"id": "embed-model"}, 7]}, "model_invalid_response"),
])
def test_model_list_payloads_are_validated(payload, code):
    recorder = Recorder(httpx.Response(200, json=payload))
    assert model_probe(transport=recorder.transport()).code == code


@pytest.mark.parametrize("status,code", [(200, None), (401, "model_auth_failed"), (403, "model_auth_failed"),
                                         (404, "model_http_error"), (405, "model_http_error"),
                                         (500, "model_http_error"), (501, "model_http_error"),
                                         (302, "model_http_error"), (307, "model_http_error")])
def test_http_statuses_are_named_and_redirects_are_not_followed(status, code):
    if status == 200:
        pytest.skip("covered by the exact-id test")
    recorder = Recorder(httpx.Response(status, headers={"location": "http://elsewhere.internal/v1/models"},
                                       json={"data": [{"id": "embed-model"}]}))
    check = model_probe(transport=recorder.transport())
    assert check.code == code
    assert check.state == "down"
    assert len(recorder.requests) == 1, "credentials must not be replayed to a redirect target"


def test_malformed_json_is_not_a_success():
    recorder = Recorder(httpx.Response(200, content=b"<html>proxy page</html>"))
    assert model_probe(transport=recorder.transport()).code == "model_invalid_response"


def test_oversized_model_list_is_rejected_before_parsing():
    body = b'{"data": [{"id": "embed-model", "junk": "' + b"x" * 4096 + b'"}]}'
    recorder = Recorder(httpx.Response(200, content=body))
    budgets = HealthBudgets(model_response_limit_bytes=512)
    check = probe_model(service_probe("embedding"), budgets, transport=recorder.transport())
    assert check.code == "model_invalid_response"


def test_drip_fed_model_list_hits_the_total_budget_not_just_a_read_timeout():
    def slow_stream(chunk):
        for _ in range(200):
            time.sleep(0.01)
            yield chunk

    recorder = Recorder(httpx.Response(200, content=slow_stream(b'{"data": [{"id": "embed-model"}'),
                                       headers={"content-type": "application/json"}))
    started = time.monotonic()
    check = probe_model(service_probe("embedding"), HealthBudgets(model_seconds=0.15, model_read_seconds=0.1),
                        transport=recorder.transport())
    assert (check.state, check.code) == ("unknown", "check_deadline_exceeded")
    assert time.monotonic() - started < 1.0


def test_connection_failure_and_timeout_codes():
    recorder = Recorder(httpx.Response(200, json={"data": [{"id": "embed-model"}]}))
    check = probe_model(service_probe("embedding"), HealthBudgets(model_connect_seconds=0.001,
                                                                 model_seconds=0.01),
                        transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(
                            httpx.ConnectError("connection refused"))))
    assert check.code == "model_unreachable"
    assert recorder.methods == []


def test_optional_services_report_their_configuration_without_network_calls():
    recorder = Recorder(httpx.Response(200, json={"data": []}))
    for action, expected in [("disabled", "service_disabled"), ("invalid", "invalid_config")]:
        probe = service_probe("vision", CAPTION_ENABLED="false" if action == "disabled" else "maybe")
        check = probe_model(probe, HealthBudgets(), transport=recorder.transport())
        assert check.code == expected, action
    assert recorder.methods == []


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class FakeConnection:
    """Stands in for one read-only SQLAlchemy connection with scripted answers."""

    def __init__(self, *, version_rows=(EXPECTED_HEAD,), extension=True, embedding_type="halfvec(2048)",
                 profile=None):
        self.version_rows = version_rows
        self.extension = extension
        self.embedding_type = embedding_type
        self.profile = profile
        self.statements: list[str] = []

    def scalar(self, statement, params=None):
        self.statements.append(str(statement))
        if "to_regclass" in str(statement):
            return bool(self.version_rows)
        raise AssertionError("unexpected scalar statement")

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "SELECT 1" in sql and "FROM" not in sql:
            return FakeResult([(1,)])
        if "mmkb_alembic_version ORDER BY" in sql:
            return FakeResult([(value,) for value in self.version_rows])
        if "pg_extension" in sql:
            return FakeResult([(1,)] if self.extension else [])
        if "format_type" in sql:
            return FakeResult([(self.embedding_type,)] if self.embedding_type else [])
        if "FROM mmkb.storage_profiles" in sql:
            return FakeResult([self.profile] if self.profile else [])
        if sql.startswith("SELECT 1 FROM mmkb."):
            return FakeResult([])
        raise AssertionError(f"unexpected statement: {sql}")


PROFIT_ROW = ("local", "/data/assets", None, None, None)


def test_database_structure_checks_are_reported_as_code_pairs():
    config = resolve_storage_config({"STORAGE_BACKEND": "local", "LOCAL_STORAGE_ROOT": "/data/assets"})
    snapshot = HealthConfig(database_url="postgresql://x", storage=config, services=())
    code, row = inspect_database(FakeConnection(profile=PROFIT_ROW), snapshot)
    assert code == "database_ready"
    assert row == ProfileRow("local", "/data/assets", None, None, None)

    cases = [
        ({"version_rows": ()}, "schema_missing"),
        ({"version_rows": ("0001_initial_schema", "0002_next")}, "schema_incompatible"),
        ({"version_rows": ("0000_bootstrap",)}, "schema_incompatible"),
        ({"version_rows": ("9999_future",)}, "schema_incompatible"),
        ({"extension": False}, "schema_incompatible"),
        ({"embedding_type": "halfvec(1024)"}, "schema_incompatible"),
        ({"embedding_type": "vector(2048)"}, "schema_incompatible"),
        ({"embedding_type": None}, "schema_incompatible"),
    ]
    for kwargs, expected in cases:
        connection = FakeConnection(**kwargs)
        assert inspect_database(connection, snapshot)[0] == expected, kwargs
        reads = [sql for sql in connection.statements if "FROM mmkb." in sql]
        if "version_rows" in kwargs:
            # An unusable migration version stops the round before any business table is touched.
            assert reads == [], kwargs
        # Every table access check selects zero rows: no resources, captions or vectors are read.
        assert all("WHERE false" in sql for sql in reads)


def test_version_table_is_read_from_the_packaged_contract_only():
    assert classify_revisions(()) == "schema_missing"
    assert classify_revisions((EXPECTED_HEAD,)) is None
    assert classify_revisions(("0001_initial_schema", "other")) == "schema_incompatible"
    # Numeric-looking identifiers are never compared as a sequence.
    assert classify_revisions(("9",)) == "schema_incompatible"
    assert classify_revisions(("0000_older",)) == "schema_incompatible"


def test_current_profile_row_is_read_by_name_and_stays_internal():
    snapshot = HealthConfig(database_url="postgresql://x",
                            storage=resolve_storage_config({"STORAGE_BACKEND": "s3", "S3_BUCKET": "b",
                                                            "S3_ENDPOINT_URL": "http://s3.internal"}),
                            services=())
    row = ("s3", None, "http://s3.internal", "b", "rustfs")
    code, profile = inspect_database(FakeConnection(profile=row), snapshot)
    assert code == "database_ready"
    assert profile == ProfileRow("s3", None, "http://s3.internal", "b", "rustfs")
    assert inspect_database(FakeConnection(), snapshot)[1] is None


class _DriverError(Exception):
    """psycopg2 keeps connect-phase wording in args and leaves pgcode unset."""


def _connect_failure(text):
    error = _DriverError(b"connection to server failed")
    error.orig = _DriverError(text.encode("utf-8"))
    return error


@pytest.mark.parametrize("text,code", [
    ('FATAL: password authentication failed for user "mmkb"', "database_auth_failed"),
    ("connection to server failed: no password supplied", "database_auth_failed"),
    ('FATAL: peer authentication failed for user "alice"', "database_auth_failed"),
    ('FATAL: pam authentication failed for user "mmkb"', "database_auth_failed"),
    ('FATAL: permission denied for database "mmkb"', "database_permission_denied"),
    ("could not connect to server: Connection refused", "database_unreachable"),
    ("server closed the connection unexpectedly", "database_unreachable"),
])
def test_connect_phase_failures_are_classified_without_a_pgcode(text, code):
    assert database_code_for(_connect_failure(text)) == code


def test_connect_phase_classification_never_repeats_the_driver_text():
    secret = "mmkb_owner / hunter2 / postgres://u:p@db.internal:5432/kb"
    error = _connect_failure(f'FATAL: password authentication failed for user "{secret}"')
    code = database_code_for(error)
    payload = check_payload(CHECK_DATABASE, check_result(CHECK_DATABASE, code, latency_ms=3))
    assert code == "database_auth_failed"
    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert payload["message"] == "数据库认证失败，请检查服务端账号配置"


def test_a_wrong_password_during_connect_is_an_auth_failure_not_a_mystery(monkeypatch):
    """The default psycopg2 driver reports a bad password with no pgcode at all."""
    import sqlalchemy as sa

    from mmkb.health_probes import probe_database

    class FailingEngine:
        def connect(self):
            orig = _DriverError(b'FATAL:  password authentication failed for user "mmkb_owner"\n')
            raise sa.exc.OperationalError("SELECT 1", {}, orig)

        def dispose(self):
            return None

    monkeypatch.setattr(sa, "create_engine", lambda *args, **kwargs: FailingEngine())
    check, row = probe_database(resolve_health_config(BASE), HealthBudgets())
    assert (check.state, check.code, check.scope, row) == ("down", "database_auth_failed", "connection", None)
    assert "mmkb_owner" not in json.dumps(check_payload(CHECK_DATABASE, check), ensure_ascii=False)


@pytest.mark.parametrize("pgcode,code", [("28P01", "database_auth_failed"), ("28000", "database_auth_failed"),
                                         ("42501", "database_permission_denied"), ("42P01", "schema_missing"),
                                         ("3F000", "schema_missing"), ("57014", "database_unreachable"),
                                         ("", "database_unreachable")])
def test_sqlstates_map_to_fixed_database_codes(pgcode, code):
    error = type("Wrapped", (Exception,), {})()
    error.orig = SimpleNamespace(pgcode=pgcode or None, sqlstate=None)
    assert database_code_for(error) == code


def test_local_directory_probe_never_creates_anything(tmp_path):
    root = tmp_path / "assets"
    root.mkdir()
    (root / "a.png").write_bytes(b"png")
    assert probe_local_directory(str(root)) == "local_directory_accessible"
    assert probe_local_directory(str(tmp_path / "missing")) == "local_root_unavailable"
    assert probe_local_directory(str(root / "a.png")) == "local_root_unavailable"
    assert probe_local_directory(None) == "local_root_unavailable"
    assert probe_local_directory("") == "local_root_unavailable"
    assert list(root.iterdir()) == [root / "a.png"], "probing must not add or remove files"


@pytest.mark.skipif(os.geteuid() == 0, reason="a root account ignores directory permission bits")
def test_local_directory_probe_reports_denial_without_retrying(tmp_path):
    import os

    root = tmp_path / "locked"
    root.mkdir(mode=0o000)
    try:
        assert probe_local_directory(str(root)) == "storage_access_denied"
    finally:
        root.chmod(0o755)


def s3_client(error=None, calls=None):
    def head_bucket(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if error is not None:
            raise error
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}
    return SimpleNamespace(head_bucket=head_bucket, close=lambda: None)


def client_error(code):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code}}, "HeadBucket")


@pytest.mark.parametrize("error,code", [
    (client_error("AccessDenied"), "storage_access_denied"),
    (client_error("403"), "storage_access_denied"),
    (client_error("NoSuchBucket"), "bucket_unavailable"),
    (client_error("NotFound"), "bucket_unavailable"),
    (client_error("404"), "bucket_unavailable"),
    (client_error("InternalError"), "bucket_unavailable"),
    (TimeoutError("read timed out"), "storage_timeout"),
    (OSError("connection reset"), "bucket_unavailable"),
])
def test_head_bucket_failures_use_the_storage_vocabulary(error, code):
    from botocore.exceptions import BotoCoreError

    if isinstance(error, OSError) and not isinstance(error, TimeoutError):
        error = BotoCoreError()
    settings = StorageConfig(storage_backend="s3", s3_bucket="bucket")
    row = ProfileRow("s3", None, "http://s3.internal", "bucket", None)
    calls = []
    assert probe_s3_bucket(row, settings, HealthBudgets(),
                           client_factory=lambda *_: s3_client(error, calls)) == code
    assert calls == [{"Bucket": "bucket"}], "only HeadBucket is allowed: no listing or writes"


def test_head_bucket_success_names_the_backend_scope():
    settings = StorageConfig(storage_backend="s3", s3_bucket="bucket")
    row = ProfileRow("s3", None, "http://s3.internal", "bucket", None)
    code = probe_s3_bucket(row, settings, HealthBudgets(), client_factory=lambda *_: s3_client())
    assert code == "bucket_accessible"
    assert Check("connected", code, 1, "s3").scope == "bucket_head"


def test_credential_failures_never_echo_identifiers():
    from botocore.exceptions import NoCredentialsError

    from mmkb.errors import StorageCredentialError

    assert classify_s3_error(NoCredentialsError()) == "storage_access_denied"
    assert classify_s3_error(StorageCredentialError()) == "storage_access_denied"


def registered_config(**overrides):
    env = {"STORAGE_BACKEND": "local", "STORAGE_PROFILE_NAME": "local-default",
           "LOCAL_STORAGE_ROOT": "/data/assets"}
    env.update({key: ("" if value is None else str(value)) for key, value in overrides.items()})
    return HealthConfig(database_url="postgresql://x", storage=resolve_storage_config(env), services=())


CONNECTED = Check("connected", "database_ready", 5)
DOWN = Check("down", "database_unreachable", 5)


def test_storage_waits_for_the_database_instead_of_guessing_the_profile():
    report = probe_storage(DOWN, None, registered_config(), HealthBudgets())
    assert (report.state, report.code, report.scope) == ("unknown", "dependency_unavailable", "dependency")
    assert report.backend is None


def test_missing_profile_row_asks_for_explicit_initialization():
    report = probe_storage(CONNECTED, None, registered_config(), HealthBudgets())
    assert (report.code, report.scope) == ("profile_not_initialized", "profile")


@pytest.mark.parametrize("row", [
    ProfileRow("s3", None, "http://other.internal", "bucket", None),
    ProfileRow("local", "/elsewhere", None, None, None),
    ProfileRow("local", "/data/assets", None, None, "unexpected-credential-ref"),
])
def test_registered_profile_conflicts_are_not_replaced_by_another_bucket(row):
    report = probe_storage(CONNECTED, row, registered_config(), HealthBudgets())
    assert (report.code, report.scope) == ("profile_conflict", "profile")


def test_local_and_s3_probes_run_against_the_registered_profile_only(tmp_path):
    config = registered_config(LOCAL_STORAGE_ROOT=str(tmp_path))
    row = ProfileRow("local", str(tmp_path), None, None, None)
    report = probe_storage(CONNECTED, row, config, HealthBudgets())
    assert (report.code, report.backend, report.scope) == ("local_directory_accessible", "local",
                                                           "local_directory")
    assert report.latency_ms is not None

    s3_env = {"STORAGE_BACKEND": "s3", "STORAGE_PROFILE_NAME": "s3-default", "S3_BUCKET": "assets",
              "S3_ENDPOINT_URL": "http://s3.internal"}
    s3_config = HealthConfig(database_url="postgresql://x", storage=resolve_storage_config(s3_env),
                             services=())
    s3_row = ProfileRow("s3", None, "http://s3.internal", "assets", None)
    calls = []
    report = probe_storage(CONNECTED, s3_row, s3_config, HealthBudgets(),
                           client_factory=lambda *_: s3_client(None, calls))
    assert (report.code, report.backend, report.scope) == ("bucket_accessible", "s3", "bucket_head")
    assert calls == [{"Bucket": "assets"}]


def test_unknown_registered_backend_is_reported_instead_of_constructing_an_adapter():
    row = ProfileRow("swift", None, None, "container", None)
    report = probe_storage(CONNECTED, row, registered_config(), HealthBudgets(),
                           client_factory=lambda *_: pytest.fail("no client for an unknown backend"))
    assert report.code == "profile_conflict"


def test_probe_round_reports_every_component_exactly_once(monkeypatch, tmp_path):
    # Nothing in a health round may write: these guards fail the test if a probe ever does.
    for name in ("put", "delete"):
        monkeypatch.setattr("mmkb.storage.LocalStorage." + name,
                            lambda *args, **kwargs: pytest.fail("storage write during health probe"))
    monkeypatch.setattr("pathlib.Path.mkdir",
                        lambda *args, **kwargs: pytest.fail("health probe created a directory"))
    monkeypatch.setattr("mmkb.storage.initialize_profile",
                        lambda *args, **kwargs: pytest.fail("health probe initialized a profile"))
    emitted: dict[str, Check] = {}

    config = HealthConfig(
        database_url="",  # invalid config path: exercises the database and storage dependency chain
        storage=resolve_storage_config({"LOCAL_STORAGE_ROOT": str(tmp_path)}),
        services=(service_probe("vision", CAPTION_ENABLED="false"), service_probe("decision",
                                                                                   configured=False)),
    )
    budgets = HealthBudgets()
    complete = run_round(config, budgets, lambda key, check: emitted.__setitem__(key, check),
                         client_factory=lambda *_: s3_client())
    assert complete
    assert set(emitted) == {"database", "storage", "vision", "decision"}
    assert emitted["database"].code == "invalid_config"
    assert emitted["storage"].code == "dependency_unavailable"
    assert emitted["vision"].code == "service_disabled"
    assert emitted["decision"].code == "optional_not_configured"


def test_probe_round_keeps_early_results_when_a_component_overruns():
    emitted: dict[str, Check] = {}

    config = HealthConfig(database_url="", storage=None, services=(service_probe("embedding"),))
    budgets = HealthBudgets(total_seconds=0.4, cleanup_seconds=0.1, model_seconds=5.0)
    recorder = Recorder(httpx.Response(200, json={"data": [{"id": "embed-model"}]}))
    complete = run_round(config, budgets, lambda key, check: emitted.__setitem__(key, check),
                         transport=httpx.MockTransport(lambda request: time.sleep(0.5) or recorder.response))
    assert complete is False
    # The database answered immediately and its result is retained even though the model round ran over.
    assert emitted["database"].code == "invalid_config"


@pytest.mark.parametrize("stage", ["create", "connect", "inspect", "dispose"])
def test_database_total_budget_includes_setup_queries_and_cleanup(monkeypatch, stage):
    release, finished = threading.Event(), threading.Event()

    def block(at):
        if stage == at:
            release.wait(2)

    class Connection:
        def __enter__(self):
            block("connect")
            return self

        def __exit__(self, *args):
            pass

    def dispose():
        block("dispose")
        finished.set()

    def create(*args, **kwargs):
        block("create")
        return SimpleNamespace(connect=Connection, dispose=dispose)

    def inspect(*args):
        block("inspect")
        return "database_ready", None

    monkeypatch.setattr("mmkb.health_probes.sa.create_engine", create)
    monkeypatch.setattr("mmkb.health_probes.inspect_database", inspect)
    try:
        started = time.monotonic()
        check, row = probe_database(registered_config(), HealthBudgets(database_seconds=0.05))
        elapsed = time.monotonic() - started
        assert elapsed < 0.5, "a synchronous driver must not hold up its component report"
        assert (check.state, check.code, row) == ("unknown", "check_deadline_exceeded", None)
    finally:
        release.set()
        assert finished.wait(1), "the fixture operation must finish before monkeypatches are removed"
    assert check.state == "unknown", "a late success must not replace the timeout observation"


@pytest.mark.parametrize("stage", ["credentials", "head", "close"])
def test_storage_total_budget_includes_credentials_request_and_cleanup(stage):
    release, finished = threading.Event(), threading.Event()

    def block(at):
        if stage == at:
            release.wait(2)

    def close():
        block("close")
        finished.set()

    def head(**kwargs):
        block("head")
        return {}

    def factory(*args):
        block("credentials")
        return SimpleNamespace(head_bucket=head, close=close)

    config = registered_config(STORAGE_BACKEND="s3", STORAGE_PROFILE_NAME="s3-default",
                               S3_ENDPOINT_URL="http://s3.internal", S3_BUCKET="assets")
    row = ProfileRow("s3", None, "http://s3.internal", "assets", None)
    try:
        started = time.monotonic()
        check = probe_storage(CONNECTED, row, config, HealthBudgets(storage_seconds=0.05),
                              client_factory=factory)
        assert time.monotonic() - started < 0.5
        assert (check.state, check.code, check.backend) == ("unknown", "check_deadline_exceeded", "s3")
    finally:
        release.set()
        assert finished.wait(1)
    assert check.state == "unknown"


def test_local_directory_open_is_also_subject_to_the_storage_budget(monkeypatch, tmp_path):
    release, finished = threading.Event(), threading.Event()

    def blocked_scandir(path):
        release.wait(2)
        finished.set()
        return nullcontext()  # The late success must not replace the timeout observation.

    monkeypatch.setattr("mmkb.health_probes.os.scandir", blocked_scandir)
    config = registered_config(LOCAL_STORAGE_ROOT=str(tmp_path))
    row = ProfileRow("local", str(tmp_path), None, None, None)
    try:
        started = time.monotonic()
        check = probe_storage(CONNECTED, row, config, HealthBudgets(storage_seconds=0.05))
        assert time.monotonic() - started < 0.5
        assert (check.state, check.code, check.backend) == ("unknown", "check_deadline_exceeded", "local")
    finally:
        release.set()
        assert finished.wait(1)
