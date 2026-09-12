"""Small task-facing observations; full operation records remain locally readable."""
from __future__ import annotations

import json
from pathlib import Path

EXECUTION_KEYS = ("execution_id", "state", "service", "error", "error_ref", "reason", "progress",
                  "source_snapshot_id", "sources", "role_progress",
                  "observed_at", "cancel_requested", "service_port", "provisioning_started",
                  "worktrees_preserved", "resources_released", "stdout", "stderr", "observation_freshness")
ROLE_KEYS = ("name", "state", "runtime_id", "host", "root", "service_port", "error", "lease_state",
             "quiet", "descendants_drained", "stdout", "stderr", "status_observed_at")


def execution_summary(value: dict, *, target=False) -> dict:
    result = {key: value[key] for key in EXECUTION_KEYS if value.get(key) is not None}
    if "id" in value and "execution_id" not in result:
        result["execution_id"] = value["id"]
        result["state"] = value.get("phase")
    roles = value.get("roles") or []
    if roles:
        result["roles"] = [{key: role[key] for key in ROLE_KEYS if role.get(key) is not None}
                           for role in roles[:8]]
        if len(roles) > 8:
            result["roles_total"] = len(roles)
    if target and value.get("target"):
        result["target"] = value["target"]
    return result


def compact_data(value: dict, *, target=False) -> dict:
    if "session" in value:
        session = value["session"]
        result = {"session": {key: session[key] for key in ("id", "state", "sources") if key in session},
                  "context_file": value.get("context_file"),
                  "attachments_count": len(value.get("attachments") or [])}
        if "source_defaults" in value:
            result["source_defaults"] = value["source_defaults"]
    else:
        result = execution_summary(value, target=target)
    if "executions" in value:
        rows = value["executions"]
        result["executions"] = [execution_summary(row) for row in rows[-5:]]
        result["executions_total"] = len(rows)
    return result


def compact_runtime(runtime):
    """Collapse repeated healthy package identities, retaining diagnostic facts."""
    if not isinstance(runtime, dict) or not runtime or set(runtime) - {"client", "daemon"}:
        return runtime
    packages = {}
    for records in runtime.values():
        if not isinstance(records, list) or not records:
            return runtime
        for record in records:
            if not isinstance(record, dict):
                return runtime
            loaded = record.get("loaded") or {}
            if not isinstance(loaded, dict):
                return runtime
            package, version = loaded.get("package"), loaded.get("version")
            if record.get("status") != "current" or not package or not version:
                return runtime
            if package in packages and packages[package] != version:
                return runtime
            packages[package] = version
    return {"status": "current", "packages": packages}


def present(result: dict, directory: Path, *, full=False, target=False) -> dict:
    """Save the actual response before projecting it. A write failure never undoes an operation."""
    path = Path(directory) / "results" / (result["invocation_id"] + ".json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        result = {**result, "record_ref": str(path)}
    except OSError as exc:
        return {**result, "warnings": [*result.get("warnings", []), f"Full record write failed: {exc}"]}
    if full:
        return result
    data = compact_data(result.get("data") or {}, target=target)
    # Bound repeated log tails and arbitrary error text; exact bytes are in record_ref.
    def bound(value):
        if isinstance(value, str):
            return value[-2000:] if len(value) > 2000 else value
        if isinstance(value, dict):
            return {key: bound(item) for key, item in value.items()}
        if isinstance(value, list):
            return [bound(item) for item in value[:8]]
        return value
    compact = {**result, "data": {**bound({k: v for k, v in data.items() if k != "target"}),
                                  **({"target": data["target"]} if "target" in data else {})}}
    if not target and "runtime" in compact:
        compact["runtime"] = compact_runtime(compact["runtime"])
    if len(json.dumps(compact, ensure_ascii=False).encode()) > 16000:
        compact["data"] = {key: data[key] for key in ("execution_id", "state", "service", "error_ref") if key in data}
        compact["summary"] = str(result["summary"])[:1000]
        compact["detail_omitted"] = True
    return compact
