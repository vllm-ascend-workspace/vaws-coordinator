"""Task-facing operations exposed identically to every MCP host.

These four names (`vaws_session`, `vaws_run`, `vaws_execution`, `vaws_finish`)
are coordinator semantics: they create and resume VAWS tasks, bind actual
worktrees, and drive executions against this user's local pool.
"""

from __future__ import annotations

import json
import time

from remote_dev.result import make_result as _default_make_result

TOOL_DESCRIPTIONS = {
    "vaws.session": "Inspect this native session's VAWS task and bind actual business worktrees. Local only: no machine is required. Use the context_file supplied by the session hook.",
    "vaws.run": "Run the task's current source snapshot on a compatible prepared runtime. Automatically sync, acquire devices, launch, renew, observe and release; never install packages or create containers. Keep request_id unchanged on retry.",
    "vaws.execution": "Observe, tail or stop one execution belonging to this VAWS task. Other tasks cannot be selected accidentally. Stop confirms process and NPU release.",
    "vaws.finish": "Finish this VAWS task by stopping only its owned executions and releasing resources; preserve worktrees and evidence.",
}


def task_schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object",
            "properties": {"context_file": {"type": "string", "description": "Local task context supplied by the native session hook; never guess from cwd or newest history."},
                           **properties},
            "required": list(required), "additionalProperties": False}


TOOL_SCHEMAS = {
    "vaws.session": task_schema({"sources": {"type": "object", "additionalProperties": {"type": "string"}}}),
    "vaws.run": task_schema({
        "request_id": {"type": "string"}, "command": {"type": "string"},
        "profile_key": {"type": "string"}, "runtime_id": {"type": "string"},
        "devices": {"type": "array", "items": {"type": "integer"}}, "npu_count": {"type": "integer", "default": 1},
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
        "timeout_seconds": {"type": "integer", "default": 1800},
    }, ("request_id", "command")),
    "vaws.execution": task_schema({"execution_id": {"type": "string"}, "action": {"type": "string", "enum": ["status", "tail", "stop"]}, "force": {"type": "boolean"}}, ("execution_id",)),
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
            value = client.run(**{key: args[key] for key in ("request_id", "command", "profile_key", "runtime_id", "devices", "npu_count", "env", "timeout_seconds") if key in args})
            status = value["state"]
        elif name == "vaws.execution":
            value = client.observe(args["execution_id"], args.get("action", "status"), args.get("force", False))
            status = value["state"]
        elif name == "vaws.finish":
            value = client.finish(args.get("force", False))
            status = value["state"]
        else:
            raise ValueError("unknown VAWS operation")
        outcome = "blocked" if status in {"uncertain", "waiting_for_runtime"} else "failed" if status == "failed" else "timeout" if status == "timeout" else "success"
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
