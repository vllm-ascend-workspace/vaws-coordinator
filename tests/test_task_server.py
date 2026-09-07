"""The stdio task server: listing, dispatch, capability, and offline degradation."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "lib"), str(ROOT / "lib/vendor"), str(ROOT)]

import task_server
from task_server import (ALIASES, SERVICE_API_VERSION, SERVICE_NAME, StdioTransport, call_tool, handle,
                         initialize_result, list_tools, service_api_version)
from vaws_agent_session import AgentSessions
from vaws_ops import TOOL_DESCRIPTIONS, TOOL_SCHEMAS

# Environment keys that would let a developer's shell leak a manager, a
# remote-dev checkout or a task registry into these tests.
LEAKY = ("VAWS_COORDINATOR_URL", "VAWS_COORDINATOR_TOKEN", "VAWS_REMOTE_DEV_ROOT",
         "VAWS_AGENT_SESSIONS_DIR", "VAWS_CONTEXT_FILE")


def clean_environment(**extra):
    environment = {key: value for key, value in os.environ.items() if key not in LEAKY}
    environment.update(extra)
    return environment


def closed_loopback_port():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    return port


class TaskRegistry:
    """One private task registry with a real attachment and its context file."""

    def __init__(self, root: Path):
        self.state_dir = root / "agent-sessions"
        self.store = AgentSessions(self.state_dir)
        self.context = self.store.attach("codex", "native-test", str(root))
        self.context_file = self.context["context_file"]

    def configure_manager(self, url):
        token = self.state_dir.parent / "token"
        token.write_text("not-a-real-token")
        token.chmod(0o600)
        (self.state_dir.parent / "coordinator-client.json").write_text(json.dumps({"url": url, "token_file": str(token)}))


class CapabilityTests(unittest.TestCase):
    def test_tools_list_advertises_the_four_portable_names_with_their_own_schemas(self):
        tools = list_tools()
        self.assertEqual([tool["name"] for tool in tools], ["vaws_session", "vaws_run", "vaws_execution", "vaws_finish"])
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                canonical = ALIASES[tool["name"]]
                self.assertEqual(tool["description"], TOOL_DESCRIPTIONS[canonical])
                self.assertEqual(tool["inputSchema"], TOOL_SCHEMAS[canonical])
        self.assertEqual(handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"], {"tools": tools})

    def test_initialize_declares_the_service_api_version_in_the_experimental_capability(self):
        result = initialize_result({"protocolVersion": "2025-03-26", "capabilities": {}})
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        declared = result["capabilities"]["experimental"][SERVICE_NAME]
        self.assertEqual(declared["service_api_version"], SERVICE_API_VERSION)
        self.assertEqual(declared["tools"], sorted(ALIASES))
        self.assertEqual(result["serverInfo"]["service_api_version"], SERVICE_API_VERSION)
        self.assertEqual(result["serverInfo"]["name"], SERVICE_NAME)
        self.assertIn("tools", result["capabilities"])
        # An unknown requested protocol version gets the newest one we speak,
        # never an echo of an arbitrary string.
        self.assertEqual(initialize_result({"protocolVersion": "9999-01-01"})["protocolVersion"],
                         task_server.PROTOCOL_VERSIONS[-1])

    def test_a_server_that_declares_nothing_is_unknown_not_supported(self):
        self.assertEqual(service_api_version(initialize_result({})), SERVICE_API_VERSION)
        # The remote-dev server's initialize result, which used to carry these
        # tools and no longer does, declares no task capability at all.
        remote_dev_shaped = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}, "resources": {}},
                             "serverInfo": {"name": "remote-dev", "version": "0.1.0"}}
        self.assertIsNone(service_api_version(remote_dev_shaped))
        self.assertIsNone(service_api_version({}))
        self.assertIsNone(service_api_version({"capabilities": {"experimental": {SERVICE_NAME: {}}}}))

    def test_json_rpc_plumbing_notifications_ping_and_unknown_methods(self):
        self.assertIsNone(handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(handle({"jsonrpc": "2.0", "id": 7, "method": "ping"}), {"jsonrpc": "2.0", "id": 7, "result": {}})
        error = handle({"jsonrpc": "2.0", "id": 8, "method": "resources/list"})["error"]
        self.assertEqual(error["code"], -32601)
        error = handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "remote_bash", "arguments": {}}})["error"]
        self.assertEqual(error["code"], -32602)
        self.assertIn("unknown task tool", error["message"])
        error = handle({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "vaws_session", "arguments": []}})["error"]
        self.assertEqual(error["code"], -32000)
        self.assertEqual(handle({"jsonrpc": "2.0", "id": 11})["error"]["code"], -32600)

    def test_describe_is_the_offline_view_of_initialize_and_tools_list(self):
        proc = subprocess.run([sys.executable, str(ROOT / "task_server.py"), "--describe"],
                              capture_output=True, text=True, check=False, env=clean_environment())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["serverInfo"]["service_api_version"], SERVICE_API_VERSION)
        self.assertEqual([tool["name"] for tool in payload["tools"]], list(ALIASES))
        proc = subprocess.run([sys.executable, str(ROOT / "task_server.py"), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage:", proc.stdout)


class DispatchTests(unittest.TestCase):
    """Every tool dispatches through `vaws_call`, with no manager configured."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = TaskRegistry(self.root)
        patcher = mock.patch.dict("os.environ", clean_environment(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, name, **arguments):
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": {"context_file": self.registry.context_file, **arguments}}})
        self.assertNotIn("error", response, response)
        result = response["result"]
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        return result

    def test_vaws_session_is_local_and_the_same_task_answers_twice(self):
        first = self.call("vaws_session")
        second = self.call("vaws_session")
        for result in (first, second):
            self.assertFalse(result["isError"], result)
            self.assertEqual(result["structuredContent"]["outcome"], "success")
            self.assertEqual(result["structuredContent"]["status"], "open")
            self.assertEqual(result["structuredContent"]["tool"], "vaws.session")
        self.assertEqual(first["structuredContent"]["data"]["session"]["id"],
                         second["structuredContent"]["data"]["session"]["id"])
        self.assertEqual(first["structuredContent"]["target"]["session_id"], self.registry.context["session"]["id"])

    def test_vaws_session_binds_an_actual_worktree_without_any_manager(self):
        repo = self.root / "repo"
        repo.mkdir()
        for args in (["init"], ["config", "user.name", "T"], ["config", "user.email", "t@example.invalid"],
                     ["commit", "--allow-empty", "-m", "base"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        result = self.call("vaws_session", sources={"repo": str(repo)})
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["data"]["session"]["sources"]["repo"]["path"], str(repo.resolve()))

    def test_vaws_run_without_a_manager_is_blocked_and_never_a_remote_success(self):
        result = self.call("vaws_run", request_id="req-1", command="true")
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["tool"], "vaws.run")
        self.assertEqual((result["structuredContent"]["outcome"], result["structuredContent"]["status"]), ("blocked", "unavailable"))
        self.assertIn("not configured", result["structuredContent"]["summary"])
        self.assertIn("local development remains available", result["structuredContent"]["summary"])
        self.assertTrue(any("No remote success is implied" in warning for warning in result["structuredContent"]["warnings"]))

    def test_vaws_execution_rejects_an_id_it_does_not_own_before_any_network(self):
        result = self.call("vaws_execution", execution_id="not-an-id", action="status")
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["tool"], "vaws.execution")
        self.assertEqual(result["structuredContent"]["outcome"], "blocked")
        self.assertIn("invalid local execution id", result["structuredContent"]["summary"])

    def test_vaws_finish_completes_locally_and_the_dotted_name_is_still_accepted(self):
        result = self.call("vaws.finish")
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["tool"], "vaws.finish")
        self.assertEqual((result["structuredContent"]["outcome"], result["structuredContent"]["status"]), ("success", "finished"))
        self.assertTrue(result["structuredContent"]["data"]["worktrees_preserved"])

    def test_a_missing_context_is_blocked_not_guessed(self):
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "vaws_session"}})
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["status"], "unavailable")
        self.assertIn("context is required", result["structuredContent"]["summary"])

    def test_call_tool_uses_this_repositorys_result_envelope(self):
        result = call_tool("vaws_session", {"context_file": self.registry.context_file})
        self.assertEqual(result["structuredContent"]["schema_version"], "remote-dev.result.v1")
        self.assertLessEqual({"tool", "invocation_id", "target", "outcome", "status", "summary", "warnings"},
                             set(result["structuredContent"]))


class UnreachableManagerTests(unittest.TestCase):
    """A configured but dead HTTP manager degrades remote tools; local ones keep working."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = TaskRegistry(self.root)
        self.registry.configure_manager(f"http://127.0.0.1:{closed_loopback_port()}/mcp")
        patcher = mock.patch.dict("os.environ", clean_environment(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, name, **arguments):
        return handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": {"context_file": self.registry.context_file, **arguments}}})["result"]

    def test_local_operations_succeed_while_the_manager_is_down(self):
        opened = self.call("vaws_session")
        self.assertFalse(opened["isError"], opened)
        self.assertEqual(opened["structuredContent"]["status"], "open")
        finished = self.call("vaws_finish")
        self.assertFalse(finished["isError"], finished)
        self.assertEqual(finished["structuredContent"]["status"], "finished")

    def test_remote_operations_are_blocked_with_a_transport_error_not_a_crash(self):
        result = self.call("vaws_run", request_id="req-1", command="true")
        self.assertTrue(result["isError"])
        self.assertEqual((result["structuredContent"]["outcome"], result["structuredContent"]["status"]), ("blocked", "unavailable"))
        # The summary names the transport failure; it does not claim a queue
        # position, a binding or a job.
        self.assertRegex(result["structuredContent"]["summary"], r"(?i)connection|refused|urlopen|errno")
        self.assertNotIn("execution_id", result["structuredContent"])
        # The registry still recorded the planned execution and finish still
        # completes locally: the dead manager never held any lease for it.
        finished = self.call("vaws_finish")
        self.assertFalse(finished["isError"], finished)
        self.assertEqual(finished["structuredContent"]["status"], "finished")


class StdioClient:
    """A minimal client for one real server subprocess, framed or line-delimited."""

    def __init__(self, framed: bool, env: dict):
        self.framed = framed
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "task_server.py")], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.sequence = 0

    def send(self, payload):
        body = json.dumps(payload).encode()
        if self.framed:
            self.proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        else:
            self.proc.stdin.write(body + b"\n")
        self.proc.stdin.flush()

    def receive(self):
        if not self.framed:
            return json.loads(self.proc.stdout.readline())
        headers = {}
        while True:
            line = self.proc.stdout.readline()
            if line in {b"\r\n", b"\n"}:
                break
            key, value = line.decode().split(":", 1)
            headers[key.strip().lower()] = value.strip()
        return json.loads(self.proc.stdout.read(int(headers["content-length"])))

    def rpc(self, method, params=None):
        self.sequence += 1
        self.send({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params or {}})
        reply = self.receive()
        assert reply["id"] == self.sequence, reply
        return reply

    def close(self):
        self.proc.stdin.close()
        code = self.proc.wait(timeout=20)
        stderr = self.proc.stderr.read().decode()
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code, stderr


class LiveStdioTests(unittest.TestCase):
    """Actual tool calls over a subprocess stdio session, in both framings."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = TaskRegistry(Path(self.temp.name))
        self.env = clean_environment()

    def session(self, framed):
        client = StdioClient(framed, self.env)
        init = client.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "test", "version": "0"}})
        self.assertEqual(service_api_version(init["result"]), SERVICE_API_VERSION)
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        names = [tool["name"] for tool in client.rpc("tools/list")["result"]["tools"]]
        self.assertEqual(names, ["vaws_session", "vaws_run", "vaws_execution", "vaws_finish"])
        first = client.rpc("tools/call", {"name": "vaws_session", "arguments": {"context_file": self.registry.context_file}})
        second = client.rpc("tools/call", {"name": "vaws_session", "arguments": {"context_file": self.registry.context_file}})
        for reply in (first, second):
            self.assertFalse(reply["result"]["isError"], reply)
        self.assertEqual(first["result"]["structuredContent"]["data"]["session"]["id"],
                         second["result"]["structuredContent"]["data"]["session"]["id"])
        run = client.rpc("tools/call", {"name": "vaws_run", "arguments": {"context_file": self.registry.context_file,
                                                                          "request_id": "r1", "command": "true"}})
        self.assertTrue(run["result"]["isError"])
        self.assertEqual(run["result"]["structuredContent"]["status"], "unavailable")
        unknown = client.rpc("tools/call", {"name": "remote_bash", "arguments": {}})
        self.assertEqual(unknown["error"]["code"], -32602)
        code, stderr = client.close()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")

    def test_content_length_framed_session(self):
        self.session(framed=True)

    def test_newline_delimited_session_as_native_clients_speak_it(self):
        self.session(framed=False)

    def test_malformed_frames_get_json_rpc_errors_and_the_session_continues(self):
        client = StdioClient(True, self.env)
        client.proc.stdin.write(b"Content-Length: 5\r\n\r\n{nope")
        client.proc.stdin.flush()
        self.assertEqual(client.receive()["error"]["code"], -32700)
        client.proc.stdin.write(b"Content-Length: 2\r\n\r\n[]")
        client.proc.stdin.flush()
        self.assertEqual(client.receive()["error"]["code"], -32600)
        self.assertEqual(client.rpc("ping")["result"], {})
        code, stderr = client.close()
        self.assertEqual(code, 0, stderr)


class TransportUnitTests(unittest.TestCase):
    def test_framing_is_decided_by_the_first_line_and_mirrored_in_replies(self):
        import io
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        reader = io.BytesIO(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        writer = io.BytesIO()
        transport = StdioTransport(reader, writer)
        self.assertEqual(json.loads(transport.receive()), json.loads(body))
        transport.send({"ok": True})
        self.assertTrue(writer.getvalue().startswith(b"Content-Length: "))
        self.assertIsNone(transport.receive())
        reader = io.BytesIO(b"\n" + body + b"\n")
        writer = io.BytesIO()
        transport = StdioTransport(reader, writer)
        self.assertEqual(json.loads(transport.receive()), json.loads(body))
        transport.send({"ok": True})
        self.assertEqual(writer.getvalue(), b'{"ok":true}\n')

    def test_a_frame_without_content_length_is_a_protocol_error(self):
        import io
        transport = StdioTransport(io.BytesIO(b"Content-Type: x\r\n\r\n{}"), io.BytesIO())
        transport.framed = True
        with self.assertRaisesRegex(task_server.ProtocolError, "Content-Length"):
            transport.receive()


if __name__ == "__main__":
    unittest.main()
