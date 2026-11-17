"""Report contract, summary priority, TTL cache and probe-process supervision."""
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from mmkb.config import resolve_health_config
from mmkb.health import HealthBudgets, HealthService, ProbeRound, SCHEMA_VERSION, parse_check_payload
from mmkb.health_contract import (
    CHECK_KEYS, Check, check_result, error_code_catalog, summarize_status,
)
from mmkb.schema_contract import EXPECTED_HEAD, EXPECTED_REVISIONS

ROOT = Path(__file__).resolve().parents[1]
ENV = {"DATABASE_URL": "postgresql://user:secret@db.internal:5432/mmkb",
       "EMBEDDING_BASE_URL": "http://model.internal:8000/v1", "EMBEDDING_MODEL": "embed-model"}
BASELINE = {"api": "api_reachable", "database": "database_ready", "embedding": "model_list_verified",
            "storage": "bucket_accessible", "vision": "model_list_verified",
            "decision": "model_list_verified", "worker": "heartbeat_not_implemented"}


def report_checks(**overrides) -> dict[str, Check]:
    checks = {}
    for key, code in BASELINE.items():
        backend = "s3" if key == "storage" else None
        checks[key] = check_result(key, code, backend=backend)
    for key, value in overrides.items():
        checks[key] = value if isinstance(value, Check) else check_result(key, value)
    return checks


@pytest.mark.parametrize("overrides,expected", [
    ({}, "healthy"),
    ({"database": "database_unreachable"}, "unavailable"),
    ({"database": "schema_outdated"}, "unavailable"),
    ({"embedding": "required_not_configured"}, "unavailable"),
    ({"storage": "profile_not_initialized"}, "unavailable"),
    ({"embedding": "check_deadline_exceeded"}, "unknown"),
    ({"database": "check_deadline_exceeded"}, "unknown"),
    # A required unknown outranks optional degradations, and the worker never changes the outcome.
    ({"database": "check_deadline_exceeded", "vision": "model_unreachable"}, "unknown"),
    ({"vision": "model_unreachable"}, "degraded"),
    ({"decision": "check_deadline_exceeded"}, "degraded"),
    ({"vision": "invalid_config"}, "degraded"),
    ({"vision": "required_not_configured"}, "degraded"),
    ({"vision": "optional_not_configured"}, "healthy"),
    ({"decision": "service_disabled"}, "healthy"),
    ({"vision": "optional_not_configured", "decision": "service_disabled"}, "healthy"),
])
def test_summary_priority(overrides, expected):
    assert summarize_status(report_checks(**overrides)) == expected


def test_worker_is_always_unmonitored_and_never_degrades_the_report():
    checks = report_checks()
    assert checks["worker"].state == "unknown"
    assert checks["worker"].code == "heartbeat_not_implemented"
    assert checks["worker"].scope == "not_monitored"
    assert summarize_status(checks) == "healthy"


def test_every_code_carries_a_fixed_message_and_scope():
    for key, codes in error_code_catalog().items():
        for code in sorted(codes):
            check = check_result(key, code, backend="local" if key == "storage" else None)
            assert check.message and "\n" not in check.message
            assert check.scope
            # Probes cannot claim a success state for a failure code, or the reverse.
            with pytest.raises(ValueError):
                Check("connected" if check.state != "connected" else "down", code)


class FakeRunner:
    """Stands in for the subprocess runner; records rounds and can hang on demand."""

    def __init__(self, round_result=None, *, gate=None, rounds=None):
        self.rounds = list(rounds) if rounds else [round_result or ProbeRound()]
        self.calls: list[float] = []
        self.gate = gate
        self.reaped = 0
        self.closed = 0

    async def run(self, request, *, timeout):
        self.calls.append(timeout)
        if self.gate is not None:
            await self.gate.wait()
        return self.rounds[min(len(self.calls) - 1, len(self.rounds) - 1)]

    async def reap(self):
        self.reaped += 1
        return True

    async def aclose(self):
        self.closed += 1


def service(*, runner=None, clock=None, budgets=None):
    state = {"now": 1_000.0}

    def tick():
        return state["now"]

    health = HealthService(resolve_health_config(ENV), budgets=budgets or HealthBudgets(),
                           runner=runner or FakeRunner(), clock=clock or tick)
    health.advance = lambda seconds: state.__setitem__("now", state["now"] + seconds)  # type: ignore[attr-defined]
    return health


def test_report_shape_is_the_frozen_contract():
    async def scenario():
        health = service(runner=FakeRunner(ProbeRound(checks={
            "database": check_result("database", "database_ready", latency_ms=30),
            "embedding": check_result("embedding", "model_list_verified", latency_ms=180),
            "storage": check_result("storage", "bucket_accessible", latency_ms=90, backend="s3"),
            "vision": check_result("vision", "optional_not_configured"),
            "decision": check_result("decision", "service_disabled"),
        })))
        return await health.report()

    report = asyncio.run(scenario())
    assert report["schema_version"] == SCHEMA_VERSION == 1
    assert report["status"] == "healthy"
    assert list(report["checks"]) == list(CHECK_KEYS)
    assert report["checked_at"].endswith("Z")
    assert report["cached"] is False and report["age_ms"] == 0
    assert report["ttl_ms"] == 10_000
    api = report["checks"]["api"]
    assert api == {"state": "connected", "required": True, "code": "api_reachable",
                   "message": "API 已响应", "latency_ms": None, "scope": "http_response"}
    assert report["checks"]["database"]["latency_ms"] == 30
    assert report["checks"]["storage"]["backend"] == "s3"
    assert report["checks"]["vision"] == {"state": "not_configured", "required": False,
                                          "code": "optional_not_configured", "message": "未配置该可选服务",
                                          "latency_ms": None, "scope": "configuration"}
    assert report["checks"]["worker"]["state"] == "unknown"
    for payload in report["checks"].values():
        # No free-form details: only the fixed keys may appear.
        assert set(payload) <= {"state", "required", "code", "message", "latency_ms", "scope", "backend"}


def test_concurrent_requests_share_one_round_and_cache_expires():
    async def scenario():
        runner = FakeRunner(ProbeRound(checks={"database": check_result("database", "database_ready")}),
                            gate=asyncio.Event())
        health = service(runner=runner)
        first = asyncio.ensure_future(health.report())
        second = asyncio.ensure_future(health.report())
        await asyncio.sleep(0)
        runner.gate.set()
        left, right = await asyncio.gather(first, second)
        return health, runner, left, right

    health, runner, left, right = asyncio.run(scenario())
    assert len(runner.calls) == 1, "concurrent GET requests must not multiply external probes"
    assert left["cached"] is False and right["cached"] is False
    # A child that exits without reporting storage is an incomplete observation, not a healthy one.
    assert left["checks"]["storage"]["code"] == "probe_protocol_error"

    async def later():
        health.advance(9.0)
        cached = await health.report()
        health.advance(1.1)
        fresh = await health.report()
        return cached, fresh

    cached, fresh = asyncio.run(later())
    assert cached["cached"] is True and cached["age_ms"] == 9_000
    assert cached["checks"] == left["checks"]
    assert fresh["cached"] is False and len(runner.calls) == 2


def test_one_cancelling_client_does_not_cancel_the_shared_round():
    async def scenario():
        runner = FakeRunner(ProbeRound(checks={"database": check_result("database", "database_ready")}),
                            gate=asyncio.Event())
        health = service(runner=runner)
        waiter_one = asyncio.ensure_future(health.report())
        waiter_two = asyncio.ensure_future(health.report())
        await asyncio.sleep(0)
        waiter_one.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_one
        assert not waiter_two.done()
        runner.gate.set()
        report = await waiter_two
        return runner, report

    runner, report = asyncio.run(scenario())
    assert len(runner.calls) == 1
    assert report["status"] == "unknown"  # storage was never reported by the fake child


def test_deadline_keeps_completed_components_and_marks_the_rest_unknown():
    async def scenario():
        runner = FakeRunner(ProbeRound(checks={"database": check_result("database", "database_ready", latency_ms=12),
                                               "embedding": check_result("embedding", "model_timeout", latency_ms=2_000)},
                                       outcome="deadline"))
        report = await service(runner=runner).report()
        return report

    report = asyncio.run(scenario())
    assert report["checks"]["database"]["state"] == "connected"
    assert report["checks"]["embedding"] == {"state": "down", "required": True, "code": "model_timeout",
                                             "message": "模型服务列表检查超时", "latency_ms": 2_000,
                                             "scope": "model_list"}
    # The child never reported storage, so the parent cannot conclude anything about it.
    assert report["checks"]["storage"]["code"] == "check_deadline_exceeded"
    for key in ("vision", "decision"):
        assert report["checks"][key]["code"] == "check_deadline_exceeded"
        assert report["checks"][key]["scope"] == "probe"
    assert report["status"] == "unavailable"  # storage is required and could not be determined


@pytest.mark.parametrize("outcome,code", [("start_failed", "probe_start_failed"),
                                          ("protocol_error", "probe_protocol_error"),
                                          ("occupied", "probe_start_failed")])
def test_probe_faults_are_unknown_observations_not_dependency_failures(outcome, code):
    async def scenario():
        report = await service(runner=FakeRunner(ProbeRound(outcome=outcome))).report()
        return report

    report = asyncio.run(scenario())
    assert report["status"] == "unknown"
    for key in ("database", "embedding", "storage", "vision", "decision"):
        assert report["checks"][key]["code"] == code
    assert report["checks"]["api"]["state"] == "connected"


def test_unreaped_probe_blocks_new_rounds_until_it_is_recovered():
    async def scenario():
        runner = FakeRunner(ProbeRound(checks={"database": check_result("database", "database_ready")},
                                       outcome="deadline", unreaped_pid=4242))
        health = service(runner=runner)
        first = await health.report()
        second = await health.report()
        # The occupancy fault itself is never cached; the honest partial snapshot still serves its TTL.
        cached = await health.report()
        health.advance(10.1)
        runner.rounds = [ProbeRound(checks={"database": check_result("database", "database_ready")})]
        recovered = await health.report()
        return first, second, cached, recovered, runner

    first, second, cached, recovered, runner = asyncio.run(scenario())
    assert first["checks"]["embedding"]["code"] == "check_deadline_exceeded"
    assert second["checks"]["database"]["code"] == "probe_start_failed"
    assert cached["cached"] is True and cached["checks"]["database"]["code"] == "database_ready"
    assert recovered["cached"] is False and recovered["checks"]["database"]["code"] == "database_ready"
    assert len(runner.calls) == 2, "no new probe process may start while one is unconfirmed"
    assert runner.reaped == 1


def test_close_cancels_the_round_and_closes_the_runner():
    async def scenario():
        gate = asyncio.Event()
        runner = FakeRunner(ProbeRound(outcome="cancelled"), gate=gate)
        health = service(runner=runner)
        pending = asyncio.ensure_future(health.report())
        await asyncio.sleep(0)
        await health.aclose()
        with pytest.raises(asyncio.CancelledError):
            await pending
        with pytest.raises(RuntimeError):
            await health.report()
        return runner

    runner = asyncio.run(scenario())
    assert runner.closed == 1


@pytest.mark.parametrize("frame", [
    {"v": 2, "check": "database", "code": "database_ready"},
    {"v": 1, "check": "api", "code": "api_reachable"},
    {"v": 1, "check": "worker", "code": "heartbeat_not_implemented"},
    {"v": 1, "check": "database", "code": "model_not_found"},
    {"v": 1, "check": "storage", "code": "model_not_found"},
    {"v": 1, "check": "embedding", "code": "database_ready"},
    {"v": 1, "check": "database", "code": "database_ready", "latency_ms": -1},
    {"v": 1, "check": "database", "code": "database_ready", "latency_ms": "30"},
    {"v": 1, "check": "embedding", "code": "model_list_verified", "backend": "s3"},
    {"v": 1, "check": "storage", "code": "bucket_accessible", "backend": "swift"},
    {"v": 1, "check": "storage", "code": "bucket_accessible", "message": "secrets"},
    {"v": 1, "check": "vision", "code": "model_list_verified", "state": "down"},
    "not-a-dict",
])
def test_child_frames_outside_the_contract_are_rejected(frame):
    assert parse_check_payload(frame) is None


def test_accepted_child_frame_carries_only_contract_fields():
    check = parse_check_payload({"v": 1, "check": "storage", "code": "local_directory_accessible",
                                 "latency_ms": 3, "backend": "local"})
    assert check is not None
    assert (check.state, check.code, check.latency_ms, check.backend) == (
        "connected", "local_directory_accessible", 3, "local")
    assert check.scope == "local_directory"


def test_migration_contract_matches_repository_heads():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(ROOT / "migrations"))
    assert set(script.get_heads()) == {EXPECTED_HEAD}
    assert {revision.revision for revision in script.walk_revisions()} == set(EXPECTED_REVISIONS)


def test_revision_metadata_is_installed_with_the_package(tmp_path):
    # An installed wheel has no alembic.ini or migrations/ next to the working directory.
    code = ("import mmkb.schema_contract as c; "
            "print(c.EXPECTED_HEAD, len(c.EXPECTED_REVISIONS), c.VERSION_TABLE, c.EXPECTED_EMBEDDING_TYPE)")
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [EXPECTED_HEAD, str(len(EXPECTED_REVISIONS)),
                                     "public.mmkb_alembic_version", "halfvec(2048)"]
