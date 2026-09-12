"""Version-only idle upgrades retain the existing daemon lifecycle policy."""
from pathlib import Path
from unittest.mock import Mock
import sys

import pytest

from vaws_coordinator import service


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    state = tmp_path / "coordinator"
    owner = service.CoordinatorService(state)
    marker = state / "test-listener"
    marker.touch()
    current = {"package": "vaws-coordinator", "version": "0.4.1", "pid": 999}
    loaded = {"package": "vaws-coordinator", "version": "0.4.0", "pid": 100}
    calls, launches = [], []
    clock = [100.0]
    restart_error = []
    monkeypatch.setattr(service, "LOADED_RUNTIMES", [current])
    monkeypatch.setattr(service, "_daemon_upgrade_attempts", {})
    monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(service, "socket_path", lambda _: marker)

    class Client:
        def __init__(self, state_dir):
            self.state_dir = Path(state_dir)

        def call(self, operation):
            calls.append(operation)
            if operation == "ping":
                if not marker.exists():
                    raise FileNotFoundError("stopped")
                return {"runtime": [{"loaded": dict(loaded)}]}
            assert operation == "restart_if_idle"
            if restart_error:
                raise restart_error[0]
            reply = owner.restart_if_idle()
            if reply["status"] == "stopping":
                marker.unlink()
            return reply

    def launch(command, **kwargs):
        assert not marker.exists(), "new daemon started before old listener closed"
        launches.append((command, kwargs))
        loaded.update(version=current["version"], pid=loaded["pid"] + 1)
        marker.touch()
        return Mock(poll=lambda: None)

    monkeypatch.setattr(service, "CoordinatorClient", Client)
    monkeypatch.setattr(service.subprocess, "Popen", launch)
    return state, owner, current, loaded, calls, launches, clock, restart_error


def test_idle_older_daemon_restarts_with_callers_interpreter(daemon):
    state, owner, current, loaded, calls, launches, *_ = daemon
    client = service.ensure_daemon(state)
    assert calls == ["ping", "ping", "restart_if_idle", "ping"]
    assert owner._stopped.is_set()
    assert client.runtime[0]["loaded"]["version"] == "0.4.1"
    assert client.runtime[0]["loaded"]["pid"] == 101
    assert len(launches) == 1
    assert launches[0][0] == [sys.executable, "-m", "vaws_coordinator", "daemon", "--state-dir", str(state)]


def test_busy_daemon_keeps_work_and_upgrade_retries_after_cooldown(daemon):
    state, owner, current, loaded, calls, launches, clock, _ = daemon
    owner._active_requests = 1
    first = service.ensure_daemon(state)
    assert first.runtime[0]["loaded"]["pid"] == 100
    assert not owner._stopped.is_set() and not launches
    service.ensure_daemon(state)
    assert calls == ["ping", "ping", "restart_if_idle", "ping"]
    owner._active_requests = 0
    clock[0] += 61
    assert service.ensure_daemon(state).runtime[0]["loaded"]["version"] == "0.4.1"
    assert calls.count("restart_if_idle") == 2
    assert len(launches) == 1


@pytest.mark.parametrize("caller,running", [
    ("0.3.9", "0.4.0"), ("0.4.0", "0.4.0"),
    ("unversioned", "0.4.0"), ("0.4.1", "unversioned"),
])
def test_old_equal_or_unparseable_version_does_not_restart(daemon, caller, running):
    state, owner, current, loaded, calls, launches, *_ = daemon
    current["version"], loaded["version"] = caller, running
    client = service.ensure_daemon(state)
    assert client.runtime[0]["loaded"]["pid"] == 100
    assert calls == ["ping"]
    assert not owner._stopped.is_set() and not launches


def test_failed_restart_request_is_throttled_but_new_pid_is_rechecked(daemon):
    state, owner, current, loaded, calls, launches, clock, restart_error = daemon
    restart_error.append(RuntimeError("old daemon does not implement idle restart"))
    service.ensure_daemon(state)
    service.ensure_daemon(state)
    assert calls.count("restart_if_idle") == 1
    loaded["pid"] = 200
    assert service.ensure_daemon(state).runtime[0]["loaded"]["pid"] == 200
    assert calls.count("restart_if_idle") == 2
    assert not owner._stopped.is_set() and not launches
