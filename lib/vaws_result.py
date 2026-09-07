"""Result envelope compatible with remote-dev's `remote-dev.result.v1`.

The envelope contract is remote-dev's, not this repository's. This mirror is
what `task_server.py` emits, so the task tools work in a deployment that has
no remote-dev checkout and the task server never imports one; a foreign MCP
host that registers the task tools should inject its own factory instead so
one process emits exactly one implementation. The mirror must stay
field-compatible and must never be treated as the schema authority.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "remote-dev.result.v1"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_invocation_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def make_result(
    *,
    tool: str,
    target: dict[str, Any],
    outcome: str,
    status: str,
    summary: str,
    invocation_id: str | None = None,
    started_at: str | None = None,
    duration_ms: int | None = None,
    preview: dict[str, Any] | None = None,
    refs: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    changed_files: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    next: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "invocation_id": invocation_id or new_invocation_id(),
        "target": target,
        "outcome": outcome,
        "status": status,
        "summary": summary,
        "started_at": started_at or utc_now_iso(),
        "duration_ms": duration_ms,
        "preview": preview or {},
        "refs": refs or {},
        "artifacts": artifacts or [],
        "changed_files": changed_files or [],
        "warnings": warnings or [],
        "next": next,
    }
    if extra:
        payload.update(extra)
    return payload
