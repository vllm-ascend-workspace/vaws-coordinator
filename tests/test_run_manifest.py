"""Run Manifest v1 and the ready_runtime → validate_manifest round trip."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vaws_coordinator.code_identity import code_identity, manifest_code
from vaws_coordinator.ready_runtime import RuntimePool
from vaws_coordinator.run_manifest import (
    GIT_SHA_RE,
    RunManifestError,
    add_artifact,
    load_manifest,
    new_manifest,
    transition_status,
    validate_manifest,
    write_manifest,
)

NOW = "2026-09-09T00:00:00Z"


class RunManifestTests(unittest.TestCase):
    def test_new_manifest_requires_git_code_identity(self) -> None:
        manifest = new_manifest(run_type="debug", run_id="debug-case-1", created_at=NOW)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertRegex(manifest["code"]["source_head"], GIT_SHA_RE.pattern)
        self.assertRegex(manifest["code"]["snapshot_commit"], GIT_SHA_RE.pattern)
        self.assertIs(manifest["code"]["dirty"], False)
        validate_manifest(manifest)

    def test_missing_code_is_rejected(self) -> None:
        manifest = new_manifest(run_type="debug", run_id="debug-case-2", created_at=NOW)
        del manifest["code"]
        with self.assertRaisesRegex(RunManifestError, "missing top-level fields: code"):
            validate_manifest(manifest)

    def test_round_trip_and_status_transition(self) -> None:
        manifest = new_manifest(
            run_type="correctness",
            run_id="correctness-case-1",
            command=["python", "run.py"],
            created_at=NOW,
        )
        running = transition_status(manifest, "running", updated_at=NOW)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            write_manifest(path, running)
            self.assertEqual(load_manifest(path), running)

    def test_secret_like_environment_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(RunManifestError, "secret-like key"):
            new_manifest(
                run_type="profiling",
                run_id="profile-case-1",
                environment_variables={"SERVICE_API_TOKEN": "do-not-store"},
                created_at=NOW,
            )

    def test_duplicate_artifact_name_is_rejected(self) -> None:
        manifest = new_manifest(
            run_type="performance", run_id="perf-case-1", created_at=NOW
        )
        manifest = add_artifact(
            manifest, name="report", kind="report", uri="report.md", updated_at=NOW
        )
        with self.assertRaisesRegex(RunManifestError, "duplicated"):
            add_artifact(
                manifest, name="report", kind="raw", uri="raw.json", updated_at=NOW
            )


class ReadyRuntimeManifestRoundTripTests(unittest.TestCase):
    def test_export_manifest_validates_against_run_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool = RuntimePool(Path(tmp), backend=object())
            run = {
                "id": "testrun1",
                "created_at": NOW,
                "state": "pending",
                "intent": {
                    "snapshots": {
                        "vllm": "a" * 40,
                        "vllm-ascend": "b" * 40,
                    }
                },
                "task_id": "pool-testrun1",
                "epoch": None,
            }
            binding = {
                "profile_key": "profile-a",
                "build_key": "native-a",
                "endpoint": {"host": "host.invalid", "port": 22, "root": "/vllm-workspace"},
                "runtime_id": "runtime-a",
            }
            pool.export_manifest(run, binding)
            path = Path(tmp) / "runs" / "testrun1.json"
            manifest = load_manifest(path)
            validate_manifest(manifest)
            self.assertEqual(manifest["run_type"], "debug")
            self.assertEqual(manifest["status"], "planned")
            self.assertIn("code", manifest)
            self.assertRegex(manifest["code"]["source_head"], GIT_SHA_RE.pattern)
            self.assertEqual(
                manifest["workspace_snapshot"],
                {"vllm": "a" * 40, "vllm-ascend": "b" * 40},
            )
            self.assertEqual(
                manifest["environment"]["coordination"]["state"], "pending"
            )
            self.assertEqual(manifest["run_id"], "pool-testrun1")


class CodeIdentityTests(unittest.TestCase):
    def _run(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def _init(self, path: Path) -> str:
        self._run(path, "init")
        self._run(path, "config", "user.email", "identity@example.invalid")
        self._run(path, "config", "user.name", "Identity Test")
        (path / "README").write_text("base\n", encoding="utf-8")
        self._run(path, "add", "README")
        self._run(path, "commit", "-m", "init")
        return self._run(path, "rev-parse", "HEAD")

    def test_clean_tree_snapshot_equals_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source_head = self._init(repo)
            identity = code_identity(repo)
            self.assertFalse(identity["dirty"])
            self.assertEqual(identity["source_head"], source_head)
            self.assertEqual(identity["snapshot_commit"], source_head)
            self.assertEqual(manifest_code(repo)["snapshot_commit"], source_head)

    def test_dirty_tree_snapshot_is_a_git_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source_head = self._init(repo)
            (repo / "dirty.txt").write_text("changed\n", encoding="utf-8")
            identity = code_identity(repo)
            self.assertTrue(identity["dirty"])
            self.assertEqual(identity["source_head"], source_head)
            self.assertRegex(identity["snapshot_commit"], GIT_SHA_RE.pattern)
            self.assertNotEqual(identity["snapshot_commit"], source_head)


class ParityCommandTests(unittest.TestCase):
    def test_materialize_command_uses_the_in_package_module(self) -> None:
        from vaws_coordinator.parity import materialize_command

        command = materialize_command(
            workspace_id="ws",
            runtime_id="rt",
            endpoint={"host": "h", "port": 22, "user": "root", "root": "/vllm-workspace"},
            sources={"vllm": "/tmp/vllm", "vllm-ascend": "/tmp/vllm-ascend"},
            workspace_root="/tmp/workspace",
        )
        self.assertEqual(command[1:4], ["-m", "vaws_coordinator.parity", "sync"])
        self.assertIn("--workspace-root", command)
        self.assertEqual(command[command.index("--workspace-root") + 1], "/tmp/workspace")


class HostStateDirTests(unittest.TestCase):
    def test_host_state_dir_is_configurable(self) -> None:
        from vaws_coordinator.host.vaws_npu_coordination import (
            DEFAULT_STATE_DIR,
            resolve_host_state_dir,
        )

        self.assertEqual(resolve_host_state_dir(), DEFAULT_STATE_DIR)
        self.assertEqual(resolve_host_state_dir("/explicit"), "/explicit")
        with mock.patch.dict(
            "os.environ", {"VAWS_NPU_COORDINATOR_STATE_DIR": "/tmp/custom-host-state"}
        ):
            self.assertEqual(resolve_host_state_dir(), "/tmp/custom-host-state")


if __name__ == "__main__":
    unittest.main()
