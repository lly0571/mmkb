"""Probe-process supervision: deadline, partial results, reaping and controlled IPC."""
import asyncio
import json
import subprocess
import sys
import time

import pytest

from mmkb.config import health_config_payload, resolve_health_config
from mmkb.health import Check, HealthBudgets, HealthService
from mmkb.health_contract import DEPENDENCY_CHECKS
from mmkb.health_runner import SubprocessProbeRunner

# An empty DSN and a closed port keep every real probe answer fast and deterministic.
ENV = {"DATABASE_URL": "", "EMBEDDING_BASE_URL": "http://127.0.0.1:1/v1", "EMBEDDING_MODEL": "embed-model"}
FRAMES = {key: {"database": "database_ready", "embedding": "model_unreachable",
                "storage": "dependency_unavailable", "vision": "optional_not_configured",
                "decision": "optional_not_configured"}[key] for key in DEPENDENCY_CHECKS}

CHILD_COMMON = '''
import json, sys, time
raw = sys.stdin.read()
request = json.loads(raw)
config = request["health_config"]
out = sys.stdout


def emit(key, code, latency=None):
    out.write(json.dumps({"v": 1, "check": key, "code": code, "latency_ms": latency}) + "\\n")
    out.flush()
'''


def child(tmp_path, name, body):
    # The marker in the filename lets the tests prove no probe process survives.
    path = tmp_path / f"mmkb_probe_child_{name}.py"
    path.write_text(CHILD_COMMON + body)
    return [sys.executable, "-u", str(path)]


def payload(**overrides):
    config = resolve_health_config(dict(ENV, **overrides))
    return {"budgets": HealthBudgets(total_seconds=3.0).payload, "health_config": health_config_payload(config)}


def run(argv, *, timeout=6.0, request=None):
    async def scenario():
        runner = SubprocessProbeRunner(argv=argv)
        try:
            return await runner.run(request or payload(), timeout=timeout)
        finally:
            await runner.aclose()
    return asyncio.run(scenario())


def test_real_child_reports_every_component_from_its_stdin_snapshot(tmp_path):
    argv = child(tmp_path, "complete", """
for key, code in %r:
    emit(key, code, 5)
""" % sorted(FRAMES.items()))
    result = run(argv)
    assert result.outcome == "complete" and result.unreaped_pid is None
    assert set(result.checks) == set(DEPENDENCY_CHECKS)
    assert all(isinstance(check, Check) for check in result.checks.values())
    assert result.checks["embedding"].state == "down"
    assert result.checks["storage"].backend is None


def test_real_probe_module_answers_for_an_unreachable_setup():
    async def scenario():
        runner = SubprocessProbeRunner()
        try:
            return await runner.run(payload(), timeout=HealthBudgets().total_seconds + 1.0)
        finally:
            await runner.aclose()

    result = asyncio.run(scenario())
    assert result.outcome == "complete"
    assert result.checks["database"].code == "invalid_config"
    assert result.checks["storage"].code == "dependency_unavailable"
    assert result.checks["embedding"].code in ("model_unreachable", "model_timeout")


def test_configuration_reaches_the_child_on_stdin_and_never_on_the_command_line(tmp_path):
    record = tmp_path / "child-argv.json"
    argv = child(tmp_path, "argv", """
with open(%r, "w") as handle:
    json.dump({"argv": sys.argv[1:], "url": config["database_url"],
               "budget": request["budgets"]["total_seconds"]}, handle)
for key, code in %r:
    emit(key, code, 1)
""" % (str(record), sorted(FRAMES.items())))
    secret = "postgresql://probe-role:token@db.internal:5432/mmkb"
    result = run(argv, request={"budgets": HealthBudgets(total_seconds=3.0).payload,
                                "health_config": health_config_payload(resolve_health_config(dict(ENV, DATABASE_URL=secret)))})
    assert result.outcome == "complete"
    assert result.checks["database"].code == "database_ready"
    written = json.loads(record.read_text())
    # Only stdin carried the snapshot and the budgets; argv stayed free of them.
    assert written["url"] == secret and written["budget"] == 3.0
    assert not any("db.internal" in part or "secret" in part for part in written["argv"])


def test_hanging_child_is_killed_and_finished_results_survive(tmp_path):
    argv = child(tmp_path, "hang", """
emit("database", "database_ready", 7)
time.sleep(120)
""")
    result = run(argv, timeout=0.6)
    assert result.outcome == "deadline"
    assert set(result.checks) == {"database"}, "completed components must survive the timeout"
    assert result.unreaped_pid is None, "the probe process must be confirmed gone"
    assert runner_pids_clean()


def test_second_round_is_refused_while_the_previous_child_is_still_running(tmp_path):
    argv = child(tmp_path, "hang2", "time.sleep(120)\n")

    async def scenario():
        runner = SubprocessProbeRunner(argv=argv)
        running = asyncio.ensure_future(runner.run(payload(), timeout=5.0))
        await asyncio.sleep(0.4)
        refusal = await runner.run(payload(), timeout=0.2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await runner.aclose()
        return refusal

    refusal = asyncio.run(scenario())
    assert refusal.outcome == "occupied" and refusal.checks == {}
    assert isinstance(refusal.unreaped_pid, int)
    assert runner_pids_clean()


def test_cancelled_round_leaves_no_probe_process(tmp_path):
    argv = child(tmp_path, "hang3", "time.sleep(120)\n")

    async def scenario():
        runner = SubprocessProbeRunner(argv=argv)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(runner.run(payload(), timeout=10.0), 0.4)
        pid = runner.child_pid
        await runner.aclose()
        return pid

    pid = asyncio.run(scenario())
    assert pid is not None
    assert not process_alive(pid)


def test_broken_child_protocols_become_unknown_observations(tmp_path):
    cases = {
        "garbage": 'out.write("not json\\n")\nout.flush()\ntime.sleep(0.2)\n',
        "partial": 'emit("database", "database_ready")\n',
        "oversize": 'out.write("x" * 200000 + "\\n")\nout.flush()\ntime.sleep(0.2)\n',
        "wrong_key": 'emit("secrets", "database_ready")\n',
        "crash": "raise SystemExit(4)\n",
    }
    for name, body in cases.items():
        result = run(child(tmp_path, name, body), timeout=1.5)
        assert result.outcome in {"protocol_error", "deadline"}, name
        if name == "partial":
            assert set(result.checks) == {"database"}
        else:
            assert result.checks == {}, name


def test_unusable_executable_is_a_probe_fault(tmp_path):
    result = run([str(tmp_path / "not-an-interpreter"), "-c", "pass"])
    assert result.outcome == "start_failed" and result.checks == {}


def test_child_stderr_is_discarded_instead_of_logged(caplog, tmp_path):
    argv = child(tmp_path, "noisy", """
import logging
sys.stderr.write("dsn=postgresql://user:secret-token@db.internal/mmkb\\n")
logging.getLogger("botocore").warning("bucket secrets: private-bucket")
sys.stderr.flush()
for key, code in %r:
    emit(key, code, 1)
""" % sorted(FRAMES.items()))
    with caplog.at_level("DEBUG"):
        result = run(argv, timeout=2.0)
    assert result.outcome == "complete"
    joined = caplog.text + str(result.checks)
    for marker in ("secret-token", "private-bucket", "db.internal"):
        assert marker not in joined


def test_service_with_the_real_runner_answers_within_the_report_budget(tmp_path):
    budgets = HealthBudgets(total_seconds=5.0, ttl_seconds=10.0)

    async def scenario():
        service = HealthService(resolve_health_config(ENV), budgets=budgets,
                               runner=SubprocessProbeRunner(argv=child(
                                   tmp_path, "service", """
for key, code in %r:
    emit(key, code, 4)
""" % sorted(FRAMES.items()))))
        try:
            reports = await asyncio.gather(*[service.report() for _ in range(10)])
            cached = await service.report()
            return reports, cached
        finally:
            await service.aclose()

    reports, cached = asyncio.run(scenario())
    assert all(report["status"] == "unavailable" for report in reports)
    assert {report["checked_at"] for report in reports} == {reports[0]["checked_at"]}, \
        "ten concurrent requests must share one probe round"
    assert cached["cached"] is True
    assert reports[0]["checks"]["api"]["state"] == "connected"
    assert runner_pids_clean()


def process_alive(pid: int) -> bool:
    try:
        output = subprocess.run(["ps", "-p", str(pid), "-o", "state="], capture_output=True, text=True,
                                timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("ps is unavailable")
    state = output.stdout.strip()
    # A reaped child leaves no line; a zombie still has a row until the parent waits for it.
    return bool(state) and "Z" not in state


def runner_pids_clean() -> bool:
    output = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=5)
    leftovers = [line for line in output.stdout.splitlines()
                 if "mmkb.health_probes" in line or "mmkb_probe_child_" in line]
    return not leftovers


def test_a_child_that_exits_after_a_spent_wait_counts_as_reaped():
    """The final escalation may observe the exit without its await ever resolving."""
    async def scenario():
        runner = SubprocessProbeRunner(argv=[sys.executable, "-c", "pass"], cleanup_seconds=0.05)

        class WedgedThenGone:
            pid = 4_242
            returncode = None

            def terminate(self):
                return None

            def kill(self):
                # The exit is recorded, but the pending wait() still never resolves.
                self.returncode = -9

            async def wait(self):
                raise TimeoutError

        child = WedgedThenGone()
        runner._child = child
        reaped = await runner._stop(child, deadline=time.monotonic(), graceful=False)
        return reaped, child.returncode, await runner.reap()

    reaped, returncode, second_pass = asyncio.run(scenario())
    assert returncode is not None
    assert reaped, "an exited probe process must never be reported as still running"
    assert second_pass, "a recorded returncode ends the recovery attempt"


def test_probe_startup_cannot_wait_past_the_round_budget(monkeypatch):
    async def never_started(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("mmkb.health_runner.asyncio.create_subprocess_exec", never_started)

    async def scenario():
        runner = SubprocessProbeRunner()
        started = time.monotonic()
        result = await asyncio.wait_for(runner.run(payload(), timeout=0.05), timeout=0.5)
        return result, time.monotonic() - started, runner.child_pid

    result, elapsed, pid = asyncio.run(scenario())
    assert result.outcome == "deadline" and result.checks == {}
    assert pid is None and elapsed < 0.5


def test_cleanup_escalation_shares_one_budget_instead_of_resetting_it():
    async def scenario():
        runner = SubprocessProbeRunner(cleanup_seconds=0.06)
        signals = []

        class Unconfirmed:
            pid = 4_243
            returncode = None

            def terminate(self):
                signals.append("terminate")

            def kill(self):
                signals.append("kill")

            async def wait(self):
                await asyncio.Event().wait()

        started = time.monotonic()
        reaped = await runner._stop(Unconfirmed(), deadline=started + 0.06)
        return reaped, signals, time.monotonic() - started

    reaped, signals, elapsed = asyncio.run(scenario())
    assert not reaped and signals == ["terminate", "kill"]
    assert elapsed < 0.2, "terminate and kill must not each receive a fresh cleanup budget"


def test_component_timeout_can_finish_and_reap_a_child_with_a_hung_driver(tmp_path):
    path = tmp_path / "mmkb_probe_child_blocked_driver.py"
    path.write_text('''
import time
from mmkb import health_probes as probes

def blocked_driver(*args, **kwargs):
    time.sleep(120)

probes.sa.create_engine = blocked_driver
raise SystemExit(probes.main())
''')
    request = payload(DATABASE_URL="postgresql://unused")
    request["budgets"] = HealthBudgets(database_seconds=0.05).payload
    result = run([sys.executable, "-u", str(path)], request=request, timeout=3.0)
    assert result.outcome == "complete", "the component budget should finish before the round deadline"
    assert result.checks["database"].code == "check_deadline_exceeded"
    assert result.checks["storage"].code == "dependency_unavailable"
    assert set(result.checks) == set(DEPENDENCY_CHECKS)
    assert result.unreaped_pid is None and runner_pids_clean()
