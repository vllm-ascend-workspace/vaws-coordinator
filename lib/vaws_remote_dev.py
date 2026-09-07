"""Narrow adapter to the remote-dev substrate (a separate repository).

This coordinator never resolves endpoints by alias, session or machine: every
call carries a fully explicit endpoint mapping, so remote-dev needs no
knowledge of this component and no resolver plugin has to be registered on our
behalf. The exact surface required from a remote-dev checkout is:

* ``core.endpoint``: ``direct_endpoint(mapping)`` when it exists, otherwise
  ``resolve_endpoint(mapping)``, building an endpoint object from an explicit
  ``{"host", "port", "user", "root", "cwd"}`` mapping. Alias/session/machine
  resolution is deliberately unused.
* ``core.shell_ops.remote_bash(endpoint, *, command, timeout_ms, runtime_env)``
  returning ``{"result": {...}}`` where the result carries ``outcome``,
  ``status``, ``exit_code`` and ``refs.stdout`` / ``refs.stderr`` as paths to
  local log files.
The child-subreaper execution supervisor is deliberately **not** part of this
interface. It is owned by this repository (``workers/managed_jobs.py``, read
through ``backend.worker_source``), because this component is the only thing
that drives its ``{"root", "job_id", "action", ...}`` protocol and because the
guarantees it implements are this component's guarantees.

Nothing here is vendored. A missing or misconfigured checkout is a
fail-closed configuration error, never a silent local fallback.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

REMOTE_DEV_ROOT_ENV = "VAWS_REMOTE_DEV_ROOT"


class RemoteDevUnavailable(RuntimeError):
    """The configured remote-dev substrate cannot satisfy the required API."""


class RemoteDevShell:
    """Explicit-endpoint shell access to a configured remote-dev checkout."""

    def __init__(self, root: Path | str | None = None):
        self._root = Path(root).expanduser() if root else None
        self._api: dict[str, Any] | None = None

    @property
    def root(self) -> Path:
        if self._root is None:
            configured = os.environ.get(REMOTE_DEV_ROOT_ENV, "")
            if not configured:
                raise RemoteDevUnavailable(
                    f"remote-dev is not configured; set {REMOTE_DEV_ROOT_ENV} to a "
                    "remote-dev checkout root (the directory containing core/)"
                )
            self._root = Path(configured).expanduser()
        return self._root

    def _load(self) -> dict[str, Any]:
        if self._api is not None:
            return self._api
        root = self.root.resolve()
        if not (root / "core/shell_ops.py").is_file():
            raise RemoteDevUnavailable(f"{root} does not look like a remote-dev checkout")
        # Append, never prepend: remote-dev ships an unrelated `mcp/` package
        # that must not shadow the installed MCP SDK this server imports.
        if str(root) not in sys.path:
            sys.path.append(str(root))
        import importlib

        endpoint_module = importlib.import_module("core.endpoint")
        shell_module = importlib.import_module("core.shell_ops")
        factory = getattr(endpoint_module, "direct_endpoint", None) or getattr(
            endpoint_module, "resolve_endpoint", None
        )
        if factory is None or not hasattr(shell_module, "remote_bash"):
            raise RemoteDevUnavailable(
                "remote-dev checkout lacks core.endpoint.direct_endpoint/resolve_endpoint "
                "or core.shell_ops.remote_bash"
            )
        self._api = {"endpoint": factory, "remote_bash": shell_module.remote_bash, "root": root}
        return self._api

    def endpoint(self, target: dict[str, Any]) -> Any:
        if not target.get("host") or not target.get("port"):
            # Without both fields remote-dev would fall back to alias/session
            # resolution and could silently retarget a pool operation.
            raise ValueError("remote-dev calls require an explicit host and port")
        return self._load()["endpoint"](dict(target))

    def run(self, target: dict[str, Any], command: str, *, timeout_ms: int = 45000) -> dict[str, Any]:
        """Run one command and return remote-dev's result payload unchanged."""
        api = self._load()
        return api["remote_bash"](
            self.endpoint(target), command=command, timeout_ms=timeout_ms, runtime_env=False
        )["result"]

    def verify(self) -> Path:
        """Resolve the checkout and its required API, or fail closed.

        The manager calls this at startup so a missing or wrong
        `VAWS_REMOTE_DEV_ROOT` is a refusal to start, not a failure halfway
        through a first execution.
        """
        return self._load()["root"]

    def result_factory(self):
        """Prefer remote-dev's own result envelope when it is available."""
        try:
            self._load()
            import importlib

            return importlib.import_module("core.result").make_result
        except (RemoteDevUnavailable, ImportError, AttributeError):
            from vaws_result import make_result

            return make_result
