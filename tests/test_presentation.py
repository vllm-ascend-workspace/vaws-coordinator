import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from remote_dev.result import make_result
from vaws_coordinator.presentation import present


def test_large_history_and_duplicate_envs_are_bounded_and_readable(tmp_path):
    value = {"session": {"id": "task-1", "state": "open"}, "executions": [
        {"id": str(i), "phase": "failed", "error": "ERROR " * 2000,
         "binding": {"launch_env": {"PATH": "a" * 5000}}, "roles": []} for i in range(100)]}
    result = make_result(tool="vaws.session", target={}, outcome="success", status="open", summary="VAWS open", extra={"data": value})
    compact = present(result, tmp_path)
    assert len(json.dumps(compact).encode()) < 16000
    assert compact["data"]["executions_total"] == 100
    assert len(compact["data"]["executions"]) == 5
    assert json.loads(Path(compact["record_ref"]).read_text())["data"] == value
    assert "binding" not in json.dumps(compact)


def test_explicit_target_is_exact_or_omitted_never_silently_truncated(tmp_path):
    target = {"launch_preamble": "x" * 3000}
    result = make_result(tool="vaws.execution", target={}, outcome="success", status="running", summary="VAWS running", extra={"data": {"execution_id": "e", "state": "running", "target": target}})
    compact = present(result, tmp_path, target=True)
    assert compact["data"]["target"] == target


def test_full_mode_and_write_failure_preserve_operation_result(tmp_path):
    value = {"state": "running", "execution_id": "e", "private_field": "full"}
    result = make_result(tool="vaws.run", target={}, outcome="success", status="running", summary="VAWS running", extra={"data": value})
    assert present(result, tmp_path, full=True)["data"] == value
    with patch.object(Path, "open", side_effect=OSError("disk full")):
        reply = present(result, tmp_path)
    assert reply["outcome"] == "success"
    assert reply["data"] == value
    assert "record_ref" not in reply
    assert "disk full" in reply["warnings"][-1]


def test_healthy_runtime_is_small_but_full_and_unhealthy_facts_remain_available(tmp_path):
    packages = {"vaws-coordinator": "0.4.0", "vaws-remote-dev": "0.7.0"}
    runtime = {scope: [{"status": "current", "loaded": {"package": name, "version": version,
                        "location": "/workspace/" * 40, "python": "/workspace/python", "pid": index},
                       "installed": {"package": name, "version": version, "location": "/workspace/" * 40}}
                      for index, (name, version) in enumerate(packages.items())]
               for scope in ("client", "daemon")}

    def result(facts):
        return make_result(tool="vaws.execution", target={}, outcome="success", status="preparing", summary="VAWS preparing",
                           extra={"runtime": facts, "data": {"execution_id": "e", "state": "preparing", "target": {"launch_preamble": "exact"}}})

    compact = present(result(runtime), tmp_path)
    assert compact["runtime"] == {"status": "current", "packages": packages}
    assert len(json.dumps(compact["runtime"])) < len(json.dumps(runtime)) / 10
    assert json.loads(Path(compact["record_ref"]).read_text())["runtime"] == runtime
    assert present(result(runtime), tmp_path, full=True)["runtime"] == runtime
    assert present(result(runtime), tmp_path, target=True)["runtime"] == runtime
    unhealthy = deepcopy(runtime)
    unhealthy["daemon"][0].update(status="restart_required", error="daemon still has the old package loaded")
    preserved = present(result(unhealthy), tmp_path)
    assert preserved["runtime"] == unhealthy
