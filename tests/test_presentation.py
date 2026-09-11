import json
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
