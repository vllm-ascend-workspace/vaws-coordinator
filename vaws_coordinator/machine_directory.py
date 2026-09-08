"""Read-only view of the shared machine directory.

The inventory file is written and validated by the scaffold's machine
management (`machine_add.py`, `vaws_remote_toolbox`); this component only
reads aliases so an administrator can register a prepared container by alias
instead of retyping endpoints. Reading a directory is not an allocation.

Configure the file with ``VAWS_MACHINE_INVENTORY``. Without it, registration
still works with explicit ``host_endpoint``/``endpoint`` fields, and
``machine_catalog`` fails closed instead of inventing an empty fleet.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

MACHINE_INVENTORY_ENV = "VAWS_MACHINE_INVENTORY"


class MachineDirectoryUnavailable(RuntimeError):
    """No shared machine directory is configured for this manager."""


class MachineDirectory:
    def __init__(self, path: Path | str | None = None):
        self._path = Path(path).expanduser() if path else None

    def path(self) -> Path:
        if self._path is None:
            configured = os.environ.get(MACHINE_INVENTORY_ENV, "")
            if not configured:
                raise MachineDirectoryUnavailable(
                    f"machine directory is not configured; set {MACHINE_INVENTORY_ENV} to the "
                    "shared inventory file, or register runtimes with explicit endpoints"
                )
            self._path = Path(configured).expanduser()
        return self._path

    def load(self) -> tuple[dict[str, Any], Path]:
        path = self.path().resolve()
        if not path.is_file():
            raise MachineDirectoryUnavailable(f"machine inventory not found at {path}")
        inventory = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(inventory, dict) or not isinstance(inventory.get("machines"), list):
            raise MachineDirectoryUnavailable(f"{path} is not a machine inventory document")
        return inventory, path

    def catalog(self) -> dict[str, Any]:
        inventory, path = self.load()
        return {"inventory_path": str(path), "machines": [
            {"alias": row.get("alias"), "host": row.get("host", {}).get("ip"),
             "container_name": row.get("container", {}).get("name"),
             "container_port": row.get("container", {}).get("ssh_port")}
            for row in inventory["machines"]]}

    def host(self, alias: str) -> dict[str, Any]:
        inventory, _ = self.load()
        matches = [row for row in inventory["machines"] if row.get("alias") == alias]
        if len(matches) != 1:
            raise ValueError("machine alias must resolve uniquely in the shared inventory")
        return matches[0]["host"]
