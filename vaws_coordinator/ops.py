"""Task-facing operations exposed identically to every MCP host.

These four names (`vaws.session`, `vaws.run`, `vaws.execution`, `vaws.finish`)
are coordinator semantics. Clients pass command and experiment needs; this
package owns placement, environment, and recovery.
"""

from __future__ import annotations

import time

from remote_dev.result import make_result
from remote_dev.runtime import process_identity, runtime_status
from vaws_coordinator.presentation import present
from vaws_coordinator.placement import ENVIRONMENT_KEYS
from vaws_coordinator.state_paths import coordinator_state_dir

LOADED_RUNTIMES = [process_identity(name) for name in ("vaws-coordinator", "vaws-remote-dev")]

TOOL_DESCRIPTIONS = {
    "vaws.session": "Inspect this native session's VAWS task or replace source defaults for future submissions. Local only: no machine is required. Active executions retain their submitted inputs.",
    "vaws.run": "Submit a managed command with fixed source inputs and environment/resource/topology needs. Omitted sources uses explicit task defaults or this native attachment's automatic cwd binding; sources={} runs without source dependencies. Devices default to zero. Explicit resources.allow_external_busy=true shares one named physical device with external processes while retaining managed lease and process ownership. The coordinator places, prepares, launches and supervises the execution.",
    "vaws.execution": "Use action=status (default), tail, stop or target with an execution_id or task-scoped service name belonging to this VAWS task. Status reads current progress; stop releases that execution's devices and ports. The container and execution root remain.",
    "vaws.finish": "Finish this VAWS task by closing admission and stopping owned executions; the coordinator completes cleanup and returns leases. Preserve the container, worktrees and evidence.",
}


def task_schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object",
            "properties": {"context_file": {"type": "string", "description": "Local task context supplied by the native session hook; never guess from cwd or newest history."},
                           "full": {"type": "boolean", "default": False, "description": "Return the full operation record instead of its compact observation."},
                           **properties},
            "required": list(required), "additionalProperties": False}


RESOURCE_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "devices": {"type": "array", "items": {"type": "integer", "minimum": 0}, "uniqueItems": True},
    "npu_count": {"type": "integer", "minimum": 0, "description": "Number of NPUs; defaults to zero. If devices is also supplied, this must equal its length."},
    "allow_external_busy": {"type": "boolean", "default": False, "description": "Explicitly permit external occupancy on exactly one named physical device. Requires devices=[id]; other managed leases, holds and owned process/port cleanup still apply."},
    "service_port": {"type": "integer", "minimum": 0, "maximum": 65535}}}
RESOURCE_SCHEMA["allOf"] = [{
    "if": {"required": ["allow_external_busy"], "properties": {"allow_external_busy": {"const": True}}},
    "then": {"required": ["devices"], "properties": {"devices": {"minItems": 1, "maxItems": 1}}},
}]
ROLE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name"], "properties": {
    **RESOURCE_SCHEMA["properties"], "name": {"type": "string", "minLength": 1},
    "host": {"type": "string", "minLength": 1}, "command": {"type": "string", "minLength": 1},
    "preflight": {"type": "string", "minLength": 1},
    "env": {"type": "object", "additionalProperties": {"type": "string"}}}}


TOOL_SCHEMAS = {
    "vaws.session": task_schema({"sources": {"type": "object", "additionalProperties": {"type": "string"}}}),
    "vaws.run": task_schema({
        "command": {"type": "string"},
        "sources": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Actual worktrees to capture once. Omit to use explicit task defaults or this attachment's automatic cwd binding; {} selects no sources."},
        "preflight": {"type": "string", "description": "Optional validation command in the prepared root before NPU allocation. It must not require devices or start a service."},
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
        "environment": {"type": "object", "additionalProperties": False, "properties": {
            key: {"type": "string", "minLength": 1} for key in sorted(ENVIRONMENT_KEYS)}},
        "resources": RESOURCE_SCHEMA,
        "topology": {"type": "object", "additionalProperties": False, "properties": {
            "host": {"type": "string", "minLength": 1, "description": "Host constraint for one default role; use roles[].host for a role group."},
            "roles": {"type": "array", "minItems": 1, "items": ROLE_SCHEMA},
            "distinct_hosts": {"type": "boolean"}}, "not": {"required": ["host", "roles"]}},
        "timeout_seconds": {"type": ["integer", "null"], "default": 1800},
        "service": {"type": ["string", "null"], "description": "Ensure a task-scoped service with identical fixed sources and configuration. Changed inputs require restart. To connect without capture, use vaws_execution(service=...)."},
        "restart": {"type": "boolean", "description": "Replace a live named service, including one with the same spec"},
    }, ("command",)),
    "vaws.execution": task_schema({"execution_id": {"type": "string"}, "service": {"type": "string", "description": "Task-scoped service name, mutually exclusive with execution_id"}, "action": {"type": "string", "enum": ["status", "tail", "stop", "target"]}, "force": {"type": "boolean"}, "refresh": {"type": "boolean", "default": False, "description": "Refresh remote status instead of reusing the last snapshot for up to two seconds. Busy executions return cache age and refresh_deferred."}, "role": {"type": "string", "description": "Optional topology role name for per-role target or tail"}}),
    "vaws.finish": task_schema({"force": {"type": "boolean"}}),
}
TOOL_SCHEMAS["vaws.execution"]["oneOf"] = [
    {"required": ["execution_id"], "not": {"required": ["service"]}},
    {"required": ["service"], "not": {"required": ["execution_id"]}},
]


def vaws_call(name, args, *, allow_native_context=True):
    started = time.monotonic()
    target = {"kind": "vaws-task"}
    client = None
    try:
        if name not in TOOL_SCHEMAS:
            raise ValueError("unknown VAWS operation")
        unknown = set(args) - set(TOOL_SCHEMAS[name]["properties"])
        if unknown:
            raise ValueError("unsupported fields for " + name + ": " + ", ".join(sorted(unknown)))
        from vaws_coordinator.task_client import TaskClient
        client = TaskClient(args.get("context_file", ""), allow_native_context=allow_native_context)
        target["session_id"] = client.context["session"]["id"]
        if name == "vaws.session":
            if "sources" in args:
                client.sources(args["sources"])
            value = client.status()
            status = value["session"]["state"]
        elif name == "vaws.run":
            keys = ("command", "sources", "env", "environment", "resources", "topology",
                    "timeout_seconds", "service", "restart", "preflight")
            value = client.run(**{key: args[key] for key in keys if key in args})
            status = value["state"]
        elif name == "vaws.execution":
            value = client.observe(args.get("execution_id"), args.get("action", "status"),
                                   args.get("force", False), role=args.get("role"), refresh=bool(args.get("refresh")),
                                   **({"service": args["service"]} if "service" in args else {}))
            status = value["state"]
        elif name == "vaws.finish":
            value = client.finish(args.get("force", False))
            status = value["state"]
        else:
            raise ValueError("unknown VAWS operation")
        if status in {"failed", "inconclusive"}:
            outcome = "failed"
        elif status == "timeout":
            outcome = "timeout"
        elif status == "cancelled":
            outcome = "cancelled"
        elif status in {"uncertain", "waiting_for_runtime"}:
            outcome = "blocked"
        elif status in {"queued", "preparing", "waiting"} and not value.get("execution_id"):
            outcome = "blocked"
        else:
            # Admission and successful observation are completed tool calls.
            # A durable execution can still be queued or preparing; reporting
            # that normal progress as an MCP error makes clients retry work.
            outcome = "success"
        if name == "vaws.finish" and outcome == "success" and status != "finished":
            outcome = "blocked"
        result = make_result(tool=name, target=target, outcome=outcome, status=status,
                             summary="VAWS " + status.replace("_", " "),
                             duration_ms=int((time.monotonic() - started) * 1000), extra={"data": value})
    except Exception as exc:
        result = make_result(tool=name, target=target, outcome="blocked", status="unavailable",
                             summary=str(exc), duration_ms=int((time.monotonic() - started) * 1000),
                             warnings=["Local file and shell tools remain available. No remote success is implied."])
    result["runtime"] = {"client": [runtime_status(item) for item in LOADED_RUNTIMES]}
    if client is not None:
        service = client._service
        if service is not None and getattr(service, "runtime", None):
            result["runtime"]["daemon"] = service.runtime
        result = present(result, coordinator_state_dir(client.store.state_dir),
                         full=bool(args.get("full")), target=args.get("action") == "target")
    return {"text": result["summary"], "result": result}
