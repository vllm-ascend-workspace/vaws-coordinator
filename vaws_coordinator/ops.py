"""Task-facing operations exposed identically to every MCP host.

These four names (`vaws.session`, `vaws.run`, `vaws.execution`, `vaws.finish`)
are coordinator semantics. Clients pass command and experiment needs; this
package owns placement, environment, and recovery.
"""

from __future__ import annotations

import json
import time

from remote_dev.result import make_result as _default_make_result

TOOL_DESCRIPTIONS = {
    "vaws.session": "Inspect this native session's VAWS task and bind actual business worktrees. Local only: no machine is required. Use the context_file supplied by the session hook.",
    "vaws.run": "Run a business command on this task's isolated root in the user container. Pass environment/resource/topology needs; do not pass request IDs, profile hashes, or runtime IDs. The coordinator places, prepares, launches and recovers.",
    "vaws.execution": "Observe, tail, stop or read the launch target of one execution belonging to this VAWS task. Stop releases that execution's devices and ports; the container and task root remain.",
    "vaws.finish": "Finish this VAWS task by closing admission and stopping owned executions; the coordinator completes cleanup and returns leases. Preserve the container, worktrees and evidence.",
}


def task_schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object",
            "properties": {"context_file": {"type": "string", "description": "Local task context supplied by the native session hook; never guess from cwd or newest history."},
                           **properties},
            "required": list(required), "additionalProperties": False}


TOOL_SCHEMAS = {
    "vaws.session": task_schema({"sources": {"type": "object", "additionalProperties": {"type": "string"}}}),
    "vaws.run": task_schema({
        "command": {"type": "string"},
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
        "environment": {"type": "object"},
        "resources": {"type": "object"},
        "topology": {"type": "object"},
        "timeout_seconds": {"type": ["integer", "null"], "default": 1800},
        "service": {"type": ["string", "null"], "description": "Task-scoped service name; reconnects a live service with this name"},
        "restart": {"type": "boolean", "description": "Replace a live named service, including one with the same spec"},
    }, ("command",)),
    "vaws.execution": task_schema({"execution_id": {"type": "string"}, "action": {"type": "string", "enum": ["status", "tail", "stop", "target"]}, "force": {"type": "boolean"}, "role": {"type": "string", "description": "Optional topology role name for per-role target or tail"}}, ("execution_id",)),
    "vaws.finish": task_schema({"force": {"type": "boolean"}}),
}


def vaws_call(name, args, *, make_result=None):
    started = time.monotonic()
    target = {"kind": "vaws-task"}
    make_result = make_result or _default_make_result
    try:
        from vaws_coordinator.task_client import TaskClient
        client = TaskClient(args.get("context_file", ""))
        target["session_id"] = client.context["session"]["id"]
        if name == "vaws.session":
            if args.get("sources"):
                client.sources(args["sources"])
            value = client.status()
            status = value["session"]["state"]
        elif name == "vaws.run":
            keys = ("command", "env", "environment", "resources", "topology",
                    "timeout_seconds", "service", "restart")
            value = client.run(**{key: args[key] for key in keys if key in args})
            status = value["state"]
        elif name == "vaws.execution":
            value = client.observe(args["execution_id"], args.get("action", "status"),
                                   args.get("force", False), role=args.get("role"))
            status = value["state"]
        elif name == "vaws.finish":
            value = client.finish(args.get("force", False))
            status = value["state"]
        else:
            raise ValueError("unknown VAWS operation")
        outcome = "blocked" if status in {"uncertain", "waiting", "waiting_for_runtime", "queued", "preparing"} else "failed" if status == "failed" else "timeout" if status == "timeout" else "success"
        if name == "vaws.finish" and outcome == "success" and status != "finished":
            outcome = "blocked"
        result = make_result(tool=name, target=target, outcome=outcome, status=status,
                             summary="VAWS " + status.replace("_", " "),
                             duration_ms=int((time.monotonic() - started) * 1000), extra={"data": value})
    except Exception as exc:
        result = make_result(tool=name, target=target, outcome="blocked", status="unavailable",
                             summary=str(exc), duration_ms=int((time.monotonic() - started) * 1000),
                             warnings=["Local file and shell tools remain available. No remote success is implied."])
    return {"text": json.dumps(result, ensure_ascii=False), "result": result}
