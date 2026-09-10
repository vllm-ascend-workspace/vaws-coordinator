"""Isolated task work roots: sources, interpreter, installs, verified profile.

A donor container/SSH endpoint may be reused. Donor site-packages, editable
references and build artifacts are never mutated. Immutable image packages
may be visible through ``venv --system-site-packages``.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from vaws_coordinator.placement import SUPPORTED_RECIPES
from vaws_coordinator.provision.host_ops import DEFAULT_WORKDIR
from vaws_coordinator.ready_runtime import safe_id

INSTALL_STEPS = (
    "install-vllm",
    "check-build-compat",
    "install-vllm-ascend-requirements",
    "install-vllm-ascend",
    "verify-imports",
    "verify-deps",
    "write-marker",
)


def _safe_segment(value: str, limit: int) -> str:
    text = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(value))
    return text[:limit] or "x"


def isolated_root(session_id: str, role_name: str, host: str = "") -> str:
    session_safe = _safe_segment(session_id, 48)
    role_safe = safe_id(role_name)
    if host:
        host_safe = _safe_segment(str(host).replace(":", "-"), 40)
        return f"{DEFAULT_WORKDIR}/tasks/{session_safe}/{host_safe}/{role_safe}"
    return f"{DEFAULT_WORKDIR}/tasks/{session_safe}/{role_safe}"


def isolated_python(root: str) -> str:
    return f"{root}/.venv/bin/python"


def task_runtime_id(session_id: str, host: str, role_name: str) -> str:
    session_safe = _safe_segment(session_id, 24)
    host_safe = _safe_segment(str(host).replace(":", "-"), 40)
    role_safe = _safe_segment(role_name, 24)
    return safe_id(f"t{session_safe}-h{host_safe}-{role_safe}"[:128])


def checkout_identity(runtime_id: str, role_name: str) -> str:
    ident = f"{safe_id(runtime_id)}-{safe_id(role_name)}"
    return ident[:128]


def create_venv_script(root: str, python: str, donor_python: str | None = None) -> str:
    """Create a task-owned venv. Image packages may be reused; donor venv is not."""
    from vaws_coordinator.parity import DEFAULT_ENV_PREAMBLE
    from vaws_coordinator.parity_support import quoted

    donor = quoted(donor_python or "")
    return "\n".join(
        [
            "set -euo pipefail",
            f"mkdir -p {quoted(root)}",
            *DEFAULT_ENV_PREAMBLE,
            'IMAGE_PYTHON="$PYTHON"',
            'if [ -z "$IMAGE_PYTHON" ]; then echo "image python3 not found" >&2; exit 1; fi',
            f"DONOR={donor}",
            'if [ -n "$DONOR" ] && [ "$IMAGE_PYTHON" = "$DONOR" ]; then',
            '  IMAGE_PYTHON="$(ls -1d /usr/local/python*/bin/python3 2>/dev/null | sort -V | tail -n 1 || true)"',
            "fi",
            'if [ -n "$DONOR" ] && [ "$IMAGE_PYTHON" = "$DONOR" ]; then',
            '  echo "cannot create a task interpreter from the donor python" >&2',
            "  exit 1",
            "fi",
            f'"$IMAGE_PYTHON" -m venv --system-site-packages {quoted(str(PurePosixPath(root) / ".venv"))}',
            f"test -x {quoted(python)}",
            f'test {quoted(python)} != "$DONOR"',
        ]
    )


class TaskRootBusy(RuntimeError):
    """The deterministic task root is still bound; wait instead of overwriting."""


def prepare_task_environment(
    pool,
    *,
    user: str,
    session_id: str,
    role_name: str,
    environment: dict[str, Any] | None,
    donor: dict[str, Any],
    sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create an isolated task root, install into a task-owned interpreter, register.

    ``sources`` must be the actual local vllm / vllm-ascend worktrees. Registration
    happens only after the backend has a verified ready-profile for this root.
    """
    environment = dict(environment or {})
    sources = dict(sources or {})
    if not {"vllm", "vllm-ascend"}.issubset(sources):
        raise ValueError("bind the actual vllm and vllm-ascend worktrees before preparation")
    host = str(
        donor.get("host")
        or (donor.get("host_endpoint") or {}).get("host")
        or (donor.get("endpoint") or {}).get("host")
        or ""
    )
    if not host:
        raise ValueError("preparation needs a host identity")
    root = isolated_root(session_id, role_name, host)
    python = isolated_python(root)
    runtime_id = task_runtime_id(session_id, host, role_name)
    donor_python = donor.get("python")
    if donor_python and python == donor_python:
        raise ValueError("task-owned interpreter must not be the donor interpreter")

    existing = next((item for item in pool.catalog()
                     if item.get("runtime_id") == runtime_id or item.get("root") == root), None)
    if existing:
        if existing.get("state") == "bound" or pool.runtime_busy(existing["runtime_id"]):
            raise TaskRootBusy(f"task root {root} is still bound; wait before rematerializing")
        if existing.get("state") == "ready" and existing.get("python") == python:
            return next(row for row in _runtime_rows(pool) if row["id"] == existing["runtime_id"])

    raw_host = donor.get("host_endpoint") or {}
    host_port = raw_host.get("port")
    if not host_port and isinstance(donor.get("host"), dict):
        host_port = donor["host"].get("port")
    host_endpoint = {
        "host": host,
        "port": int(host_port or 22),
        "user": raw_host.get("user") or "root",
    }
    ssh_port = donor.get("ssh_port") or (donor.get("endpoint") or {}).get("port")
    if not ssh_port:
        raise ValueError("user container has no reserved SSH endpoint to reuse")
    recipe = environment.get("recipe") or environment.get("image") or donor.get("recipe")
    if recipe and recipe not in SUPPORTED_RECIPES and not donor.get("recipe"):
        raise ValueError(f"unsupported environment recipe {recipe!r}")
    container_endpoint = {
        "host": host_endpoint["host"],
        "port": int(ssh_port),
        "user": (donor.get("endpoint") or {}).get("user") or "root",
        "root": root,
        "cwd": root,
    }
    spec = {
        "user": user,
        "python": python,
        "recipe": recipe,
        "host_endpoint": {
            "host": host_endpoint["host"],
            "port": int(host_endpoint.get("port") or 22),
            "user": host_endpoint.get("user") or "root",
        },
        "endpoint": container_endpoint,
        "service_ports": list(donor.get("service_ports") or []),
        "container_name": donor.get("container_name") or ("vaws-" + user),
        "machine_type": environment.get("machine_type") or donor.get("machine_type"),
    }
    backend = pool.backend
    if not hasattr(backend, "prepare_task_root"):
        raise RuntimeError("backend cannot prepare an isolated task environment")
    backend.prepare_task_root(
        spec, sources=sources, environment=environment, donor_python=donor_python,
        workspace_root=str(__import__("pathlib").Path(sources["vllm"]).resolve().parent),
    )
    return pool.register(runtime_id, spec)


def _runtime_rows(pool):
    with pool.transaction() as db:
        return pool.rows(db, "runtime")
