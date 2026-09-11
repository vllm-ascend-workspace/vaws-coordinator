"""Run Manifest v1 and the ready_runtime → validate_manifest round trip."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vaws_coordinator.code_identity import (
    CodeIdentityError,
    code_identity,
    identity_workspace,
    manifest_code,
)
from vaws_coordinator.ready_runtime import RuntimePool
from vaws_coordinator.run_manifest import (
    GIT_SHA_RE,
    ZERO_SHA,
    RunManifestError,
    add_artifact,
    load_manifest,
    new_manifest,
    transition_status,
    validate_manifest,
    write_manifest,
)

NOW = "2026-09-09T00:00:00Z"
CODE = {
    "source_head": "a" * 40,
    "snapshot_commit": "b" * 40,
    "dirty": False,
}


def _run(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _run(path, "init")
    _run(path, "config", "user.email", "identity@example.invalid")
    _run(path, "config", "user.name", "Identity Test")
    (path / "README").write_text("base\n", encoding="utf-8")
    _run(path, "add", "README")
    _run(path, "commit", "-m", "init")
    return _run(path, "rev-parse", "HEAD")


class RunManifestTests(unittest.TestCase):
    def test_new_manifest_requires_git_code_identity(self) -> None:
        manifest = new_manifest(
            run_type="debug", run_id="debug-case-1", created_at=NOW, code=CODE
        )
        self.assertEqual(manifest["schema_version"], 1)
        self.assertRegex(manifest["code"]["source_head"], GIT_SHA_RE.pattern)
        self.assertRegex(manifest["code"]["snapshot_commit"], GIT_SHA_RE.pattern)
        self.assertIs(manifest["code"]["dirty"], False)
        validate_manifest(manifest)

    def test_missing_code_is_rejected(self) -> None:
        manifest = new_manifest(
            run_type="debug", run_id="debug-case-2", created_at=NOW, code=CODE
        )
        del manifest["code"]
        with self.assertRaisesRegex(RunManifestError, "missing top-level fields: code"):
            validate_manifest(manifest)

    def test_construction_without_identity_is_refused(self) -> None:
        with self.assertRaisesRegex(RunManifestError, "code identity is required"):
            new_manifest(run_type="debug", run_id="debug-case-3", created_at=NOW)

    def test_zero_sentinel_is_refused(self) -> None:
        zeros = {
            "source_head": ZERO_SHA,
            "snapshot_commit": ZERO_SHA,
            "dirty": False,
        }
        with self.assertRaisesRegex(RunManifestError, "all-zero sentinel"):
            new_manifest(
                run_type="debug", run_id="debug-case-4", created_at=NOW, code=zeros
            )
        manifest = new_manifest(
            run_type="debug", run_id="debug-case-5", created_at=NOW, code=CODE
        )
        manifest["code"] = zeros
        with self.assertRaisesRegex(RunManifestError, "all-zero sentinel"):
            validate_manifest(manifest)

    def test_round_trip_and_status_transition(self) -> None:
        manifest = new_manifest(
            run_type="correctness",
            run_id="correctness-case-1",
            command=["python", "run.py"],
            created_at=NOW,
            code=CODE,
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
                code=CODE,
            )

    def test_duplicate_artifact_name_is_rejected(self) -> None:
        manifest = new_manifest(
            run_type="performance", run_id="perf-case-1", created_at=NOW, code=CODE
        )
        manifest = add_artifact(
            manifest, name="report", kind="report", uri="report.md", updated_at=NOW
        )
        with self.assertRaisesRegex(RunManifestError, "duplicated"):
            add_artifact(
                manifest, name="report", kind="raw", uri="raw.json", updated_at=NOW
            )


class ReadyRuntimeManifestRoundTripTests(unittest.TestCase):
    def test_export_manifest_writes_nonzero_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            head = _init_repo(workspace)
            vllm = workspace / "vllm"
            ascend = workspace / "vllm-ascend"
            vllm.mkdir()
            ascend.mkdir()
            pool = RuntimePool(Path(tmp) / "state", backend=object())
            session = pool.session_open(
                "owner1",
                "sid1",
                {"vllm": str(vllm.resolve()), "vllm-ascend": str(ascend.resolve())},
            )
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
                "intent": {"session": session["id"]},
            }
            pool.export_manifest(run, binding)
            path = Path(tmp) / "state" / "runs" / "testrun1.json"
            manifest = load_manifest(path)
            validate_manifest(manifest)
            self.assertEqual(manifest["run_type"], "debug")
            self.assertEqual(manifest["status"], "planned")
            self.assertEqual(manifest["code"]["source_head"], head)
            self.assertEqual(manifest["code"]["snapshot_commit"], head)
            self.assertNotEqual(manifest["code"]["source_head"], ZERO_SHA)
            self.assertNotEqual(manifest["code"]["snapshot_commit"], ZERO_SHA)
            self.assertEqual(
                manifest["workspace_snapshot"],
                {"vllm": "a" * 40, "vllm-ascend": "b" * 40},
            )
            self.assertEqual(
                manifest["environment"]["coordination"]["state"], "pending"
            )
            self.assertEqual(manifest["run_id"], "pool-testrun1")

    def test_export_manifest_without_session_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool = RuntimePool(Path(tmp), backend=object())
            run = {
                "id": "testrun1",
                "created_at": NOW,
                "state": "pending",
                "intent": {"snapshots": {}},
                "task_id": "pool-testrun1",
                "epoch": None,
            }
            binding = {
                "profile_key": "profile-a",
                "build_key": "native-a",
                "endpoint": {"host": "host.invalid", "port": 22, "root": "/vllm-workspace"},
                "runtime_id": "runtime-a",
            }
            with self.assertRaisesRegex(ValueError, "no session"):
                pool.export_manifest(run, binding)


class IdentityWorkspaceTests(unittest.TestCase):
    def test_multiple_sources_use_the_containing_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_repo(workspace)
            vllm = workspace / "vllm"
            ascend = workspace / "vllm-ascend"
            vllm.mkdir()
            ascend.mkdir()
            chosen = identity_workspace(
                {"vllm": str(vllm), "vllm-ascend": str(ascend)}
            )
            self.assertEqual(chosen, workspace.resolve())

    def test_unrelated_sources_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            left = Path(tmp) / "left"
            right = Path(tmp) / "right"
            _init_repo(left)
            _init_repo(right)
            with self.assertRaisesRegex(CodeIdentityError, "no common containing"):
                identity_workspace({"vllm": str(left), "vllm-ascend": str(right)})


class CodeIdentityTests(unittest.TestCase):
    def test_clean_tree_snapshot_equals_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source_head = _init_repo(repo)
            identity = code_identity(repo)
            self.assertFalse(identity["dirty"])
            self.assertEqual(identity["source_head"], source_head)
            self.assertEqual(identity["snapshot_commit"], source_head)
            self.assertEqual(manifest_code(repo)["snapshot_commit"], source_head)

    def test_dirty_tree_snapshot_is_a_git_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source_head = _init_repo(repo)
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
        self.assertIn("--source", command)
        self.assertIn("vllm=/tmp/vllm", command)
        self.assertIn("vllm-ascend=/tmp/vllm-ascend", command)
        self.assertIn("--workspace-root", command)
        self.assertEqual(command[command.index("--workspace-root") + 1], str(Path("/tmp/workspace")))
        without_root = materialize_command(
            workspace_id="ws", runtime_id="rt",
            endpoint={"host": "h", "port": 22, "user": "root", "root": "/vllm-workspace"},
            sources={"vllm": "/tmp/vllm", "vllm-ascend": "/tmp/vllm-ascend"},
        )
        self.assertNotIn("--workspace-root", without_root)


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
