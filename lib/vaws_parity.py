"""Adapter to the scaffold's remote-code-parity source materialization.

Parity owns source staging and materialization; this component only pins the
commits parity reports. The CLI stays scaffold-owned, so its location is
configuration rather than an import:

* ``VAWS_PARITY_SCRIPT`` — path to the scaffold's
  ``.agents/skills/remote-code-parity/scripts/remote_code_parity.py``.
* ``VAWS_PARITY_WORKSPACE_ROOT`` — value passed as ``--workspace-root``;
  defaults to the scaffold root derived from the script location, which
  reproduces the pre-split argument.

Required contract: ``sync --apply-mode materialize`` writes one JSON document
whose ``status`` is ``ready`` or ``materialized`` and whose
``snapshot_commits`` maps each source name to its materialized commit. Staging
alone never authorizes an execution.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PARITY_SCRIPT_ENV = "VAWS_PARITY_SCRIPT"
PARITY_WORKSPACE_ROOT_ENV = "VAWS_PARITY_WORKSPACE_ROOT"


class ParityUnavailable(RuntimeError):
    """Source materialization is not configured; no job may be launched."""


def parity_script() -> Path:
    configured = os.environ.get(PARITY_SCRIPT_ENV, "")
    if not configured:
        raise ParityUnavailable(
            f"source materialization is not configured; set {PARITY_SCRIPT_ENV} to the "
            "scaffold's remote_code_parity.py. No job was launched."
        )
    path = Path(configured).expanduser()
    if not path.is_file():
        raise ParityUnavailable(f"parity script not found: {path}. No job was launched.")
    return path


def workspace_root(script: Path) -> Path:
    configured = os.environ.get(PARITY_WORKSPACE_ROOT_ENV, "")
    if configured:
        return Path(configured).expanduser()
    parents = script.resolve().parents
    # .agents/skills/remote-code-parity/scripts/<script> -> scaffold root
    return parents[4] if len(parents) > 4 else parents[-1]


def materialize_command(*, workspace_id: str, runtime_id: str, endpoint: dict,
                        sources: dict[str, str]) -> list[str]:
    script = parity_script()
    command = [sys.executable, str(script), "sync",
               "--workspace-root", str(workspace_root(script)),
               "--workspace-id", workspace_id,
               "--server-name", runtime_id,
               "--runtime-root", endpoint["root"],
               "--container-identity", runtime_id,
               "--container-host", endpoint["host"],
               "--container-port", str(endpoint["port"]),
               "--container-user", endpoint["user"],
               "--apply-mode", "materialize"]
    for name, path in sources.items():
        command.extend(["--source", name + "=" + path])
    return command
