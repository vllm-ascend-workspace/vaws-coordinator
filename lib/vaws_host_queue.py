"""Client of the host NPU device-allocation authority.

This repository owns ``host/vaws_npu_coordination.py``. The scaffold consumes
that file from this checkout. There is exactly one implementation: two copies
would mean two owners of one host's device state.

The module is stdlib-only, shipped to the host and executed there. It exposes:

* ``handle_request(request) -> dict`` answering one JSON request
  (``status``/``submit``/``acquire``/``preflight``/``activate``/``heartbeat``/
  ``release``/``cancel``), carrying ``coordination_epoch``, ``task_id``,
  ``fence_token``, ``granted_devices`` and a task ``state``;
* ``CoordinationError`` for deterministic input failures, reported as
  ``{"status": "needs_input", ...}``;
* a ``failed``/``needs_input``/``probe_failed`` status for any answer that must
  not be read as an allocation.

``VAWS_HOST_QUEUE_MODULE`` overrides the bundled path; it is not required.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
from pathlib import Path
from typing import Any, Callable

HOST_QUEUE_MODULE_ENV = "VAWS_HOST_QUEUE_MODULE"
SOURCE_DELIMITER = "__VAWS_NPU_COORDINATION_SOURCE__"
UNRESOLVED = {"failed", "needs_input", "probe_failed"}
RUNNER = """
import sys as _sys
try:
    _request = json.loads(_sys.argv[1])
    _result = handle_request(_request)
    print(json.dumps(_result, indent=2, ensure_ascii=False, sort_keys=True))
except CoordinationError as _exc:
    print(json.dumps({"status": "needs_input", "error": str(_exc)}, indent=2, ensure_ascii=False, sort_keys=True))
    raise SystemExit(2)
except Exception as _exc:
    print(json.dumps({"status": "failed", "error": str(_exc)}, indent=2, ensure_ascii=False, sort_keys=True))
    raise SystemExit(2)
"""


class HostQueueUnavailable(RuntimeError):
    """An explicitly configured host-queue module path does not exist."""


_BUNDLED_HOST_QUEUE_MODULE = Path(__file__).resolve().parent.parent / "host" / "vaws_npu_coordination.py"


def host_queue_module_path(path: Path | str | None = None) -> Path:
    configured = str(path or os.environ.get(HOST_QUEUE_MODULE_ENV, ""))
    if not configured:
        return _BUNDLED_HOST_QUEUE_MODULE
    resolved = Path(configured).expanduser()
    if not resolved.is_file():
        raise HostQueueUnavailable(f"host coordination module not found: {resolved}")
    return resolved


def load_host_protocol(path: Path | str | None = None):
    """Import the host protocol locally (used by the control-plane tests)."""
    resolved = host_queue_module_path(path)
    spec = importlib.util.spec_from_file_location("vaws_npu_coordination", resolved)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HostQueue:
    """Send one host coordination request through an explicit shell callable."""

    def __init__(self, run: Callable[[dict[str, Any], str], str], *, module_path=None):
        self._run = run
        self._module_path = module_path
        self._source: str | None = None

    def source(self) -> str:
        if self._source is None:
            self._source = host_queue_module_path(self._module_path).read_text(encoding="utf-8")
        return self._source

    def command(self, request: dict[str, Any]) -> str:
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        return (
            "set -euo pipefail\n"
            "if command -v python3 >/dev/null 2>&1; then _py=python3; "
            "elif command -v python >/dev/null 2>&1; then _py=python; "
            "else echo '{\"status\":\"failed\",\"error\":\"python not found on host\"}'; exit 127; fi\n"
            f'"$_py" - {shlex.quote(payload)} <<\'{SOURCE_DELIMITER}\'\n'
            f"{self.source()}\n{RUNNER}\n{SOURCE_DELIMITER}\n"
        )

    def request(self, host_endpoint: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        target = {**host_endpoint, "root": "/", "cwd": "/"}
        # Build the request before the transport guard: a configuration error
        # must not be reported as an uncertain host contact.
        command = self.command(request)
        try:
            stdout = self._run(target, command)
        except Exception as exc:
            raise RuntimeError(
                "host coordination failed; inspect endpoint logs and reconcile before retry: "
                f"{exc}"
            ) from exc
        payload = json.loads(stdout)
        if payload.get("status") in UNRESOLVED:
            raise RuntimeError(payload.get("error", "host state unknown"))
        return payload
