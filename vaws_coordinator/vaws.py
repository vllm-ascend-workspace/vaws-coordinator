#!/usr/bin/env python3
"""VAWS local context and task facade; the same operations are served by task_server.py over MCP stdio."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from remote_dev.result import make_result

from vaws_coordinator.agent_session import CLIENTS, AgentSessions, load_context
from vaws_coordinator.ops import vaws_call


def error_payload(tool: str, *, outcome: str, status: str, error: str) -> dict:
    """Same result contract as the remote-dev CLI wrappers: errors print a
    result JSON (never a traceback) and exit non-zero."""
    result = make_result(
        tool=tool,
        target={"kind": "vaws-task"},
        outcome=outcome,  # type: ignore[arg-type]
        status=status,
        summary=f"{tool} {status}.",
        preview={"stderr": error[-4000:]},
        extra={"error": error},
    )
    return result


def main():
    from vaws_coordinator._stdio import configure_stdio
    configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    attach = sub.add_parser("attach", help="Adapter entry: native root/resume, child, or explicit task association")
    attach.add_argument("--client", choices=sorted(CLIENTS), required=True)
    attach.add_argument("--native-session-id", required=True)
    attach.add_argument("--cwd", default=str(Path.cwd()))
    attach.add_argument("--parent-context", default="")
    attach.add_argument("--association", default="")
    attach.add_argument("--agent-id", default="")
    for name in ("session", "run", "execution", "finish"):
        child = sub.add_parser(name)
        child.add_argument("--context-file")
        child.add_argument("--full", action="store_true", default=None)
        child.add_argument("--json", default="{}", help="Additional structured tool arguments")
        if name == "run":
            child.add_argument("--command", required=True)
            child.add_argument("--service", default=None)
            child.add_argument("--restart", action="store_true")
            child.add_argument("--timeout-seconds", type=int)
        if name == "execution":
            child.add_argument("--execution-id", required=True)
            child.add_argument("--action", choices=["status", "tail", "stop", "target"])
            child.add_argument("--role", default=None)
    args = vars(parser.parse_args())
    operation = args.pop("operation")
    if operation == "attach":
        inherited = args["parent_context"] or args["association"]
        try:
            store = AgentSessions(Path(load_context(inherited)["state_dir"])) if inherited else AgentSessions()
            payload = store.attach(**args)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps(error_payload("vaws.attach", outcome="failed", status="attach_failed", error=f"{type(exc).__name__}: {exc}"), ensure_ascii=False))
            return 1
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    try:
        extra = json.loads(args.pop("json"))
    except json.JSONDecodeError as exc:
        print(json.dumps(error_payload("vaws." + operation, outcome="needs_input", status="invalid_json", error=f"invalid --json: {exc}"), ensure_ascii=False))
        return 1
    # Unset argparse defaults (None) must not silently override --json keys:
    # `--json '{"action":"stop"}'` degraded to a status query otherwise.
    merged = {**extra, **{key: value for key, value in args.items() if value is not None}}
    result = vaws_call("vaws." + operation, merged)
    print(json.dumps(result["result"], ensure_ascii=False))
    return 0 if result["result"]["outcome"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
