from threading import Event
from unittest.mock import Mock

import pytest

from mmkb import server


def child(exit_code=None):
    process = Mock()
    process.poll.return_value = exit_code
    process.terminate.side_effect = lambda: setattr(process.poll, "return_value", 0)
    process.kill.side_effect = lambda: setattr(process.poll, "return_value", -9)
    return process


@pytest.mark.parametrize("exit_code", [0, 1, -9])
def test_worker_exit_stops_api_and_is_reported_as_failure(monkeypatch, exit_code):
    api, worker = child(), child(exit_code)
    popen = Mock(side_effect=[api, worker])
    monkeypatch.setattr(server.subprocess, "Popen", popen)
    monkeypatch.setenv("UVICORN_RELOAD", "true")
    monkeypatch.setenv("UVICORN_UDS", "/tmp/unrelated.sock")
    listener = Mock()
    listener.fileno.return_value = 42
    assert server.run_backend(listener, Event(), Event()) == 1
    api.terminate.assert_called_once()
    assert popen.call_args_list[0].kwargs["pass_fds"] == (42,)
    assert "UVICORN_RELOAD" not in popen.call_args.kwargs["env"]
    assert "UVICORN_UDS" not in popen.call_args.kwargs["env"]
    for process in (api, worker):
        process.wait.assert_called_once()


def test_api_startup_failure_stops_worker(monkeypatch):
    api, worker = child(1), child()
    monkeypatch.setattr(server.subprocess, "Popen", Mock(side_effect=[api, worker]))
    assert server.run_backend(Mock(), Event(), Event()) == 1
    worker.terminate.assert_called_once()


def test_normal_stop_signals_both_children_and_returns_success(monkeypatch):
    api, worker = child(), child()
    stop = Event()

    def launch(*args, **kwargs):
        if launch.called:
            stop.set()
            return worker
        launch.called = True
        return api

    launch.called = False
    monkeypatch.setattr(server.subprocess, "Popen", launch)
    assert server.run_backend(Mock(), stop, Event()) == 0
    api.terminate.assert_called_once()
    worker.terminate.assert_called_once()


def test_partial_launch_failure_cleans_up_the_already_started_api(monkeypatch):
    api = child()
    monkeypatch.setattr(server.subprocess, "Popen", Mock(side_effect=[api, OSError("private detail")]))
    assert server.run_backend(Mock(), Event(), Event()) == 1
    api.terminate.assert_called_once()


@pytest.mark.parametrize("force_stop", [False, True])
def test_unresponsive_children_are_killed_and_reaped_with_a_shared_budget(monkeypatch, force_stop):
    api, worker = child(), child()
    api.terminate.side_effect = worker.terminate.side_effect = None
    force = Event()
    if force_stop:
        force.set()
    monkeypatch.setattr(server.time, "monotonic", Mock(side_effect=[0, 2] if not force_stop else [0]))
    assert server.stop_children([("API", api), ("Worker", worker)], 1, force) is False
    for process in (api, worker):
        process.kill.assert_called_once()
        process.wait.assert_called_once()


@pytest.mark.parametrize("args", [["--poll-seconds", "nan"], ["--shutdown-timeout", "0"], ["--port", "65536"]])
def test_invalid_cli_options_do_not_spawn_processes(monkeypatch, args):
    popen = Mock()
    monkeypatch.setattr(server.subprocess, "Popen", popen)
    with pytest.raises(SystemExit) as error:
        server.main(args)
    assert error.value.code == 2
    popen.assert_not_called()


def test_occupied_port_fails_before_starting_worker(monkeypatch):
    monkeypatch.setattr(server, "load_app_config", Mock())
    monkeypatch.setattr(server, "load_model_config", Mock())
    monkeypatch.setattr(server.socket, "create_server", Mock(side_effect=OSError("occupied")))
    popen = Mock()
    monkeypatch.setattr(server.subprocess, "Popen", popen)
    assert server.main([]) == 1
    popen.assert_not_called()
