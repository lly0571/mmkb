"""``GET /api/health`` HTTP semantics: 200 for reports, no-store, OpenAPI contract, no leaks."""
import asyncio
import json
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from mmkb.api import create_app
from mmkb.config import AppConfig, resolve_health_config
from mmkb.health import HealthBudgets, HealthService, ProbeRound
from mmkb.health_contract import CHECK_KEYS, check_result

ENV = {"DATABASE_URL": "postgresql://user-token@db.internal:5432/mmkb",
       "EMBEDDING_BASE_URL": "http://model.internal:8000/v1", "EMBEDDING_MODEL": "embed-model"}
HEALTHY = {"database": check_result("database", "database_ready", latency_ms=12),
           "embedding": check_result("embedding", "model_list_verified", latency_ms=90),
           "storage": check_result("storage", "local_directory_accessible", latency_ms=3, backend="local"),
           "vision": check_result("vision", "optional_not_configured"),
           "decision": check_result("decision", "service_disabled")}


class StubRunner:
    def __init__(self, checks=None, *, outcome="complete", error=None):
        self.checks = dict(HEALTHY if checks is None else checks)
        self.outcome = outcome
        self.error = error
        self.rounds = 0
        self.closed = 0

    async def run(self, request, *, timeout):
        self.rounds += 1
        if self.error is not None:
            raise self.error
        return ProbeRound(checks=self.checks, outcome=self.outcome)

    async def reap(self):
        return True

    async def aclose(self):
        self.closed += 1


def app_with(dependencies, runner, **overrides):
    config = dependencies
    health = HealthService(resolve_health_config(dict(ENV, **overrides.get("env", {}))),
                           budgets=HealthBudgets(), runner=runner)
    return create_app(config, Mock(), Mock(), Mock(), health_service=health), health


@pytest.fixture
def config():
    return AppConfig(database_url="postgresql://unused")


def get_health(app):
    # raise_server_exceptions=False keeps the real 500 behaviour observable.
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.get("/api/health")


def test_dependency_failures_still_answer_http_200_with_no_store(config):
    runner = StubRunner(dict(HEALTHY, database=check_result("database", "database_unreachable")))
    app, _health = app_with(config, runner)
    response = get_health(app)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["database"]["state"] == "down"
    assert body["checks"]["database"]["required"] is True
    assert body["checks"]["vision"]["required"] is False
    assert list(body["checks"]) == list(CHECK_KEYS)
    assert runner.rounds == 1


def test_healthy_report_names_the_scope_of_every_component(config):
    app, _health = app_with(config, StubRunner())
    body = get_health(app).json()
    assert body["status"] == "healthy"
    assert body["checks"]["api"] == {"state": "connected", "required": True, "code": "api_reachable",
                                     "message": "API 已响应", "latency_ms": None, "scope": "http_response"}
    assert body["checks"]["storage"]["backend"] == "local"
    assert {check["scope"] for check in body["checks"].values()} == {
        "http_response", "connection_and_schema", "model_list", "local_directory", "configuration",
        "not_monitored",
    }
    # The page must be able to state the limits of each check without inferring more.
    assert body["checks"]["embedding"]["message"] == "配置模型已在服务列表中发现，未执行推理"
    assert body["checks"]["worker"]["message"] == "尚无 Worker 心跳，无法判断是否运行"


def test_a_shared_snapshot_is_marked_as_cached(config):
    runner = StubRunner()
    app, _health = app_with(config, runner)
    with TestClient(app) as client:
        first = client.get("/api/health").json()
        second = client.get("/api/health").json()
    assert (first["cached"], second["cached"]) == (False, True)
    assert second["age_ms"] >= 0 and second["ttl_ms"] == 10_000
    assert runner.rounds == 1, "the browser must not multiply external dependency checks"


def test_the_report_never_leaks_credentials_addresses_or_model_ids(config):
    app, _health = app_with(config, StubRunner())
    text = get_health(app).text
    for marker in ("user-token", "db.internal", "model.internal", "embed-model", "5432", "bearer",
                   "postgresql"):
        assert marker.lower() not in text.lower(), marker
    assert json.loads(text)["status"] == "healthy"


def test_an_aggregator_bug_stays_a_500_instead_of_an_empty_green_report(config):
    app, _health = app_with(config, StubRunner(error=RuntimeError("aggregator crashed")))
    response = get_health(app)
    assert response.status_code == 500
    assert "api_reachable" not in response.text


def test_child_process_faults_are_reported_as_unknown_not_offline_dependencies(config):
    app, _health = app_with(config, StubRunner({}, outcome="start_failed"))
    body = get_health(app).json()
    assert body["status"] == "unknown"
    assert body["checks"]["database"]["code"] == "probe_start_failed"
    assert body["checks"]["api"]["state"] == "connected"


def test_openapi_documents_the_frozen_report_contract(config):
    app, _health = app_with(config, StubRunner())
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
    reference = schema["paths"]["/api/health"]["get"]["responses"]["200"]["content"] \
        ["application/json"]["schema"]["$ref"]
    assert reference == "#/components/schemas/HealthReport"
    assert set(schema["components"]["schemas"]["HealthReport"]["properties"]) == {
        "schema_version", "status", "checked_at", "duration_ms", "cached", "age_ms", "ttl_ms", "checks"}
    assert {"state", "required", "code", "message", "latency_ms", "scope", "backend"} <= set(
        schema["components"]["schemas"]["HealthCheckModel"]["properties"])


def test_an_injected_health_service_is_closed_by_its_owner(config):
    runner = StubRunner()
    app, _health = app_with(config, runner)
    get_health(app)
    assert runner.closed == 0, "only an owned service may be closed by the application lifespan"


def test_owned_health_service_is_closed_on_shutdown(config, monkeypatch):
    import mmkb.api as api

    closed = []
    real = HealthService

    class Owned(real):
        async def aclose(self):
            closed.append("closed")
            await real.aclose(self)

    class Runner:
        async def run(self, request, *, timeout):
            raise AssertionError("no probe should run in this test")

        async def reap(self):
            return True

        async def aclose(self):
            return None

    monkeypatch.setattr(api, "HealthService", Owned)
    monkeypatch.setattr(api, "health_config_from_app", lambda _config: resolve_health_config(ENV))
    monkeypatch.setattr(api, "SubprocessProbeRunner", Runner)
    app = create_app(config, Mock(), Mock(), Mock())
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/health").status_code in {200, 500}
    assert closed == ["closed"]


def test_the_probe_snapshot_describes_the_app_that_answers_the_request(monkeypatch):
    """create_app(AppConfig(...)) must probe those settings, not a second deployment from the env."""
    import mmkb.api as api

    monkeypatch.setenv("DATABASE_URL", "postgresql://env-user@env-db.internal:5432/env_db")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_PROFILE_NAME", "env-profile")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", "/tmp/env-assets")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://model.internal:8000/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "embed-model")
    served = AppConfig(database_url="postgresql://app-user@app-db.internal:5432/app_db",
                       storage_backend="s3", storage_profile_name="app-profile",
                       local_storage_root="/tmp/app-assets", s3_endpoint_url="http://s3.internal:9000",
                       s3_bucket="app-bucket")

    class NoRound(StubRunner):
        async def run(self, request, *, timeout):
            raise AssertionError("building the app must not start a probe round")

    monkeypatch.setattr(api, "SubprocessProbeRunner", NoRound)
    app = create_app(served, Mock(), Mock(), Mock())
    with TestClient(app, raise_server_exceptions=False):
        snapshot = app.state.health.config

    assert snapshot.database_url == served.database_url
    assert "env_db" not in snapshot.database_url
    assert snapshot.storage is not None
    assert (snapshot.storage.storage_backend, snapshot.storage.storage_profile_name,
            snapshot.storage.s3_bucket, snapshot.storage.s3_endpoint_url) == (
        "s3", "app-profile", "app-bucket", "http://s3.internal:9000")
    assert snapshot.storage.local_storage_root == "/tmp/app-assets"
    # Model services are not part of AppConfig, so they still come from the environment.
    embedding = next(item for item in snapshot.services if item.key == "embedding")
    assert embedding.config is not None
    assert (embedding.config.base_url, embedding.config.model) == ("http://model.internal:8000/v1",
                                                                   "embed-model")


def test_the_new_endpoint_does_not_displace_the_resource_contract(config):
    app, _health = app_with(config, StubRunner())
    with TestClient(app, raise_server_exceptions=False) as client:
        paths = client.get("/openapi.json").json()["paths"]
    assert "/api/health" in paths
    for route in ("/api/resources", "/api/resources/{resource_id}", "/api/resources/{resource_id}/original",
                  "/api/search"):
        assert route in paths, route


def test_close_is_idempotent_and_rejects_later_reports():
    async def scenario():
        health = HealthService(resolve_health_config(ENV), budgets=HealthBudgets(), runner=StubRunner())
        await health.report()
        await health.aclose()
        await health.aclose()
        try:
            await health.report()
        except RuntimeError as error:
            return str(error)
        raise AssertionError("a closed health service must refuse reports")

    assert "closed" in asyncio.run(scenario()).lower()
