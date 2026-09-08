#!/usr/bin/env python3
"""Stdio MCP server for the four task-facing tools.

`vaws_session`, `vaws_run`, `vaws_execution` and `vaws_finish` are coordinator
semantics: they create and resume VAWS tasks, bind actual worktrees and drive
pooled executions. They were once registered inside the remote-dev stdio
server, which owns neither the task registry nor the runtime pool; remote-dev
dropped them and will not grow a plugin hook for foreign tools. This process
is their home now, next to `server.py`, the authenticated HTTP manager for the
shared pool. Each repository serves its own semantics; nothing here proxies
another repository's server.

Local first. `vaws_session` binds worktrees without contacting anything, and
task creation, editing and finish work with no manager configured. Only
`vaws_run` and a live `vaws_execution` need the HTTP manager, and when it is
missing or unreachable the tool answers `blocked`/`unavailable` with a warning
instead of failing the process or implying a remote success.

Wire contract: JSON-RPC 2.0 over stdio. The first bytes decide the framing for
the whole session: `Content-Length: <n>\\r\\n\\r\\n<body>` (LSP-style, what a
hand-driven client or the remote-dev prior art sends) or one JSON object per
line (what the MCP stdio transport specifies and what native clients send).
Replies use the framing of the request. Standard library only: no SDK is
imported for this, and nothing from remote-dev is imported either.

Capability: the `initialize` result declares
`capabilities.experimental["vaws-coordinator-task"].service_api_version` as
the integer published in `service-api.json`, and mirrors it in `serverInfo`
for raw readers (SDK clients validate `serverInfo` against a fixed model and
drop the copy; `experimental` is the one to probe).
A client that does not find it is talking to a
server that never declared the task tools -- an older configuration or the
remote-dev server itself -- and must treat the fact as unknown, not supported.
`service_api_version()` below is that probe.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, BinaryIO

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "lib"), str(ROOT / "lib/vendor"), str(ROOT / "host")]
from vaws_npu_coordination import SCHEMA_VERSION
from vaws_ops import TOOL_DESCRIPTIONS, TOOL_SCHEMAS, vaws_call
from vaws_result import make_result

SERVICE_NAME = "vaws-coordinator-task"
SERVER_VERSION = "0.1.0"
# Bump when a tool's arguments, result fields or outcome mapping change in a
# way a client has to know about. Absence means "no task tools declared".
# `service-api.json` next to this module is the single source of that number,
# so the wire declaration and the published contract cannot drift apart, and
# it is an integer here because the four-provider contract is integer-typed:
# a client comparing `initialize` against `service-api.json` or against a
# sibling provider must never have to reconcile `"1"` with `1`.
SERVICE_API_CONTRACT = json.loads((ROOT / "service-api.json").read_text(encoding="utf-8"))
SERVICE_API_VERSION: int = int(SERVICE_API_CONTRACT["service_api_version"])
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
# Portable underscore names are advertised (dotted names break at least one
# client's session registry); the canonical dotted names stay accepted on
# `tools/call` for existing integrations and for `scripts/vaws.py`.
ALIASES = {name.replace(".", "_"): name for name in TOOL_SCHEMAS}
INSTRUCTIONS = (
    "VAWS task tools. Pass the context_file supplied by the native session "
    "hook; never guess a task from cwd or history. vaws_session and "
    "vaws_finish are local and need no manager; vaws_run needs the shared "
    "coordinator and reports blocked/unavailable when it is not configured "
    "or not reachable. Local file and shell tools stay usable either way."
)


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
                "service_api_version": SERVICE_API_VERSION,
                "host_protocol_schema_version": SCHEMA_VERSION,
                "tools": sorted(ALIASES),
            }
        },
    }


def server_info() -> dict[str, Any]:
    return {"name": SERVICE_NAME, "version": SERVER_VERSION, "service_api_version": SERVICE_API_VERSION}


def initialize_result(params: dict[str, Any]) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    return {
        "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1],
        "capabilities": capabilities(),
        "serverInfo": server_info(),
        "instructions": INSTRUCTIONS,
    }


def service_api_version(initialize_payload: dict[str, Any]) -> int | None:
    """Client-side probe over an `initialize` result.

    Returns the declared version as an integer, matching `service-api.json`
    and the sibling providers, or None when the server declared nothing.
    None is "unknown": the peer may be the remote-dev server, an older task
    server, or anything else, and a client must degrade rather than assume
    the task tools exist there. A peer that still declares the old string
    form is normalised to the integer contract; anything that is not an
    integer at all is unknown too, since it names no contract version.
    """
    experimental = (initialize_payload.get("capabilities") or {}).get("experimental") or {}
    declared = experimental.get(SERVICE_NAME) or {}
    value = declared.get("service_api_version")
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
    payload = vaws_call(canonical, arguments or {}, make_result=make_result)
    result = payload["result"]
    return {
        "content": [{"type": "text", "text": payload["text"]}],
        "structuredContent": result,
        # `blocked` (no manager, registry unavailable) is an error to the
        # client so it does not read a degraded answer as a remote success.
        "isError": result.get("outcome") not in {"success", "cancelled"},
    }


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message; None means nothing is sent back."""
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
    except Exception as exc:  # noqa: BLE001 - every failure becomes a JSON-RPC error
        return _error(request_id, -32000, str(exc), {"type": type(exc).__name__})


def _result(request_id: Any, value: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    if data is not None:
        payload["error"]["data"] = data
    return payload


class StdioTransport:
    """Content-Length framed or newline-delimited JSON-RPC over two streams.

    The first line received decides the framing for the rest of the session,
    and every reply uses that framing. Detection reads a line rather than
    peeking, because a pipe may hand back fewer bytes than asked for.
    """

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
        """Return the next raw message body, or None at end of stream."""
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
    """Offline view of what a client would learn from initialize/tools/list."""
    return {"serverInfo": server_info(), "capabilities": capabilities(), "tools": list_tools()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the VAWS task tools over stdio (JSON-RPC 2.0, "
                                                 "Content-Length framed or newline-delimited).")
    parser.add_argument("--describe", action="store_true",
                        help="print the capability declaration and tool list as JSON, then exit")
    args = parser.parse_args(argv)
    if args.describe:
        print(json.dumps(describe(), ensure_ascii=False, indent=2))
        return 0
    reader, writer = sys.stdin.buffer, sys.stdout.buffer
    # Anything a library prints to stdout would corrupt the protocol stream;
    # route stray prints to stderr for the lifetime of the server.
    sys.stdout = sys.stderr
    return serve(reader, writer)


if __name__ == "__main__":
    raise SystemExit(main())
