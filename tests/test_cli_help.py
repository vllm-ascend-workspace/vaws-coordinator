from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CliHelpTests(unittest.TestCase):
    def test_task_facade_uses_one_cli_without_endpoint_or_network_requirements(self):
        script = ROOT / "scripts/vaws.py"
        for args in (["--help"], ["attach", "--help"], ["session", "--help"],
                     ["run", "--help"], ["execution", "--help"], ["finish", "--help"]):
            with self.subTest(args=args):
                proc = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("usage:", proc.stdout)

    def test_json_arguments_are_not_overridden_by_argparse_defaults(self) -> None:
        code = """
import importlib.util, json, sys
from unittest import mock
spec = importlib.util.spec_from_file_location("vaws_cli", r"%s")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
captured = {}
def fake(name, args, **kwargs):
    captured.update(args)
    return {"result": {"outcome": "success"}}
argv = ["vaws.py", "execution", "--execution-id", "e1", "--json", json.dumps({"action": "stop"})]
with mock.patch.object(module, "vaws_call", side_effect=fake), mock.patch.object(sys, "argv", argv):
    module.main()
print(json.dumps(captured))
""" % (ROOT / "scripts/vaws.py")
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        merged = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(merged["action"], "stop")

    def test_vaws_cli_bad_json_returns_result_contract_without_traceback(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts/vaws.py"), "session", "--json", "{not json"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"]["schema_version"], "remote-dev.result.v1")
        self.assertEqual(payload["result"]["tool"], "vaws.session")
        self.assertEqual(payload["result"]["status"], "invalid_json")
        self.assertEqual(payload["result"]["outcome"], "needs_input")

    def test_vaws_cli_attach_error_returns_result_contract_without_traceback(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/vaws.py"),
                "attach",
                "--client",
                "kimi",
                "--native-session-id",
                "native-test",
                "--parent-context",
                "/nonexistent/vaws-agent-context.json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"]["schema_version"], "remote-dev.result.v1")
        self.assertEqual(payload["result"]["tool"], "vaws.attach")
        self.assertEqual(payload["result"]["status"], "attach_failed")
        self.assertEqual(payload["result"]["outcome"], "failed")


if __name__ == "__main__":
    unittest.main()
