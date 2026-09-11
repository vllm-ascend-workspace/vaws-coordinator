"""Status caching is observation only; explicit refresh and ownership stay intact."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import time
from unittest import mock

import pytest
import test_coordinator as fixtures
from vaws_coordinator.ops import vaws_call
from vaws_coordinator.service import CoordinatorClient, STATUS_CACHE_SECONDS


@pytest.fixture
def task():
    value = fixtures.TaskClientTests("runTest")
    value.setUp()
    try:
        yield value
    finally:
        value.tearDown()


def test_cached_status_avoids_remote_control_and_expiration_refreshes(task):
    execution = task.client.run("true")["execution_id"]
    with mock.patch.object(task.pool, "managed_control", wraps=task.pool.managed_control) as control:
        first = task.client.observe(execution, refresh=False)
        assert control.call_count == 1
        second = task.client.observe(execution, refresh=False)
        assert control.call_count == 1
        assert first["state"] == second["state"] == "running"
        assert second["observation_freshness"]["source"] == "cache"
        assert second["observation_freshness"]["fresh"] is True
        assert second["resources_released"] is False
        with task.store.transaction() as db:
            row = task.store.get(db, "execution", execution)
        row["jobs_observed_at"] = time.time() - STATUS_CACHE_SECONDS - 1
        task.store.save_execution(row)
        expired = task.client.observe(execution, refresh=False)
        assert control.call_count == 2
        assert expired["observation_freshness"]["source"] == "refreshed"
        assert expired["roles"][0]["status_observed_at"] is not None
        forced = task.client.observe(execution, refresh=True)
        assert control.call_count == 3
        assert forced["observation_freshness"]["refresh_requested"] is True
        # Preserve the library's existing fresh-by-default contract.
        task.client.observe(execution)
        assert control.call_count == 4


def test_busy_refresh_returns_age_and_deferred_without_waiting_for_execution(task):
    execution = task.client.run("true")["execution_id"]
    first = task.client.observe(execution)
    lock = task.client.coordinator._lock_for("execution", execution)
    with ThreadPoolExecutor(max_workers=1) as executor:
        lock.acquire()
        try:
            with mock.patch.object(task.pool, "managed_control", side_effect=AssertionError("busy query must not probe")):
                future = executor.submit(task.client.observe, execution, refresh=True)
                result = future.result(timeout=2)
        finally:
            lock.release()
    freshness = result["observation_freshness"]
    assert freshness["refresh_deferred"] is True
    assert freshness["snapshot_completed_at"] == first["observation_freshness"]["snapshot_completed_at"]
    assert freshness["age_seconds"] >= 0
    assert result["state"] == "running"


def test_task_tool_defaults_to_cache_and_preserves_freshness_in_compact_output(task):
    execution = task.client.run("true")["execution_id"]
    task.client.observe(execution)
    with mock.patch("vaws_coordinator.task_client.TaskClient", return_value=task.client):
        with mock.patch.object(task.pool, "managed_control", wraps=task.pool.managed_control) as control:
            cached = vaws_call("vaws.execution", {"execution_id": execution})["result"]
            assert control.call_count == 0
            assert cached["data"]["observation_freshness"]["source"] == "cache"
            fresh = vaws_call("vaws.execution", {"execution_id": execution, "refresh": True})["result"]
            assert control.call_count == 1
            assert fresh["data"]["observation_freshness"]["source"] == "refreshed"


def test_cached_status_still_rejects_another_task_before_remote_control(task):
    from vaws_coordinator.task_client import TaskClient
    execution = task.client.run("true")["execution_id"]
    task.client.observe(execution)
    foreign = task.store.attach("codex", "another-native-task", str(task.root))
    other = TaskClient(foreign["context_file"], user="alice", service=task.client.coordinator)
    with mock.patch.object(task.pool, "managed_control", side_effect=AssertionError("must not probe")):
        with pytest.raises(ValueError, match="another VAWS task"):
            other.observe(execution, refresh=False)


def test_ipc_and_cli_forward_explicit_refresh_without_overwriting_json(task):
    client = CoordinatorClient(task.root)
    with mock.patch.object(client, "call") as call:
        client.advance(task.root, "alice", "a" * 64, refresh=False)
    assert call.call_args.kwargs["refresh"] is False
    from vaws_coordinator import vaws
    for options in (["--refresh"], ["--json", '{"refresh":true}']):
        with mock.patch("sys.argv", ["vaws", "execution", "--execution-id", "a" * 64, *options]):
            with mock.patch.object(vaws, "vaws_call", return_value={"result": {"outcome": "success"}}) as call:
                with contextlib.redirect_stdout(io.StringIO()):
                    assert vaws.main() == 0
        assert call.call_args.args[1]["refresh"] is True
