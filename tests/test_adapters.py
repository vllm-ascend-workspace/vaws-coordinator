"""Contracts of the injected dependencies this repository does not own."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "lib"), str(ROOT / "lib/vendor"), str(ROOT)]

from vaws_git_sources import discover_repo_tree, iter_postorder
from vaws_host_queue import HostQueue, HostQueueUnavailable, load_host_protocol
from vaws_machine_directory import MachineDirectory, MachineDirectoryUnavailable
from vaws_ops import TOOL_DESCRIPTIONS, TOOL_SCHEMAS, vaws_call
from vaws_remote_dev import RemoteDevShell, RemoteDevUnavailable
from vaws_result import SCHEMA_VERSION, make_result

HOST_MODULE = '''
import json


class CoordinationError(ValueError):
    pass


def handle_request(request):
    return {"status": "ok", "echo": request}
'''


def write_remote_dev(root: Path, *, endpoint_symbol="direct_endpoint") -> Path:
    core = root / "core"
    core.mkdir(parents=True)
    (core / "__init__.py").write_text("")
    (core / "endpoint.py").write_text(f"def {endpoint_symbol}(mapping):\n    return dict(mapping)\n")
    (core / "shell_ops.py").write_text(
        "CALLS = []\n"
        "def remote_bash(endpoint, *, command, timeout_ms=None, runtime_env=None, **rest):\n"
        "    CALLS.append((endpoint, command, timeout_ms, runtime_env))\n"
        "    return {'result': {'outcome': 'success', 'refs': {'stdout': ''}}}\n"
    )
    (core / "managed_jobs.py").write_text("# vaws managed job supervisor\n")
    return root


class RemoteDevAdapterTests(unittest.TestCase):
    def tearDown(self):
        for name in [key for key in sys.modules if key == "core" or key.startswith("core.")]:
            del sys.modules[name]

    def test_missing_configuration_fails_closed_without_a_local_fallback(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RemoteDevUnavailable, "VAWS_REMOTE_DEV_ROOT"):
                RemoteDevShell().worker_source("managed_jobs")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RemoteDevUnavailable, "does not look like"):
                RemoteDevShell(tmp).worker_source("managed_jobs")

    def test_calls_are_endpoint_explicit_and_never_alias_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            shell = RemoteDevShell(write_remote_dev(Path(tmp)))
            for target in ({"alias": "runtime-a"}, {"host": "runtime.invalid"}, {"port": 22}):
                with self.subTest(target=target), self.assertRaisesRegex(ValueError, "explicit host and port"):
                    shell.endpoint(target)
            result = shell.run({"host": "runtime.invalid", "port": 46010, "user": "root",
                                "root": "/vllm-workspace", "cwd": "/vllm-workspace"}, "true")
            self.assertEqual(result["outcome"], "success")
            calls = sys.modules["core.shell_ops"].CALLS
            self.assertEqual(calls[0][0]["host"], "runtime.invalid")
            self.assertEqual((calls[0][2], calls[0][3]), (45000, False))

    def test_supervisor_source_comes_from_the_remote_dev_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            shell = RemoteDevShell(write_remote_dev(Path(tmp)))
            self.assertIn("managed job supervisor", shell.worker_source("managed_jobs"))
            (Path(tmp) / "core/managed_jobs.py").unlink()
            RemoteDevShell(Path(tmp)).worker_source  # unresolved until called
            with self.assertRaisesRegex(RemoteDevUnavailable, "missing core/managed_jobs.py"):
                RemoteDevShell(Path(tmp)).worker_source("managed_jobs")

    def test_legacy_resolve_endpoint_name_is_still_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            shell = RemoteDevShell(write_remote_dev(Path(tmp), endpoint_symbol="resolve_endpoint"))
            self.assertEqual(shell.endpoint({"host": "runtime.invalid", "port": 22})["port"], 22)

    def test_result_factory_prefers_remote_dev_and_falls_back_to_the_mirror(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIs(RemoteDevShell().result_factory(), make_result)
        with tempfile.TemporaryDirectory() as tmp:
            root = write_remote_dev(Path(tmp))
            (root / "core/result.py").write_text(
                "def make_result(**kwargs):\n    return {'schema_version': 'remote-dev.result.v1', **kwargs}\n")
            self.assertIsNot(RemoteDevShell(root).result_factory(), make_result)


class HostQueueAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.module = Path(self.temp.name) / "vaws_npu_coordination.py"
        self.module.write_text(HOST_MODULE)
        self.addCleanup(self.temp.cleanup)

    def test_unconfigured_authority_fails_closed(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(HostQueueUnavailable, "VAWS_HOST_QUEUE_MODULE"):
                HostQueue(lambda target, command: "").request({"host": "h", "port": 22}, {"action": "status"})

    def test_request_ships_the_configured_module_and_pins_the_host_root(self):
        seen = {}

        def run(target, command):
            seen.update(target=target, command=command)
            return json.dumps({"status": "ok", "tasks": []})

        queue = HostQueue(run, module_path=self.module)
        self.assertEqual(queue.request({"host": "host.invalid", "port": 22, "user": "root"},
                                       {"action": "status"})["status"], "ok")
        self.assertEqual(seen["target"], {"host": "host.invalid", "port": 22, "user": "root",
                                          "root": "/", "cwd": "/"})
        self.assertIn("def handle_request(request)", seen["command"])
        self.assertIn('{"action":"status"}', seen["command"])

    def test_unresolved_host_answers_and_transport_failures_never_look_granted(self):
        for status in ("failed", "needs_input", "probe_failed"):
            queue = HostQueue(lambda target, command: json.dumps({"status": status, "error": "no"}),
                              module_path=self.module)
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, "no"):
                queue.request({"host": "host.invalid", "port": 22}, {"action": "acquire"})

        def broken(target, command):
            raise RuntimeError("runtime probe failed (failed/nonzero_exit, exit 255): ssh closed")

        with self.assertRaisesRegex(RuntimeError, "host coordination failed"):
            HostQueue(broken, module_path=self.module).request({"host": "host.invalid", "port": 22},
                                                              {"action": "release"})

    def test_the_host_protocol_loads_under_its_canonical_module_name(self):
        module = load_host_protocol(self.module)
        self.assertEqual(module.handle_request({"action": "status"})["status"], "ok")


class MachineDirectoryTests(unittest.TestCase):
    def test_unconfigured_directory_fails_closed_instead_of_an_empty_fleet(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(MachineDirectoryUnavailable, "VAWS_MACHINE_INVENTORY"):
                MachineDirectory().catalog()

    def test_catalog_and_unique_alias_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "machine-inventory.json"
            path.write_text(json.dumps({"machines": [
                {"alias": "alpha", "host": {"ip": "host-a.invalid", "port": 22, "user": "root"},
                 "container": {"name": "prepared-a", "ssh_port": 46010}},
                {"alias": "beta", "host": {"ip": "host-b.invalid"}, "container": {}},
                {"alias": "beta", "host": {"ip": "host-c.invalid"}, "container": {}}]}))
            directory = MachineDirectory(path)
            catalog = directory.catalog()
            self.assertEqual(catalog["inventory_path"], str(path.resolve()))
            self.assertEqual(catalog["machines"][0], {"alias": "alpha", "host": "host-a.invalid",
                                                      "container_name": "prepared-a", "container_port": 46010})
            self.assertEqual(directory.host("alpha")["port"], 22)
            with self.assertRaisesRegex(ValueError, "resolve uniquely"):
                directory.host("beta")
            with self.assertRaisesRegex(ValueError, "resolve uniquely"):
                directory.host("missing")


class GitSourceTests(unittest.TestCase):
    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def init(self, repo):
        repo.mkdir(parents=True)
        self.git(repo, "init")
        self.git(repo, "config", "user.name", "Test")
        self.git(repo, "config", "user.email", "test@example.invalid")
        (repo / "file.txt").write_text("content\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-m", "base")

    def test_missing_and_unpopulated_submodules_are_reported_before_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self.init(root)
            (root / ".gitmodules").write_text('[submodule "child"]\n path = nested/child\n url = ./child\n')
            self.git(root, "add", ".gitmodules")
            self.git(root, "commit", "-m", "declare")
            with self.assertRaisesRegex(RuntimeError, "missing"):
                discover_repo_tree(root, ".")
            (root / "nested/child").mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "not a populated Git worktree"):
                discover_repo_tree(root, ".")

    def test_children_are_visited_before_their_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self.init(root)
            (root / ".gitmodules").write_text('[submodule "child"]\n path = nested/child\n url = ./child\n')
            self.init(root / "nested/child")
            order = [node.relpath for node in iter_postorder(discover_repo_tree(root, "."))]
            self.assertEqual(order, ["nested/child", "."])


class VendoredContractTests(unittest.TestCase):
    def test_the_vendored_run_manifest_matches_its_recorded_upstream_digest(self):
        record = json.loads((ROOT / "lib/vendor/UPSTREAM.json").read_text())
        vendored = ROOT / "lib/vendor" / record["file"]
        digest = hashlib.sha256(vendored.read_bytes()).hexdigest()
        self.assertEqual(digest, record["sha256"],
                         "Run Manifest v1 stays scaffold-owned: update the upstream record "
                         "and both repositories together, never only this copy.")

    def test_result_envelope_mirrors_the_remote_dev_contract(self):
        result = make_result(tool="vaws.session", target={"kind": "vaws-task"}, outcome="success",
                             status="open", summary="VAWS open")
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertLessEqual({"tool", "invocation_id", "target", "outcome", "status", "summary",
                              "started_at", "duration_ms", "preview", "refs", "artifacts",
                              "changed_files", "warnings", "next"}, set(result))

    def test_task_tools_publish_their_own_descriptions_and_schemas(self):
        self.assertEqual(set(TOOL_DESCRIPTIONS), set(TOOL_SCHEMAS))
        self.assertEqual(set(TOOL_SCHEMAS), {"vaws.session", "vaws.run", "vaws.execution", "vaws.finish"})
        for name, schema in TOOL_SCHEMAS.items():
            with self.subTest(name=name):
                self.assertIn("context_file", schema["properties"])
                self.assertFalse(schema["additionalProperties"])

    def test_an_unavailable_task_registry_is_blocked_and_never_a_remote_success(self):
        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return {"outcome": kwargs["outcome"]}

        with mock.patch.dict("os.environ", {}, clear=True):
            payload = vaws_call("vaws.session", {}, make_result=factory)
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(captured["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
