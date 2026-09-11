"""Stdio MCP server for the four task-facing tools.

`vaws_session`, `vaws_run`, `vaws_execution` and `vaws_finish` are coordinator
semantics. This process is their home. It is local-first: `vaws_session` and
`vaws_finish` need no remote resources; `vaws_run` uses this process's own
runtime pool.

Wire contract: JSON-RPC 2.0 over stdio. The first bytes decide the framing
for the whole session: Content-Length framed or one JSON object per line.
Standard library only for the transport; results use `remote_dev.result`.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, BinaryIO

from vaws_coordinator.host_queue import SCHEMA_VERSION
from vaws_coordinator.ops import TOOL_DESCRIPTIONS, TOOL_SCHEMAS, vaws_call, LOADED_RUNTIMES

SERVICE_NAME = "vaws-coordinator-task"
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
ALIASES = {name.replace(".", "_"): name for name in TOOL_SCHEMAS}
INSTRUCTIONS = (
    "VAWS task tools. Pass the context_file supplied by the native session "
    "hook; never guess a task from cwd or history. vaws_session and "
    "vaws_finish are local. vaws_run uses this process's local runtime pool."
)


def package_version() -> str:
    return LOADED_RUNTIMES[0]["version"] or "unknown"


class ProtocolError(ValueError):
    """A malformed frame or message that gets a JSON-RPC error, not a crash."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def capabilities() -> dict[str, Any]:
    return {
        "tools": {"listChanged": False},
        "experimental": {
            SERVICE_NAME: {
                "version": package_version(),
                "host_protocol_schema_version": SCHEMA_VERSION,
                "tools": sorted(ALIASES),
            }
        },
    }


def server_info() -> dict[str, Any]:
    return {"name": SERVICE_NAME, "version": package_version()}


def initialize_result(params: dict[str, Any]) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    return {
        "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1],
        "capabilities": capabilities(),
        "serverInfo": server_info(),
        "instructions": INSTRUCTIONS,
    }


def list_tools() -> list[dict[str, Any]]:
    return [
        {"name": alias, "description": TOOL_DESCRIPTIONS[name], "inputSchema": TOOL_SCHEMAS[name]}
        for alias, name in ALIASES.items()
    ]


def canonical_name(name: str) -> str:
    return ALIASES.get(name, name)


def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    canonical = canonical_name(name)
    if canonical not in TOOL_SCHEMAS:
        raise ProtocolError(-32602, f"unknown task tool: {name}")
    # A persistent MCP server's environment can outlive the native caller.
    # It must use the caller's context, never the thread that launched it.
    payload = vaws_call(canonical, arguments or {}, allow_native_context=False)
    result = payload["result"]
    return {
        "content": [{"type": "text", "text": payload["text"]}],
        "structuredContent": result,
        "isError": result.get("outcome") not in {"success", "cancelled"},
    }


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if request_id is None and isinstance(method, str) and method.startswith("notifications/"):
        return None
    if not isinstance(method, str):
        return _error(request_id, -32600, "request must carry a string method")
    try:
        if method == "initialize":
            return _result(request_id, initialize_result(params))
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": list_tools()})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            arguments = {} if arguments is None else arguments
            if not isinstance(name, str):
                raise ValueError("tools/call requires a string name")
            if not isinstance(arguments, dict):
                raise ValueError("tools/call arguments must be an object")
            return _result(request_id, call_tool(name, arguments))
        return _error(request_id, -32601, f"method not found: {method}")
    except ProtocolError as exc:
        return _error(request_id, exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error(request_id, -32000, str(exc), {"type": type(exc).__name__})


def _result(request_id: Any, value: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    if data is not None:
        payload["error"]["data"] = data
    return payload


class StdioTransport:
    def __init__(self, reader: BinaryIO, writer: BinaryIO):
        self.reader = reader
        self.writer = writer
        self.framed: bool | None = None

    def send(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self.framed:
            self.writer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii"))
            self.writer.write(encoded)
        else:
            self.writer.write(encoded + b"\n")
        self.writer.flush()

    def _read_exact(self, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.reader.read(length - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks)

    def receive(self) -> bytes | None:
        line = self.reader.readline()
        if not line:
            return None
        if self.framed is None:
            self.framed = line.lower().startswith(b"content-length:")
        if not self.framed:
            while not line.strip():
                line = self.reader.readline()
                if not line:
                    return None
            return line
        headers: dict[str, str] = {}
        while line not in {b"\r\n", b"\n"}:
            text = line.decode("ascii", errors="replace").strip()
            if ":" in text:
                key, value = text.split(":", 1)
                headers[key.lower()] = value.strip()
            line = self.reader.readline()
            if not line:
                return None
        try:
            length = int(headers["content-length"])
        except (KeyError, ValueError) as exc:
            raise ProtocolError(-32600, "missing or invalid Content-Length header") from exc
        return self._read_exact(length)

    def messages(self):
        while True:
            try:
                body = self.receive()
            except ProtocolError as exc:
                self.send(_error(None, exc.code, str(exc)))
                continue
            if body is None:
                return
            try:
                message = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.send(_error(None, -32700, f"parse error: {exc}"))
                continue
            if not isinstance(message, dict):
                self.send(_error(None, -32600, "request must be an object"))
                continue
            yield message


def serve(reader: BinaryIO, writer: BinaryIO) -> int:
    transport = StdioTransport(reader, writer)
    for message in transport.messages():
        response = handle(message)
        if response is not None:
            transport.send(response)
    return 0


def describe() -> dict[str, Any]:
    return {"serverInfo": server_info(), "capabilities": capabilities(), "tools": list_tools()}


def main(argv: list[str] | None = None) -> int:
    from vaws_coordinator._stdio import configure_stdio
    configure_stdio()
    parser = argparse.ArgumentParser(
        description="Serve the VAWS task tools over stdio (JSON-RPC 2.0, "
                    "Content-Length framed or newline-delimited)."
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="print the capability declaration and tool list as JSON, then exit",
    )
    args = parser.parse_args(argv)
    if args.describe:
        print(json.dumps(describe(), ensure_ascii=False, indent=2))
        return 0
    reader, writer = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr
    return serve(reader, writer)


if __name__ == "__main__":
    raise SystemExit(main())
