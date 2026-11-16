from dataclasses import replace
from io import BytesIO
from threading import Event
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa

from mmkb.config import AppConfig
from mmkb.errors import ResourceError
from mmkb.processing import ProcessingResult, ResourceSnapshot
from mmkb.providers import ProviderError
from mmkb.worker import ClaimedResource, Worker, WorkerLock, safe_error


def connection():
    conn = MagicMock()
    conn.execution_options.return_value = conn
    conn.closed = conn.invalidated = False
    return conn


def test_singleton_lock_pins_and_releases_one_session():
    conn, engine = connection(), Mock()
    engine.connect.return_value = conn
    conn.scalar.side_effect = [True, 101, 101]
    with WorkerLock(engine) as guard:
        guard.check()
        conn.close.assert_not_called()
    engine.connect.assert_called_once()
    assert "pg_advisory_unlock" in str(conn.execute.call_args.args[0])
    conn.close.assert_called_once()


def test_second_worker_refuses_to_run_without_unlocking_the_other_owner():
    conn, engine = connection(), Mock()
    engine.connect.return_value = conn
    conn.scalar.return_value = False
    with pytest.raises(ResourceError, match="Another Worker"):
        with WorkerLock(engine):
            pytest.fail("A second Worker must not enter")
    conn.execute.assert_not_called()
    conn.close.assert_called_once()


def test_lost_lock_connection_is_never_reconnected():
    conn, engine = connection(), Mock()
    engine.connect.return_value = conn
    conn.scalar.side_effect = [True, 101]
    with WorkerLock(engine) as guard:
        conn.invalidated = True
        with pytest.raises(ResourceError, match="ownership lost"):
            guard.check()
    engine.connect.assert_called_once()
    conn.execute.assert_not_called()


def test_session_change_aborts_and_unlock_failure_discards_connection():
    conn, engine = connection(), Mock()
    engine.connect.return_value = conn
    conn.scalar.side_effect = [True, 101, 102]
    conn.execute.side_effect = sa.exc.SQLAlchemyError("lost connection")
    with WorkerLock(engine) as guard:
        with pytest.raises(ResourceError, match="session changed"):
            guard.check()
    conn.invalidate.assert_called_once()
    conn.close.assert_called_once()


@pytest.fixture
def worker():
    processor = Mock()
    snapshot = ResourceSnapshot(uuid4(), 1, "text", user_text="text")
    result = ProcessingResult(snapshot.resource_id, 1, "text", None, None,
                              [1.0] + [0.0] * 2047, "text", [])
    processor.process.return_value = result
    worker = Worker(Mock(), AppConfig(database_url="postgresql://unused"), Mock(), processor)
    worker.claim = Mock(return_value=ClaimedResource(snapshot, None, None, None))
    worker.complete = Mock(return_value=True)
    worker.fail = Mock(return_value=True)
    worker.resources.cleanup_batch = Mock(return_value={"scanned": 0, "cleaned": 0, "failed": 0})
    return worker


def test_once_processes_one_snapshot_and_runs_cleanup(worker):
    result = worker.run_once()
    assert result["claimed"] == 1 and result["outcome"] == "ready"
    worker.claim.assert_called_once()
    worker.complete.assert_called_once_with(worker.processor.process.return_value)
    worker.fail.assert_not_called()
    worker.resources.cleanup_batch.assert_called_once_with(100)


def test_failed_and_stale_tasks_do_not_prevent_cleanup(worker):
    worker.processor.process.side_effect = ProviderError("embedding", "timeout", "safe")
    worker.fail.return_value = False
    assert worker.run_once()["outcome"] == "stale"
    worker.complete.assert_not_called()
    worker.resources.cleanup_batch.assert_called_once()


def test_database_failure_stops_the_loop_instead_of_marking_a_task_failed(worker):
    worker.complete.side_effect = sa.exc.SQLAlchemyError("private database error")
    with pytest.raises(sa.exc.SQLAlchemyError):
        worker.run_once()
    worker.fail.assert_not_called()
    worker.resources.cleanup_batch.assert_not_called()


def test_original_read_failure_closes_the_stream_before_recording_failure(worker):
    class BrokenStream(BytesIO):
        def read(self, *args):
            raise OSError("private endpoint details")

    stream = BrokenStream(b"original")
    worker.resolver.return_value.open.return_value = stream
    worker.claim.return_value = replace(worker.claim.return_value, object_key="original.png", profile=Mock(), mime_type="image/png")
    assert worker.run_once()["outcome"] == "failed"
    assert stream.closed
    worker.processor.process.assert_not_called()
    assert "private" not in safe_error(worker.fail.call_args.args[1])


def test_shutdown_finishes_current_resource_then_stops_claiming(worker):
    stop = Event()

    def process(*args, **kwargs):
        stop.set()
        return Mock()

    worker.processor.process.side_effect = process
    worker.run(stop)
    worker.claim.assert_called_once()
    worker.complete.assert_called_once()


def test_guard_failure_before_commit_does_not_open_a_business_transaction():
    sessions = Mock()
    guard = Mock(side_effect=ResourceError("worker_lock_lost", "lost", 503))
    worker = Worker(sessions, AppConfig(database_url="postgresql://unused"), Mock(), Mock(), ensure_owner=guard)
    with pytest.raises(ResourceError):
        worker.complete(Mock())
    sessions.begin.assert_not_called()


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_poll_interval_must_be_finite_and_positive(worker, interval):
    with pytest.raises(ValueError):
        worker.run(Event(), poll_seconds=interval)
