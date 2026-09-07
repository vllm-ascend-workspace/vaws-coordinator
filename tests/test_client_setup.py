"""`scripts/vaws_client_setup.py` writes both stdio servers and nothing else."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "lib"), str(ROOT / "lib/vendor")]

from vaws_remote_dev import RemoteDevUnavailable

spec = importlib.util.spec_from_file_location("vaws_client_setup", ROOT / "scripts/vaws_client_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class ClientSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.remote_dev = self.root / "remote-dev"
        (self.remote_dev / "mcp").mkdir(parents=True)
        (self.remote_dev / "mcp/server.py").write_text("# stand-in remote-dev server\n")
        # A developer shell's remote-dev root must not leak into the expectations.
        patcher = mock.patch.dict("os.environ", {key: value for key, value in os.environ.items()
                                                 if key != "VAWS_REMOTE_DEV_ROOT"}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, client, **kwargs):
        kwargs.setdefault("remote_dev_root", self.remote_dev)
        kwargs.setdefault("kimi_config", self.root / "kimi.toml")
        return setup.configuration(client, self.project, **kwargs)

    def json_servers(self, client, relative):
        files = self.configure(client)
        return json.loads(files[self.project / relative])["mcpServers"]

    def toml_servers(self, client):
        files = self.configure(client)
        return tomllib.loads(files[self.project / ("." + client) / "config.toml"])["mcp_servers"]

    def test_every_client_gets_both_servers_the_task_server_from_this_checkout(self):
        expected_task = [sys.executable, [str(ROOT / "task_server.py")]]
        expected_remote = [sys.executable, [str(self.remote_dev / "mcp/server.py")]]
        for client, relative in (("claude", ".mcp.json"), ("cursor", ".cursor/mcp.json"), ("kimi", ".kimi-code/mcp.json")):
            with self.subTest(client=client):
                servers = self.json_servers(client, relative)
                self.assertEqual(set(servers), {"remote-dev", "vaws-task"})
                for entry in servers.values():
                    self.assertEqual(entry["type"], "stdio")
                    self.assertIn("timeout", entry)
                self.assertEqual([servers["vaws-task"]["command"], servers["vaws-task"]["args"]], expected_task)
                self.assertEqual([servers["remote-dev"]["command"], servers["remote-dev"]["args"]], expected_remote)
        for client in ("codex", "grok"):
            with self.subTest(client=client):
                servers = self.toml_servers(client)
                self.assertEqual(set(servers), {"remote_dev", "vaws_task"})
                self.assertEqual([servers["vaws_task"]["command"], servers["vaws_task"]["args"]], expected_task)
                self.assertEqual([servers["remote_dev"]["command"], servers["remote_dev"]["args"]], expected_remote)

    def test_existing_json_configuration_gains_the_task_server_and_keeps_everything_else(self):
        path = self.project / ".mcp.json"
        path.write_text(json.dumps({"mcpServers": {
            "remote-dev": {"command": "python3", "args": ["/old/remote-dev/mcp/server.py"], "type": "stdio", "timeout": 5},
            "vaws-coordinator": {"type": "http", "url": "http://127.0.0.1:8766/mcp",
                                 "headers": {"Authorization": "Bearer PRIVATE"}}}}))
        servers = self.json_servers("claude", ".mcp.json")
        # The HTTP manager entry is the user's private configuration and is untouched.
        self.assertEqual(servers["vaws-coordinator"]["headers"], {"Authorization": "Bearer PRIVATE"})
        # Our own remote-dev entry is repointed; a user-chosen timeout survives.
        self.assertEqual(servers["remote-dev"]["args"], [str(self.remote_dev / "mcp/server.py")])
        self.assertEqual(servers["remote-dev"]["timeout"], 5)
        self.assertEqual(servers["vaws-task"]["args"], [str(ROOT / "task_server.py")])
        # Re-running on the written result is a no-op, so `--apply` twice
        # writes nothing the second time.
        files = self.configure("claude")
        path.write_text(files[path])
        self.assertEqual(self.configure("claude")[path], files[path])

    def test_hooks_and_permissions_are_preserved_and_no_trust_is_granted(self):
        settings = self.project / ".claude/settings.local.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"permissions": {"deny": ["Bash(ssh *)"]},
                                        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "my-hook"}]}]}}))
        files = self.configure("claude")
        value = json.loads(files[settings])
        self.assertEqual(value["permissions"], {"deny": ["Bash(ssh *)"]})
        self.assertEqual(len(value["hooks"]["SessionStart"]), 2)
        self.assertNotIn("allow", value["permissions"])
        self.assertNotIn("enableAllProjectMcpServers", value)
        for content in files.values():
            self.assertNotIn("Bearer", content)

    def test_toml_never_replaces_an_existing_server_and_adds_only_the_missing_one(self):
        config = self.project / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text('[mcp_servers.remote_dev]\ncommand = "user-python"\nargs = ["/user/remote-dev/mcp/server.py"]\n'
                          '\n[mcp_servers.vaws_coordinator]\nurl = "http://127.0.0.1:8766/mcp"\n'
                          'bearer_token_env_var = "VAWS_COORDINATOR_TOKEN"\n')
        servers = self.toml_servers("codex")
        self.assertEqual(servers["remote_dev"]["command"], "user-python")
        self.assertEqual(servers["vaws_coordinator"]["bearer_token_env_var"], "VAWS_COORDINATOR_TOKEN")
        self.assertEqual(servers["vaws_task"]["args"], [str(ROOT / "task_server.py")])
        text = self.configure("codex")[config]
        self.assertIn("# BEGIN VAWS vaws-task\n", text)
        self.assertNotIn("# BEGIN VAWS remote-dev\n", text)
        # Once both exist the file is not rewritten at all.
        config.write_text(text)
        self.assertNotIn(config, self.configure("codex"))

    def test_task_only_configures_the_local_server_without_a_remote_dev_checkout(self):
        with self.assertRaisesRegex(RemoteDevUnavailable, "--task-only"):
            setup.configuration("claude", self.project, remote_dev_root=None)
        servers = json.loads(setup.configuration("claude", self.project, task_only=True)[self.project / ".mcp.json"])["mcpServers"]
        self.assertEqual(list(servers), ["vaws-task"])
        servers = tomllib.loads(setup.configuration("grok", self.project, task_only=True)[self.project / ".grok/config.toml"])
        self.assertEqual(list(servers["mcp_servers"]), ["vaws_task"])

    def test_the_task_server_entry_launches_a_file_that_exists_in_this_checkout(self):
        self.assertTrue(setup.task_server().is_file())
        with mock.patch.object(setup, "TASK_SERVER", self.root / "missing.py"):
            with self.assertRaisesRegex(FileNotFoundError, "task server not found"):
                setup.mcp_servers(self.remote_dev)


if __name__ == "__main__":
    unittest.main()
