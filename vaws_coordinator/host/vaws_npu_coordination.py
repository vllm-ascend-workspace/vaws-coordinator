#!/usr/bin/env python3
"""Cooperative host-local NPU task coordination.

The coordinator is deliberately advisory.  It gives independent VAWS agents a
shared queue and lease ledger on one host, but it does not prevent an operator
or a non-participating process from using an NPU.  Observed hardware occupancy
always wins over declarations.

The module is stdlib-only because the agent-facing wrapper sends this source to
the bare-metal host and executes it there.  State defaults to
``/tmp/vaws-npu-coordinator/v1`` (override with ``VAWS_NPU_COORDINATOR_STATE_DIR``
or ``request["state_dir"]``) and is expected to disappear after a host or
``/tmp`` reset; a missing database simply starts a new coordination epoch.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

SCHEMA_VERSION = 4
DEFAULT_STATE_DIR = "/tmp/vaws-npu-coordinator/v1"
HOST_STATE_DIR_ENV = "VAWS_NPU_COORDINATOR_STATE_DIR"
DEFAULT_CONTAINER_SSH_PORT_RANGE = "46000:46999"
DEFAULT_SERVING_PORT_RANGE = "30000:45999"
SESSION_HOLD_HORIZON_SECONDS = 365 * 24 * 3600


def resolve_host_state_dir(explicit: str | Path | None = None) -> str:
    """Return the host coordination state directory.

    Precedence: explicit argument, then ``VAWS_NPU_COORDINATOR_STATE_DIR``,
    then ``DEFAULT_STATE_DIR``.
    """
    if explicit:
        return str(explicit)
    return os.environ.get(HOST_STATE_DIR_ENV) or DEFAULT_STATE_DIR
DEFAULT_QUEUE_TTL_SECONDS = 3600
DEFAULT_GRANT_TTL_SECONDS = 60
DEFAULT_START_TTL_SECONDS = 60
DEFAULT_HEARTBEAT_TTL_SECONDS = 120
DEFAULT_ESTIMATED_DURATION_SECONDS = 3600
HBM_BUSY_THRESHOLD_MB = 4096

TASK_STATES = {
    "queued",
    "granted",
    "starting",
    "active",
    "orphaned_busy",
    "session_reserved",
    "released",
    "expired",
    "cancelled",
}
RESERVING_TASK_STATES = {"granted", "starting", "active", "orphaned_busy", "session_reserved"}
PORT_KINDS = {"container_ssh", "service"}
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")


class CoordinationError(RuntimeError):
    """Raised for deterministic coordinator input or state failures."""


def process_guard_busy(value: str | dict | None, *, completion_confirmed: bool = False) -> bool:
    """Retain a managed lease while its process family can still use devices.

    Empty NPU occupancy during CPU initialization is not proof of completion.
    Subreaper jobs additionally require the manager's drained-process receipt;
    a crashed supervisor may have lost clean-environment descendants. A marker
    scan alone can never release these leases. Unreadable state retains them.
    """
    if not value:
        return False
    try:
        guard = json.loads(value) if isinstance(value, str) else value
        if not re.fullmatch(r"[0-9a-f]{32}", guard["marker"]):
            return True
        if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != guard["boot_id"]:
            return True
        marker = ("VAWS_REMOTE_JOB_TOKEN=" + guard["marker"]).encode()
        unknown = False
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                state = (process / "stat").read_text().rsplit(") ", 1)[1].split()[0]
                if state != "Z" and marker in (process / "environ").read_bytes().split(b"\0"):
                    return True
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                unknown = True
        return unknown or (guard.get("retain_until_release") is True and not completion_confirmed)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return True


def utc_now_iso(epoch: float | None = None) -> str:
    value = time.time() if epoch is None else epoch
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_instant(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CoordinationError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise CoordinationError(f"timestamp must include a timezone: {value!r}")
    return parsed.timestamp()


def require_safe_id(value: str | None, *, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise CoordinationError(
            f"invalid {label}: use 3-128 characters from A-Z a-z 0-9 _ . : -"
        )
    return value


def parse_devices(value: Any, *, allow_none: bool = True) -> list[int] | None:
    if value is None:
        if allow_none:
            return None
        raise CoordinationError("devices are required")
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raise CoordinationError("devices must be a comma-separated string or list")
    devices: list[int] = []
    seen: set[int] = set()
    for raw in raw_items:
        token = str(raw).strip()
        if not token:
            raise CoordinationError("devices contains an empty device id")
        try:
            device = int(token, 10)
        except ValueError as exc:
            raise CoordinationError(f"devices contains a non-integer id: {token!r}") from exc
        if device < 0:
            raise CoordinationError(f"devices contains a negative id: {device}")
        if device in seen:
            raise CoordinationError(f"devices contains a duplicate id: {device}")
        seen.add(device)
        devices.append(device)
    if not devices:
        raise CoordinationError("devices must not be empty")
    return sorted(devices)


def _json_devices(value: list[int] | None) -> str | None:
    return None if value is None else json.dumps(sorted(value), separators=(",", ":"))


def _load_devices(value: str | None) -> list[int]:
    if not value:
        return []
    loaded = json.loads(value)
    return [int(item) for item in loaded]


def parse_port_range(value: str) -> tuple[int, int]:
    start_s, sep, end_s = str(value).partition(":")
    if not sep:
        raise CoordinationError(f"port range must be START:END, got {value!r}")
    try:
        start = int(start_s)
        end = int(end_s)
    except ValueError as exc:
        raise CoordinationError(f"invalid port range: {value!r}") from exc
    if start <= 0 or end <= 0 or start > end or end > 65535:
        raise CoordinationError(f"invalid port range: {value!r}")
    return start, end


def _parse_listening_port_text(output: str) -> set[int]:
    ports: set[int] = set()
    for line in output.splitlines():
        match = re.search(r"[:.](\d+)$", line.strip())
        if match:
            ports.add(int(match.group(1)))
    return ports


def probe_listening_ports() -> dict[str, Any]:
    """Return host TCP listen ports from ss, then netstat."""
    commands = (
        ["ss", "-ltnH"],
        ["netstat", "-ltn"],
    )
    last_error = "ss and netstat are unavailable"
    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_error = str(exc)
            continue
        if result.returncode != 0:
            last_error = (result.stderr or result.stdout or f"{command[0]} failed")[-2000:]
            continue
        stdout = result.stdout or ""
        if command[0] == "netstat":
            addresses = []
            for line in stdout.splitlines()[2:]:
                fields = line.split()
                if len(fields) >= 4:
                    addresses.append(fields[3])
            stdout = "\n".join(addresses)
        else:
            addresses = []
            for line in stdout.splitlines():
                fields = line.split()
                if len(fields) >= 4:
                    addresses.append(fields[3])
            stdout = "\n".join(addresses)
        return {
            "status": "ok",
            "ports": sorted(_parse_listening_port_text(stdout)),
            "source": command[0],
        }
    return {"status": "failed", "error": last_error, "ports": []}


def probe_named_container(name: str) -> dict[str, Any]:
    """Report whether a docker container name exists and is running."""
    if not name:
        return {"status": "ok", "exists": False, "running": False, "name": name}
    try:
        listed = subprocess.run(
            ["docker", "container", "ls", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "error": str(exc), "exists": None, "running": None, "name": name}
    if listed.returncode != 0:
        return {
            "status": "failed",
            "error": "host Docker state unavailable",
            "exists": None,
            "running": None,
            "name": name,
            "stderr": (listed.stderr or listed.stdout)[-2000:],
        }
    exists = name in listed.stdout.splitlines()
    running = False
    if exists:
        try:
            inspected = subprocess.run(
                ["docker", "inspect", "--type", "container", name],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"status": "failed", "error": str(exc), "exists": True, "running": None, "name": name}
        if inspected.returncode != 0:
            return {
                "status": "failed",
                "error": "docker inspect failed",
                "exists": True,
                "running": None,
                "name": name,
            }
        try:
            payload = json.loads(inspected.stdout)
            running = bool(payload[0]["State"]["Running"])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return {"status": "failed", "error": str(exc), "exists": True, "running": None, "name": name}
    return {"status": "ok", "exists": exists, "running": running, "name": name}


def _parse_npu_smi_device_table(
    output: str,
) -> tuple[set[int], dict[int, dict[str, int]], dict[tuple[int, int], int]]:
    """Visible Phy-IDs, HBM used/total, and (NPU, Chip) -> Phy-ID mapping."""
    dev_ids: set[int] = set()
    header_ids: set[int] = set()
    hbm: dict[int, dict[str, int]] = {}
    chip_devices: dict[tuple[int, int], int] = {}
    current_npu: int | None = None
    for line in output.splitlines():
        if "0000:" in line:
            chip = re.match(r"\|\s*(\d+)\s+(\d+)\s+\|.*0000:", line)
            if chip:
                dev_id = int(chip.group(2))
                if current_npu is not None:
                    chip_devices[(current_npu, int(chip.group(1)))] = dev_id
            elif current_npu is not None:
                dev_id = current_npu
            else:
                continue
            dev_ids.add(dev_id)
            pairs = re.findall(r"(\d+)\s*/\s*(\d+)", line)
            if len(pairs) >= 2:
                hbm[dev_id] = {
                    "used_mb": int(pairs[-1][0]),
                    "total_mb": int(pairs[-1][1]),
                }
            continue
        header = re.match(r"\|\s*(\d+)\s+\d*\w+\d+\w*\s+\|", line)
        if header:
            current_npu = int(header.group(1))
            header_ids.add(current_npu)
    if not dev_ids:
        dev_ids.update(header_ids)
    return dev_ids, hbm, chip_devices


def parse_npu_smi_hbm(output: str) -> dict[int, dict[str, int]]:
    """Physical-device HBM used/total from npu-smi info text.

    Does not require a process table; occupancy failure still returns HBM.
    """
    _dev_ids, hbm, _chip_devices = _parse_npu_smi_device_table(output)
    return dict(sorted(hbm.items()))


def parse_npu_smi_info(output: str) -> dict[str, Any]:
    """Parse visible devices plus process/HBM occupancy from common layouts."""
    dev_ids, hbm, chip_devices = _parse_npu_smi_device_table(output)
    lines = output.splitlines()

    process_busy: dict[int, list[dict[str, Any]]] = {}
    in_process_table = False
    process_error = None
    for line in lines:
        if "Process name" in line or "Process memory" in line:
            in_process_table = True
            continue
        if in_process_table and "No running processes" in line:
            continue
        if in_process_table and line.startswith("|"):
            columns = [column.strip() for column in line.split("|")[1:-1]]
            # A3: | NPU Chip | PID | Process name | Memory |. Phy-ID from
            # the device table is the allocation identity, not the NPU index.
            if len(columns) >= 3 and re.fullmatch(r"\d+(?:\s+\d+)?", columns[0]):
                identity = [int(value) for value in columns[0].split()]
                if len(identity) == 2:
                    device = chip_devices.get(tuple(identity))
                elif chip_devices:
                    # A parsed (NPU, Chip) -> Phy-ID table means a multi-chip
                    # layout. A lone column is then an NPU index, not a phy-id;
                    # guessing would attach the process to the wrong device and
                    # report the real one as free. Fail closed instead.
                    device = None
                else:
                    device = identity[0]
                if device not in dev_ids or not columns[1].isdigit() or not columns[2]:
                    process_error = "npu-smi process row has an unknown device or invalid PID/name"
                    break
                process_busy.setdefault(device, []).append(
                    {"kind": "process", "pid": int(columns[1]), "name": columns[2]}
                )
                continue
            # Preserve the older flat device/chip/PID/owner/name layout.
            match = re.match(r"\|\s*(\d+)\s+\S+\s+(\d+)\s+(\S+)\s+(\S+)", line)
            if match:
                device = int(match.group(1))
                if device not in dev_ids:
                    process_error = "npu-smi process row has an unknown device"
                    break
                process_busy.setdefault(device, []).append(
                    {
                        "kind": "process",
                        "pid": int(match.group(2)),
                        "owner": match.group(3),
                        "name": match.group(4),
                    }
                )
            elif any(columns):
                process_error = "npu-smi process row could not be parsed"
                break

    if process_error or not in_process_table:
        return {"status": "failed", "error": process_error or "npu-smi process table is missing",
                "devices": sorted(dev_ids), "busy": {}, "free": []}

    busy: dict[int, list[dict[str, Any]]] = {}
    for device in sorted(dev_ids):
        reasons = list(process_busy.get(device, []))
        usage = hbm.get(device)
        if usage and usage["used_mb"] >= HBM_BUSY_THRESHOLD_MB and not reasons:
            reasons.append(
                {
                    "kind": "hbm_threshold",
                    "hbm_used_mb": usage["used_mb"],
                    "threshold_mb": HBM_BUSY_THRESHOLD_MB,
                }
            )
        if reasons:
            busy[device] = reasons
    return {
        "status": "ok",
        "collected_at": utc_now_iso(),
        "devices": sorted(dev_ids),
        "busy": {str(key): value for key, value in sorted(busy.items())},
        "free": sorted(device for device in dev_ids if device not in busy),
        "hbm": {str(key): value for key, value in sorted(hbm.items())},
        "hbm_busy_threshold_mb": HBM_BUSY_THRESHOLD_MB,
    }


def probe_npu_occupancy() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["npu-smi", "info"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "error": str(exc), "devices": [], "busy": {}, "free": []}
    if result.returncode != 0:
        return {
            "status": "failed",
            "error": "npu-smi info failed",
            "returncode": result.returncode,
            "stderr": (result.stderr or result.stdout)[-2000:],
            "devices": [],
            "busy": {},
            "free": [],
        }
    parsed = parse_npu_smi_info(result.stdout)
    if not parsed["devices"]:
        return {
            "status": "failed",
            "error": "npu-smi output did not contain any parseable devices",
            "devices": [],
            "busy": {},
            "free": [],
        }
    return parsed


class NpuCoordinator:
    """SQLite-backed cooperative queue for one bare-metal host."""

    def __init__(
        self,
        state_dir: str | Path = DEFAULT_STATE_DIR,
        *,
        clock: Callable[[], float] = time.time,
        expected_epoch: str | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.db_path = self.state_dir / "coordinator.sqlite3"
        self.clock = clock
        self.expected_epoch = expected_epoch
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    agent_alias TEXT,
                    session_id TEXT,
                    container_name TEXT,
                    requested_count INTEGER NOT NULL,
                    requested_devices TEXT,
                    not_before REAL NOT NULL,
                    latest_start REAL NOT NULL,
                    estimated_duration_seconds INTEGER NOT NULL,
                    preemptible INTEGER NOT NULL DEFAULT 0,
                    priority INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    submitted_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    granted_devices TEXT,
                    activation_deadline REAL,
                    fence_token INTEGER,
                    started_at REAL,
                    expected_end REAL,
                    heartbeat_at REAL,
                    heartbeat_deadline REAL,
                    pid INTEGER,
                    message TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_queue
                    ON tasks(state, priority DESC, submitted_at ASC);
                CREATE TABLE IF NOT EXISTS holds (
                    hold_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    devices TEXT NOT NULL,
                    not_before REAL NOT NULL,
                    end_at REAL NOT NULL,
                    reason TEXT,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_holds_window
                    ON holds(state, not_before, end_at);
                CREATE TABLE IF NOT EXISTS ports (
                    port INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ports_task
                    ON ports(task_id);
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at REAL NOT NULL,
                    kind TEXT NOT NULL,
                    task_id TEXT,
                    data TEXT NOT NULL
                );
                """
            )
            task_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tasks)")
            }
            if "agent_alias" not in task_columns:
                try:
                    connection.execute("ALTER TABLE tasks ADD COLUMN agent_alias TEXT")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
            if "process_guard" not in task_columns:
                try:
                    connection.execute("ALTER TABLE tasks ADD COLUMN process_guard TEXT")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
            # Older clients send the host protocol source with each request.
            # They do not know about process guards. Do not let their generic
            # "hardware free" UPDATE release a guarded CPU-initializing job.
            connection.execute("""
                CREATE TRIGGER IF NOT EXISTS protect_managed_process_release
                BEFORE UPDATE OF state ON tasks
                WHEN OLD.process_guard IS NOT NULL AND NEW.process_guard IS NOT NULL
                     AND NEW.state IN ('released', 'cancelled', 'expired')
                BEGIN
                    SELECT RAISE(ABORT, 'managed process guard must be verified before release');
                END
            """)
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('coordination_epoch', ?)",
                (str(uuid.uuid4()),),
            )
            connection.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('next_fence', '0')")
        finally:
            connection.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if self.expected_epoch is not None:
                epoch = connection.execute("SELECT value FROM meta WHERE key='coordination_epoch'").fetchone()
                if epoch is None or epoch["value"] != self.expected_epoch:
                    raise CoordinationError("coordination epoch changed; reconcile ownership before retrying")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _event(
        self,
        connection: sqlite3.Connection,
        kind: str,
        *,
        task_id: str | None = None,
        data: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO events(at, kind, task_id, data) VALUES(?, ?, ?, ?)",
            (self.clock() if now is None else now, kind, task_id, json.dumps(data or {}, sort_keys=True)),
        )

    def _next_fence(self, connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT value FROM meta WHERE key='next_fence'").fetchone()
        value = int(row["value"]) + 1
        connection.execute("UPDATE meta SET value=? WHERE key='next_fence'", (str(value),))
        return value

    @staticmethod
    def _busy_set(observed: dict[str, Any] | None) -> set[int] | None:
        if observed is None or observed.get("status") != "ok":
            return None
        return {int(key) for key in observed.get("busy", {})}

    @staticmethod
    def _serialize_task(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        payload["requested_devices"] = _load_devices(payload.get("requested_devices")) or None
        payload["granted_devices"] = _load_devices(payload.get("granted_devices"))
        payload["preemptible"] = bool(payload.get("preemptible"))
        payload["process_guarded"] = bool(payload.pop("process_guard", None))
        for key in (
            "not_before",
            "latest_start",
            "submitted_at",
            "updated_at",
            "activation_deadline",
            "started_at",
            "expected_end",
            "heartbeat_at",
            "heartbeat_deadline",
        ):
            if payload.get(key) is not None:
                payload[key] = utc_now_iso(float(payload[key]))
        return payload

    @staticmethod
    def _serialize_hold(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        payload["devices"] = _load_devices(payload.get("devices"))
        for key in ("not_before", "end_at", "created_at", "updated_at"):
            payload[key] = utc_now_iso(float(payload[key]))
        return payload

    def _task_row(self, connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise CoordinationError(f"task not found: {task_id}")
        return row

    def _check_token(self, row: sqlite3.Row, token: int) -> None:
        if row["fence_token"] is None or int(row["fence_token"]) != int(token):
            raise CoordinationError(f"stale or invalid fencing token for task {row['task_id']}")

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        task_id = require_safe_id(request.get("task_id") or str(uuid.uuid4()), label="task id")
        agent_id = require_safe_id(request.get("agent_id"), label="agent id")
        agent_alias = request.get("agent_alias")
        if agent_alias is not None:
            agent_alias = require_safe_id(agent_alias, label="agent alias")
        session_id = request.get("session_id")
        if session_id is not None:
            session_id = require_safe_id(session_id, label="session id")
        devices = parse_devices(request.get("devices"))
        count_raw = request.get("npu_count")
        if devices is not None and count_raw is not None:
            raise CoordinationError("use only one of devices or npu_count")
        if devices is None:
            count = int(count_raw or 0)
            if count < 1:
                raise CoordinationError("npu_count must be >= 1 when devices are not specified")
        else:
            count = len(devices)
        duration = int(request.get("estimated_duration_seconds") or DEFAULT_ESTIMATED_DURATION_SECONDS)
        if duration < 1:
            raise CoordinationError("estimated_duration_seconds must be >= 1")
        not_before = parse_instant(request.get("not_before"))
        if not_before is None:
            not_before = now
        latest_start = parse_instant(request.get("latest_start"))
        if latest_start is None:
            queue_ttl = int(request.get("queue_ttl_seconds") or DEFAULT_QUEUE_TTL_SECONDS)
            if queue_ttl < 1:
                raise CoordinationError("queue_ttl_seconds must be >= 1")
            latest_start = now + queue_ttl
        if latest_start < not_before:
            raise CoordinationError("latest_start must not be earlier than not_before")
        priority = int(request.get("priority") or 0)
        container_name = request.get("container_name")
        with self._transaction() as connection:
            existing = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if existing is not None:
                expected = {
                    "agent_id": agent_id,
                    "agent_alias": agent_alias,
                    "session_id": session_id,
                    "container_name": container_name,
                    "requested_count": count,
                    "requested_devices": _json_devices(devices),
                }
                mismatched = {
                    key: {"existing": existing[key], "requested": value}
                    for key, value in expected.items()
                    if existing[key] != value
                }
                if mismatched:
                    raise CoordinationError(
                        f"task id {task_id} already exists with different ownership or resources: "
                        f"{json.dumps(mismatched, sort_keys=True)}"
                    )
                return {"status": "ok", "reused": True, "task": self._serialize_task(existing)}
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, agent_id, agent_alias, session_id, container_name,
                    requested_count, requested_devices, not_before, latest_start,
                    estimated_duration_seconds, preemptible, priority, state,
                    submitted_at, updated_at, message
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    task_id,
                    agent_id,
                    agent_alias,
                    session_id,
                    container_name,
                    count,
                    _json_devices(devices),
                    not_before,
                    latest_start,
                    duration,
                    1 if request.get("preemptible") else 0,
                    priority,
                    now,
                    now,
                    request.get("message"),
                ),
            )
            self._event(connection, "task-submitted", task_id=task_id, data={"count": count}, now=now)
            row = self._task_row(connection, task_id)
        return {"status": "queued", "reused": False, "task": self._serialize_task(row)}

    def add_hold(self, request: dict[str, Any], observed: dict[str, Any] | None = None) -> dict[str, Any]:
        now = self.clock()
        hold_id = require_safe_id(request.get("hold_id") or str(uuid.uuid4()), label="hold id")
        owner = require_safe_id(request.get("owner"), label="hold owner")
        devices = parse_devices(request.get("devices"), allow_none=False) or []
        not_before = parse_instant(request.get("not_before"))
        if not_before is None:
            not_before = now
        end_at = parse_instant(request.get("end_at"))
        if end_at is None:
            duration = int(request.get("duration_seconds") or 0)
            if duration < 1:
                raise CoordinationError("a hold requires end_at or duration_seconds >= 1")
            end_at = not_before + duration
        if end_at <= not_before:
            raise CoordinationError("hold end_at must be later than not_before")
        with self._transaction() as connection:
            existing = connection.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
            if existing is not None:
                expected = {
                    "owner": owner,
                    "devices": _json_devices(devices),
                    "not_before": not_before,
                    "end_at": end_at,
                }
                mismatched = {
                    key: {"existing": existing[key], "requested": value}
                    for key, value in expected.items()
                    if existing[key] != value
                }
                if mismatched:
                    raise CoordinationError(
                        f"hold id {hold_id} already exists with different ownership, devices, or window: "
                        f"{json.dumps(mismatched, sort_keys=True)}"
                    )
                return {"status": "ok", "reused": True, "hold": self._serialize_hold(existing)}
            connection.execute(
                """
                INSERT INTO holds(
                    hold_id, owner, devices, not_before, end_at, reason,
                    state, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (hold_id, owner, _json_devices(devices), not_before, end_at, request.get("reason"), now, now),
            )
            self._event(connection, "hold-added", data={"hold_id": hold_id, "devices": devices}, now=now)
            conflicts: list[dict[str, Any]] = []
            for task in connection.execute(
                "SELECT * FROM tasks WHERE state IN ('granted','starting','active','orphaned_busy','session_reserved')"
            ).fetchall():
                overlap = sorted(set(devices) & set(_load_devices(task["granted_devices"])))
                if overlap:
                    conflicts.append({"task_id": task["task_id"], "devices": overlap, "state": task["state"]})
            busy = self._busy_set(observed)
            if busy:
                overlap = sorted(set(devices) & busy)
                if overlap:
                    conflicts.append({"external_busy": True, "devices": overlap})
            row = connection.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
        return {
            "status": "recorded_with_conflicts" if conflicts else "recorded",
            "hold": self._serialize_hold(row),
            "conflicts": conflicts,
        }

    def remove_hold(self, hold_id: str) -> dict[str, Any]:
        hold_id = require_safe_id(hold_id, label="hold id")
        now = self.clock()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
            if row is None:
                raise CoordinationError(f"hold not found: {hold_id}")
            connection.execute(
                "UPDATE holds SET state='cancelled', updated_at=? WHERE hold_id=?",
                (now, hold_id),
            )
            self._event(connection, "hold-cancelled", data={"hold_id": hold_id}, now=now)
            row = connection.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
        return {"status": "cancelled", "hold": self._serialize_hold(row)}

    def _housekeep(
        self,
        connection: sqlite3.Connection,
        observed: dict[str, Any] | None,
        *,
        now: float,
    ) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        busy = self._busy_set(observed)
        visible = (
            {int(device) for device in observed.get("devices", [])}
            if observed is not None and observed.get("status") == "ok"
            else None
        )

        for row in connection.execute("SELECT * FROM holds WHERE state='active' AND end_at<=?", (now,)).fetchall():
            connection.execute("UPDATE holds SET state='expired', updated_at=? WHERE hold_id=?", (now, row["hold_id"]))
            changes.append({"hold_id": row["hold_id"], "from": "active", "to": "expired"})
            self._event(connection, "hold-expired", data={"hold_id": row["hold_id"]}, now=now)

        for row in connection.execute(
            "SELECT * FROM tasks WHERE state='queued' AND latest_start<=?", (now,)
        ).fetchall():
            connection.execute(
                "UPDATE tasks SET state='expired', updated_at=?, message=? WHERE task_id=?",
                (now, "latest_start elapsed before grant", row["task_id"]),
            )
            changes.append({"task_id": row["task_id"], "from": "queued", "to": "expired"})
            self._event(connection, "task-expired-in-queue", task_id=row["task_id"], now=now)

        deadline_rows = connection.execute(
            """
            SELECT * FROM tasks
            WHERE state IN ('granted','starting')
              AND activation_deadline IS NOT NULL
              AND activation_deadline<=?
            """,
            (now,),
        ).fetchall()
        for row in deadline_rows:
            devices = set(_load_devices(row["granted_devices"]))
            still_busy = (
                busy is None
                or visible is None
                or not devices.issubset(visible)
                or bool(devices & busy)
            )
            next_state = "orphaned_busy" if still_busy else "expired"
            message = (
                "activation deadline elapsed; occupancy still busy or unknown"
                if still_busy
                else "activation deadline elapsed before task became active"
            )
            connection.execute(
                "UPDATE tasks SET state=?, updated_at=?, message=? WHERE task_id=?",
                (next_state, now, message, row["task_id"]),
            )
            changes.append({"task_id": row["task_id"], "from": row["state"], "to": next_state})
            self._event(
                connection,
                "activation-deadline-elapsed",
                task_id=row["task_id"],
                data={"to": next_state},
                now=now,
            )

        heartbeat_rows = connection.execute(
            """
            SELECT * FROM tasks
            WHERE state='active'
              AND heartbeat_deadline IS NOT NULL
              AND heartbeat_deadline<=?
            """,
            (now,),
        ).fetchall()
        for row in heartbeat_rows:
            devices = set(_load_devices(row["granted_devices"]))
            still_busy = (
                busy is None
                or visible is None
                or not devices.issubset(visible)
                or bool(devices & busy)
                or process_guard_busy(row["process_guard"])
            )
            next_state = "orphaned_busy" if still_busy else "released"
            message = (
                "heartbeat expired while hardware remained busy or unknown"
                if still_busy
                else "heartbeat expired and hardware was observed free"
            )
            connection.execute(
                "UPDATE tasks SET state=?, process_guard=CASE WHEN ?='released' THEN NULL ELSE process_guard END, updated_at=?, message=? WHERE task_id=?",
                (next_state, next_state, now, message, row["task_id"]),
            )
            changes.append({"task_id": row["task_id"], "from": "active", "to": next_state})
            self._event(connection, "heartbeat-expired", task_id=row["task_id"], data={"to": next_state}, now=now)

        if busy is not None and visible is not None:
            for row in connection.execute("SELECT * FROM tasks WHERE state='orphaned_busy'").fetchall():
                devices = set(_load_devices(row["granted_devices"]))
                if devices.issubset(visible) and not devices.intersection(busy) and not process_guard_busy(row["process_guard"]):
                    connection.execute(
                        "UPDATE tasks SET state='released', process_guard=NULL, updated_at=?, message=? WHERE task_id=?",
                        (now, "orphaned task hardware is now free", row["task_id"]),
                    )
                    changes.append({"task_id": row["task_id"], "from": "orphaned_busy", "to": "released"})
                    self._event(connection, "orphaned-task-released", task_id=row["task_id"], now=now)

        for row in connection.execute(
            "SELECT * FROM tasks WHERE state='active' AND expected_end IS NOT NULL AND expected_end<=?",
            (now,),
        ).fetchall():
            if row["message"] != "estimated duration exceeded; lease remains protected":
                connection.execute(
                    "UPDATE tasks SET updated_at=?, message=? WHERE task_id=?",
                    (now, "estimated duration exceeded; lease remains protected", row["task_id"]),
                )
                changes.append({"task_id": row["task_id"], "state": "active", "overdue": True})
                self._event(connection, "task-overdue", task_id=row["task_id"], now=now)
        return changes

    @staticmethod
    def _reserved_devices(connection: sqlite3.Connection, *, exclude_task: str | None = None) -> set[int]:
        reserved: set[int] = set()
        query = (
            "SELECT task_id, granted_devices FROM tasks "
            "WHERE state IN ('granted','starting','active','orphaned_busy','session_reserved')"
        )
        for row in connection.execute(query).fetchall():
            if exclude_task and row["task_id"] == exclude_task:
                continue
            reserved.update(_load_devices(row["granted_devices"]))
        return reserved

    @staticmethod
    def _hold_conflicts(
        connection: sqlite3.Connection,
        *,
        device: int,
        start_at: float,
        end_at: float,
    ) -> bool:
        for row in connection.execute(
            """
            SELECT devices FROM holds
            WHERE state='active' AND not_before<? AND end_at>?
            """,
            (end_at, start_at),
        ).fetchall():
            if device in _load_devices(row["devices"]):
                return True
        return False

    def _coordination_epoch(self, connection: sqlite3.Connection) -> str:
        row = connection.execute("SELECT value FROM meta WHERE key='coordination_epoch'").fetchone()
        if row is None:
            raise CoordinationError("coordination epoch is missing")
        return str(row["value"])

    @staticmethod
    def _serialize_port(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        payload["created_at"] = utc_now_iso(float(payload["created_at"]))
        return payload

    def _task_ports(self, connection: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM ports WHERE task_id=? ORDER BY port ASC", (task_id,)
        ).fetchall()
        return [item for item in (self._serialize_port(row) for row in rows) if item is not None]

    def _available_devices(
        self,
        connection: sqlite3.Connection,
        observed: dict[str, Any],
        *,
        exclude_task: str | None,
        start_at: float,
        end_at: float,
    ) -> list[int]:
        busy = self._busy_set(observed) or set()
        reserved = self._reserved_devices(connection, exclude_task=exclude_task)
        available: list[int] = []
        for device in observed.get("devices", []):
            device = int(device)
            if device in busy or device in reserved:
                continue
            if self._hold_conflicts(connection, device=device, start_at=start_at, end_at=end_at):
                continue
            available.append(device)
        return available

    def _select_granted_devices(
        self,
        *,
        requested: list[int],
        count: int,
        available: list[int],
    ) -> tuple[list[int] | None, list[int]]:
        if requested:
            missing = sorted(set(requested) - set(available))
            if missing:
                return None, missing
            return requested, []
        if len(available) < count:
            return None, []
        return sorted(available)[:count], []

    def _allocated_ports(self, connection: sqlite3.Connection, *, exclude_task: str | None = None) -> set[int]:
        allocated: set[int] = set()
        for row in connection.execute("SELECT port, task_id FROM ports").fetchall():
            if exclude_task and row["task_id"] == exclude_task:
                continue
            allocated.add(int(row["port"]))
        return allocated

    def _pick_port(
        self,
        connection: sqlite3.Connection,
        *,
        preferred: int | None,
        port_range: str,
        listening: dict[str, Any],
        kind: str,
        task_id: str,
        owner: str,
        now: float,
        exclude_task: str | None = None,
    ) -> int:
        if kind not in PORT_KINDS:
            raise CoordinationError(f"unsupported port kind: {kind!r}")
        if listening.get("status") != "ok":
            raise CoordinationError(
                listening.get("error") or "host listening ports are unavailable"
            )
        start, end = parse_port_range(port_range)
        allocated = self._allocated_ports(connection, exclude_task=exclude_task)
        live = {int(port) for port in listening.get("ports", [])}
        owned_kinds = {
            int(row["port"]): str(row["kind"])
            for row in connection.execute("SELECT port, kind FROM ports WHERE task_id=?", (task_id,)).fetchall()
        }
        candidates: list[int] = [preferred] if preferred is not None else list(range(start, end + 1))
        for port in candidates:
            if port is None:
                continue
            if port < start or port > end:
                raise CoordinationError(f"port {port} is outside allowed range {port_range}")
            existing_kind = owned_kinds.get(port)
            if existing_kind is not None:
                if existing_kind != kind:
                    if preferred is not None:
                        raise CoordinationError(
                            f"port {port} is already reserved as {existing_kind} for this session"
                        )
                    continue
                return port
            if port in allocated or port in live:
                if preferred is not None:
                    raise CoordinationError(f"port {port} is already reserved or bound on the host")
                continue
            connection.execute(
                "INSERT INTO ports(port, kind, task_id, owner, created_at) VALUES(?, ?, ?, ?, ?)",
                (port, kind, task_id, owner, now),
            )
            return port
        raise CoordinationError(f"no free port found in range {port_range}")

    def _reject_durable_session(self, row: sqlite3.Row, *, operation: str) -> None:
        if row["state"] == "session_reserved":
            raise CoordinationError(
                f"durable session reservation cannot be {operation}; "
                "use session-release with workspace, session, epoch, and fencing token"
            )

    def _check_session_owner(self, row: sqlite3.Row, request: dict[str, Any], token: int) -> None:
        self._check_token(row, token)
        workspace_id = require_safe_id(request.get("workspace_id"), label="workspace id")
        session_id = require_safe_id(request.get("session_id"), label="session id")
        if row["agent_id"] != workspace_id or row["session_id"] != session_id:
            raise CoordinationError(f"wrong owner for task {row['task_id']}")

    def _session_payload(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        *,
        status: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = self._task_row(connection, task_id)
        ports = self._task_ports(connection, task_id)
        payload = {
            "status": status,
            "task": self._serialize_task(row),
            "coordination_epoch": self._coordination_epoch(connection),
            "state_dir": str(self.state_dir),
            "npu_devices": _load_devices(row["granted_devices"]),
            "container_ssh_port": next(
                (item["port"] for item in ports if item.get("kind") == "container_ssh"),
                None,
            ),
            "service_ports": [item["port"] for item in ports if item.get("kind") == "service"],
            "ports": ports,
        }
        if extra:
            payload.update(extra)
        return payload

    def acquire(
        self,
        task_id: str,
        observed: dict[str, Any],
        *,
        grant_ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
    ) -> dict[str, Any]:
        task_id = require_safe_id(task_id, label="task id")
        if grant_ttl_seconds < 1:
            raise CoordinationError("grant_ttl_seconds must be >= 1")
        if observed.get("status") != "ok":
            return {"status": "probe_failed", "error": observed.get("error"), "occupancy": observed}
        now = self.clock()
        with self._transaction() as connection:
            changes = self._housekeep(connection, observed, now=now)
            row = self._task_row(connection, task_id)
            self._reject_durable_session(row, operation="acquired as a pool grant")
            if row["state"] != "queued":
                return {"status": row["state"], "task": self._serialize_task(row), "gc": changes}
            if float(row["not_before"]) > now:
                return {"status": "waiting", "reason": "not_before", "task": self._serialize_task(row), "gc": changes}
            head = connection.execute(
                """
                SELECT * FROM tasks
                WHERE state='queued' AND not_before<=? AND latest_start>?
                ORDER BY priority DESC, submitted_at ASC, task_id ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if head is None or head["task_id"] != task_id:
                return {
                    "status": "waiting",
                    "reason": "strict_fifo",
                    "ahead_task_id": None if head is None else head["task_id"],
                    "task": self._serialize_task(row),
                    "gc": changes,
                }
            estimated_end = now + int(row["estimated_duration_seconds"])
            available = self._available_devices(
                connection,
                observed,
                exclude_task=task_id,
                start_at=now,
                end_at=estimated_end,
            )
            requested = _load_devices(row["requested_devices"])
            selected, missing = self._select_granted_devices(
                requested=requested,
                count=int(row["requested_count"]),
                available=available,
            )
            if selected is None:
                if requested:
                    return {
                        "status": "waiting",
                        "reason": "requested_devices_unavailable",
                        "unavailable_devices": missing,
                        "available_devices": sorted(available),
                        "task": self._serialize_task(row),
                        "gc": changes,
                    }
                return {
                    "status": "waiting",
                    "reason": "not_enough_devices",
                    "needed": int(row["requested_count"]),
                    "available_devices": sorted(available),
                    "task": self._serialize_task(row),
                    "gc": changes,
                }
            fence = self._next_fence(connection)
            deadline = now + grant_ttl_seconds
            connection.execute(
                """
                UPDATE tasks
                SET state='granted', granted_devices=?, activation_deadline=?,
                    fence_token=?, updated_at=?, message=?
                WHERE task_id=?
                """,
                (_json_devices(selected), deadline, fence, now, "short-lived grant issued", task_id),
            )
            self._event(
                connection,
                "task-granted",
                task_id=task_id,
                data={"devices": selected, "fence_token": fence, "deadline": utc_now_iso(deadline)},
                now=now,
            )
            row = self._task_row(connection, task_id)
        return {
            "status": "granted",
            "task": self._serialize_task(row),
            "environment": {"ASCEND_RT_VISIBLE_DEVICES": ",".join(str(item) for item in selected)},
            "occupancy": observed,
            "gc": changes,
        }

    def preflight(
        self,
        task_id: str,
        token: int,
        observed: dict[str, Any],
        *,
        start_ttl_seconds: int = DEFAULT_START_TTL_SECONDS,
    ) -> dict[str, Any]:
        task_id = require_safe_id(task_id, label="task id")
        if start_ttl_seconds < 1:
            raise CoordinationError("start_ttl_seconds must be >= 1")
        if observed.get("status") != "ok":
            return {"status": "probe_failed", "error": observed.get("error"), "occupancy": observed}
        now = self.clock()
        busy = self._busy_set(observed) or set()
        visible = {int(device) for device in observed.get("devices", [])}
        with self._transaction() as connection:
            changes = self._housekeep(connection, observed, now=now)
            row = self._task_row(connection, task_id)
            self._reject_durable_session(row, operation="preflighted")
            self._check_token(row, token)
            if row["state"] != "granted":
                raise CoordinationError(f"task {task_id} is {row['state']}, expected granted")
            devices = _load_devices(row["granted_devices"])
            conflicts = sorted((set(devices) & busy) | (set(devices) - visible))
            if conflicts:
                next_state = "queued" if float(row["latest_start"]) > now else "expired"
                connection.execute(
                    """
                    UPDATE tasks
                    SET state=?, granted_devices=NULL, activation_deadline=NULL,
                        fence_token=NULL, updated_at=?, message=?
                    WHERE task_id=?
                    """,
                    (next_state, now, f"preflight found externally busy devices: {conflicts}", task_id),
                )
                self._event(
                    connection,
                    "preflight-conflict",
                    task_id=task_id,
                    data={"devices": conflicts, "to": next_state},
                    now=now,
                )
                row = self._task_row(connection, task_id)
                return {
                    "status": "waiting" if next_state == "queued" else "expired",
                    "reason": "external_occupancy",
                    "conflicting_devices": conflicts,
                    "task": self._serialize_task(row),
                    "gc": changes,
                }
            deadline = now + start_ttl_seconds
            connection.execute(
                "UPDATE tasks SET state='starting', activation_deadline=?, updated_at=?, message=? WHERE task_id=?",
                (deadline, now, "preflight passed; launch before activation deadline", task_id),
            )
            self._event(connection, "preflight-passed", task_id=task_id, data={"devices": devices}, now=now)
            row = self._task_row(connection, task_id)
        return {
            "status": "starting",
            "task": self._serialize_task(row),
            "environment": {"ASCEND_RT_VISIBLE_DEVICES": ",".join(str(item) for item in devices)},
            "occupancy": observed,
            "gc": changes,
        }

    def activate(
        self,
        task_id: str,
        token: int,
        *,
        pid: int,
        process_guard: dict | None = None,
        heartbeat_ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
    ) -> dict[str, Any]:
        task_id = require_safe_id(task_id, label="task id")
        if pid < 1:
            raise CoordinationError("pid must be >= 1")
        if heartbeat_ttl_seconds < 1:
            raise CoordinationError("heartbeat_ttl_seconds must be >= 1")
        if process_guard is not None:
            if (not isinstance(process_guard, dict)
                    or not {"marker", "boot_id"}.issubset(process_guard)
                    or set(process_guard) - {"marker", "boot_id", "retain_until_release"}
                    or not re.fullmatch(r"[0-9a-f]{32}", str(process_guard.get("marker", "")))
                    or not isinstance(process_guard.get("boot_id"), str)
                    or ("retain_until_release" in process_guard and not isinstance(process_guard["retain_until_release"], bool))):
                raise CoordinationError("invalid managed process guard")
            if not process_guard_busy(process_guard, completion_confirmed=True):
                raise CoordinationError("managed supervisor is no longer present")
        now = self.clock()
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            self._reject_durable_session(row, operation="activated")
            self._check_token(row, token)
            if row["state"] != "starting":
                raise CoordinationError(f"task {task_id} is {row['state']}, expected starting")
            if row["activation_deadline"] is not None and float(row["activation_deadline"]) <= now:
                raise CoordinationError(f"activation deadline elapsed for task {task_id}")
            expected_end = now + int(row["estimated_duration_seconds"])
            heartbeat_deadline = now + heartbeat_ttl_seconds
            connection.execute(
                """
                UPDATE tasks
                SET state='active', pid=?, started_at=?, expected_end=?,
                    heartbeat_at=?, heartbeat_deadline=?, process_guard=?, activation_deadline=NULL,
                    updated_at=?, message=?
                WHERE task_id=?
                """,
                (pid, now, expected_end, now, heartbeat_deadline,
                 json.dumps(process_guard) if process_guard else None, now, "task reported active", task_id),
            )
            self._event(connection, "task-activated", task_id=task_id, data={"pid": pid}, now=now)
            row = self._task_row(connection, task_id)
        return {"status": "active", "task": self._serialize_task(row)}

    def heartbeat(
        self,
        task_id: str,
        token: int,
        *,
        heartbeat_ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
    ) -> dict[str, Any]:
        task_id = require_safe_id(task_id, label="task id")
        if heartbeat_ttl_seconds < 1:
            raise CoordinationError("heartbeat_ttl_seconds must be >= 1")
        now = self.clock()
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            self._reject_durable_session(row, operation="heartbeated")
            self._check_token(row, token)
            if row["state"] not in {"active", "orphaned_busy"}:
                raise CoordinationError(f"task {task_id} is {row['state']}, expected active or orphaned_busy")
            connection.execute(
                """
                UPDATE tasks
                SET state='active', heartbeat_at=?, heartbeat_deadline=?,
                    updated_at=?, message=?
                WHERE task_id=?
                """,
                (now, now + heartbeat_ttl_seconds, now, "heartbeat received", task_id),
            )
            row = self._task_row(connection, task_id)
        return {"status": "active", "task": self._serialize_task(row)}

    def release(self, task_id: str, token: int, observed: dict[str, Any], *,
                completion_confirmed: bool = False) -> dict[str, Any]:
        task_id = require_safe_id(task_id, label="task id")
        now = self.clock()
        busy = self._busy_set(observed)
        visible = (
            {int(device) for device in observed.get("devices", [])}
            if observed.get("status") == "ok"
            else None
        )
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            self._reject_durable_session(row, operation="released via pool release")
            self._check_token(row, token)
            devices = set(_load_devices(row["granted_devices"]))
            conflicts = (
                sorted((devices & busy) | (devices - visible))
                if busy is not None and visible is not None
                else sorted(devices)
            )
            if (busy is None or visible is None or conflicts
                    or process_guard_busy(row["process_guard"], completion_confirmed=completion_confirmed)):
                connection.execute(
                    "UPDATE tasks SET state='orphaned_busy', updated_at=?, message=? WHERE task_id=?",
                    (now, "release requested but processes or hardware remained busy or unknown", task_id),
                )
                self._event(connection, "release-deferred", task_id=task_id, data={"devices": conflicts}, now=now)
                row = self._task_row(connection, task_id)
                return {
                    "status": "orphaned_busy",
                    "conflicting_devices": conflicts,
                    "task": self._serialize_task(row),
                    "occupancy": observed,
                }
            connection.execute(
                "UPDATE tasks SET state='released', process_guard=NULL, updated_at=?, message=? WHERE task_id=?",
                (now, "hardware observed free; cooperative lease released", task_id),
            )
            self._event(connection, "task-released", task_id=task_id, now=now)
            row = self._task_row(connection, task_id)
        return {"status": "released", "task": self._serialize_task(row), "occupancy": observed}

    def cancel(self, task_id: str, observed: dict[str, Any] | None = None) -> dict[str, Any]:
        task_id = require_safe_id(task_id, label="task id")
        now = self.clock()
        busy = self._busy_set(observed)
        visible = (
            {int(device) for device in observed.get("devices", [])}
            if observed is not None and observed.get("status") == "ok"
            else None
        )
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            self._reject_durable_session(row, operation="cancelled")
            if row["state"] in {"released", "expired", "cancelled"}:
                return {"status": row["state"], "task": self._serialize_task(row)}
            devices = set(_load_devices(row["granted_devices"]))
            still_busy = bool(devices) and (
                busy is None
                or visible is None
                or not devices.issubset(visible)
                or bool(devices & busy)
                or process_guard_busy(row["process_guard"])
            )
            next_state = "orphaned_busy" if still_busy else "cancelled"
            connection.execute(
                "UPDATE tasks SET state=?, process_guard=CASE WHEN ?='cancelled' THEN NULL ELSE process_guard END, updated_at=?, message=? WHERE task_id=?",
                (next_state, next_state, now, "cancel requested", task_id),
            )
            self._event(connection, "task-cancelled", task_id=task_id, data={"to": next_state}, now=now)
            row = self._task_row(connection, task_id)
        return {"status": next_state, "task": self._serialize_task(row)}

    def reserve_session(
        self,
        request: dict[str, Any],
        observed: dict[str, Any],
        listening: dict[str, Any],
    ) -> dict[str, Any]:
        workspace_id = require_safe_id(request.get("workspace_id"), label="workspace id")
        session_id = require_safe_id(request.get("session_id"), label="session id")
        agent_id = require_safe_id(request.get("agent_id") or workspace_id, label="agent id")
        container_name = request.get("container_name")
        devices = parse_devices(request.get("devices"))
        count_raw = request.get("npu_count")
        if devices is not None and count_raw is not None:
            raise CoordinationError("use only one of devices or npu_count")
        npu_requested = devices is not None or count_raw is not None
        if npu_requested:
            if devices is None:
                count = int(count_raw or 0)
                if count < 1:
                    raise CoordinationError("npu_count must be >= 1 when devices are not specified")
            else:
                count = len(devices)
            if observed.get("status") != "ok":
                return {"status": "probe_failed", "error": observed.get("error"), "occupancy": observed}
        else:
            count = 0
        task_id = require_safe_id(
            request.get("task_id") or f"session.{workspace_id}.{session_id}",
            label="task id",
        )
        owner = f"{workspace_id}:{session_id}"
        preferred = request.get("container_ssh_port")
        if preferred is not None:
            preferred = int(preferred)
        port_range = str(request.get("container_ssh_port_range") or DEFAULT_CONTAINER_SSH_PORT_RANGE)
        now = self.clock()
        with self._transaction() as connection:
            existing = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if existing is not None and existing["state"] == "session_reserved":
                if existing["agent_id"] != agent_id or existing["session_id"] != session_id:
                    raise CoordinationError(
                        f"task id {task_id} already exists with different ownership"
                    )
                expected_devices = _json_devices(devices) if devices is not None else existing["requested_devices"]
                if existing["requested_count"] != count or existing["requested_devices"] != expected_devices:
                    raise CoordinationError(
                        f"task id {task_id} already exists with different ownership or resources"
                    )
                return self._session_payload(connection, task_id, status="reserved", extra={"reused": True})
            if existing is not None and existing["state"] not in {"released", "expired", "cancelled"}:
                raise CoordinationError(
                    f"task id {task_id} already exists in state {existing['state']}"
                )
            selected: list[int] = []
            if npu_requested:
                available = self._available_devices(
                    connection,
                    observed,
                    exclude_task=task_id,
                    start_at=now,
                    end_at=now + SESSION_HOLD_HORIZON_SECONDS,
                )
                selected, missing = self._select_granted_devices(
                    requested=devices or [],
                    count=count,
                    available=available,
                )
                if selected is None:
                    if devices:
                        return {
                            "status": "waiting",
                            "reason": "requested_devices_unavailable",
                            "unavailable_devices": missing,
                            "available_devices": sorted(available),
                            "occupancy": observed,
                        }
                    return {
                        "status": "waiting",
                        "reason": "not_enough_devices",
                        "needed": count,
                        "available_devices": sorted(available),
                        "occupancy": observed,
                    }
            fence = self._next_fence(connection)
            latest_start = now + SESSION_HOLD_HORIZON_SECONDS
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id, agent_id, agent_alias, session_id, container_name,
                        requested_count, requested_devices, not_before, latest_start,
                        estimated_duration_seconds, preemptible, priority, state,
                        submitted_at, updated_at, granted_devices, fence_token, message
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'session_reserved', ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        agent_id,
                        request.get("agent_alias"),
                        session_id,
                        container_name,
                        count,
                        _json_devices(devices),
                        now,
                        latest_start,
                        SESSION_HOLD_HORIZON_SECONDS,
                        now,
                        now,
                        _json_devices(selected) if selected else _json_devices([]),
                        fence,
                        "durable session reservation",
                    ),
                )
            else:
                connection.execute("DELETE FROM ports WHERE task_id=?", (task_id,))
                connection.execute(
                    """
                    UPDATE tasks
                    SET agent_id=?, agent_alias=?, session_id=?, container_name=?,
                        requested_count=?, requested_devices=?, not_before=?, latest_start=?,
                        estimated_duration_seconds=?, state='session_reserved',
                        granted_devices=?, fence_token=?, activation_deadline=NULL,
                        heartbeat_at=NULL, heartbeat_deadline=NULL, process_guard=NULL,
                        pid=NULL, started_at=NULL, expected_end=NULL, updated_at=?, message=?
                    WHERE task_id=?
                    """,
                    (
                        agent_id,
                        request.get("agent_alias"),
                        session_id,
                        container_name,
                        count,
                        _json_devices(devices),
                        now,
                        latest_start,
                        SESSION_HOLD_HORIZON_SECONDS,
                        _json_devices(selected) if selected else _json_devices([]),
                        fence,
                        now,
                        "durable session reservation",
                        task_id,
                    ),
                )
            port = self._pick_port(
                connection,
                preferred=preferred,
                port_range=port_range,
                listening=listening,
                kind="container_ssh",
                task_id=task_id,
                owner=owner,
                now=now,
            )
            self._event(
                connection,
                "session-reserved",
                task_id=task_id,
                data={"devices": selected, "container_ssh_port": port, "fence_token": fence},
                now=now,
            )
            return self._session_payload(
                connection,
                task_id,
                status="reserved",
                extra={"reused": False, "occupancy": observed},
            )

    def release_session(
        self,
        request: dict[str, Any],
        observed: dict[str, Any],
        container: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = require_safe_id(request.get("task_id"), label="task id")
        token = int(request["fence_token"])
        now = self.clock()
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            self._check_session_owner(row, request, token)
            if row["state"] in {"released", "expired", "cancelled"}:
                return self._session_payload(connection, task_id, status=row["state"])
            if row["state"] != "session_reserved":
                raise CoordinationError(
                    f"task {task_id} is {row['state']}, expected session_reserved"
                )
            expected_name = request.get("container_name") or row["container_name"]
            if expected_name and container.get("name") not in {None, expected_name}:
                return self._session_payload(
                    connection,
                    task_id,
                    status="retained",
                    extra={
                        "reason": "container probe did not name the reserved container",
                        "container": container,
                        "occupancy": observed,
                    },
                )
            if container.get("status") != "ok":
                return self._session_payload(
                    connection,
                    task_id,
                    status="retained",
                    extra={
                        "reason": container.get("error") or "container state is unknown",
                        "container": container,
                        "occupancy": observed,
                    },
                )
            if container.get("running"):
                return self._session_payload(
                    connection,
                    task_id,
                    status="retained",
                    extra={
                        "reason": "owned container is still running",
                        "container": container,
                        "occupancy": observed,
                    },
                )
            devices = set(_load_devices(row["granted_devices"]))
            if container.get("exists") and devices:
                busy = self._busy_set(observed)
                visible = (
                    {int(device) for device in observed.get("devices", [])}
                    if observed.get("status") == "ok"
                    else None
                )
                conflicts = (
                    sorted((devices & busy) | (devices - visible))
                    if busy is not None and visible is not None
                    else sorted(devices)
                )
                if busy is None or visible is None or conflicts:
                    return self._session_payload(
                        connection,
                        task_id,
                        status="retained",
                        extra={
                            "reason": "allocated hardware is busy or unknown while the container still exists",
                            "conflicting_devices": conflicts,
                            "container": container,
                            "occupancy": observed,
                        },
                    )
            connection.execute("DELETE FROM ports WHERE task_id=?", (task_id,))
            connection.execute(
                "UPDATE tasks SET state='released', process_guard=NULL, granted_devices=NULL, "
                "updated_at=?, message=? WHERE task_id=?",
                (now, "session reservation released after host confirmation", task_id),
            )
            self._event(connection, "session-released", task_id=task_id, now=now)
            return self._session_payload(
                connection,
                task_id,
                status="released",
                extra={"container": container, "occupancy": observed},
            )

    def reserve_service_port(self, request: dict[str, Any], listening: dict[str, Any]) -> dict[str, Any]:
        task_id = require_safe_id(request.get("task_id"), label="task id")
        token = int(request["fence_token"])
        preferred = request.get("port") or request.get("requested_port")
        if preferred is not None:
            preferred = int(preferred)
        port_range = str(request.get("serving_port_range") or DEFAULT_SERVING_PORT_RANGE)
        now = self.clock()
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            self._check_session_owner(row, request, token)
            if row["state"] != "session_reserved":
                raise CoordinationError(
                    f"task {task_id} is {row['state']}, expected session_reserved"
                )
            owner = f"{row['agent_id']}:{row['session_id']}"
            port = self._pick_port(
                connection,
                preferred=preferred,
                port_range=port_range,
                listening=listening,
                kind="service",
                task_id=task_id,
                owner=owner,
                now=now,
            )
            self._event(
                connection,
                "service-port-reserved",
                task_id=task_id,
                data={"port": port},
                now=now,
            )
            return self._session_payload(
                connection,
                task_id,
                status="reserved",
                extra={"port": port},
            )

    def release_service_port(self, request: dict[str, Any]) -> dict[str, Any]:
        task_id = require_safe_id(request.get("task_id"), label="task id")
        token = int(request["fence_token"])
        port = int(request["port"])
        now = self.clock()
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            self._check_session_owner(row, request, token)
            if row["state"] != "session_reserved":
                raise CoordinationError(
                    f"task {task_id} is {row['state']}, expected session_reserved"
                )
            record = connection.execute(
                "SELECT * FROM ports WHERE port=? AND task_id=? AND kind='service'",
                (port, task_id),
            ).fetchone()
            if record is None:
                raise CoordinationError(f"service port {port} is not reserved by task {task_id}")
            connection.execute("DELETE FROM ports WHERE port=? AND task_id=?", (port, task_id))
            self._event(
                connection,
                "service-port-released",
                task_id=task_id,
                data={"port": port},
                now=now,
            )
            return self._session_payload(connection, task_id, status="released", extra={"port": port})

    def snapshot(
        self,
        observed: dict[str, Any] | None,
        *,
        task_id: str | None = None,
        event_limit: int = 50,
    ) -> dict[str, Any]:
        now = self.clock()
        with self._transaction() as connection:
            changes = self._housekeep(connection, observed, now=now)
            meta = {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}
            if task_id:
                task_id = require_safe_id(task_id, label="task id")
                tasks = [self._task_row(connection, task_id)]
            else:
                tasks = connection.execute(
                    "SELECT * FROM tasks ORDER BY submitted_at ASC, task_id ASC"
                ).fetchall()
            holds = connection.execute("SELECT * FROM holds ORDER BY not_before ASC, hold_id ASC").fetchall()
            if task_id:
                port_rows = connection.execute(
                    "SELECT * FROM ports WHERE task_id=? ORDER BY port ASC", (task_id,)
                ).fetchall()
            else:
                port_rows = connection.execute("SELECT * FROM ports ORDER BY port ASC").fetchall()
            events = connection.execute(
                "SELECT * FROM events ORDER BY event_id DESC LIMIT ?", (max(0, event_limit),)
            ).fetchall()
        return {
            "status": "ok",
            "schema_version": int(meta.get("schema_version", SCHEMA_VERSION)),
            "coordination_epoch": meta.get("coordination_epoch"),
            "state_dir": str(self.state_dir),
            "database": str(self.db_path),
            "ephemeral": True,
            "tasks": [self._serialize_task(row) for row in tasks],
            "holds": [self._serialize_hold(row) for row in holds],
            "ports": [item for item in (self._serialize_port(row) for row in port_rows) if item is not None],
            "events": [
                {
                    "event_id": row["event_id"],
                    "at": utc_now_iso(float(row["at"])),
                    "kind": row["kind"],
                    "task_id": row["task_id"],
                    "data": json.loads(row["data"]),
                }
                for row in events
            ],
            "occupancy": observed,
            "gc": changes,
        }


def _confirmed_free_probe(
    *,
    samples: int,
    interval_seconds: float,
    probe: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    if samples < 1:
        raise CoordinationError("free_samples must be >= 1")
    observations: list[dict[str, Any]] = []
    for index in range(samples):
        if index:
            time.sleep(interval_seconds)
        observations.append(probe())
    latest = dict(observations[-1])
    failed = [item for item in observations if item.get("status") != "ok"]
    if failed:
        latest.update(
            {
                "status": "failed",
                "error": "one or more release confirmation probes failed",
                "busy": {},
                "free": [],
            }
        )
    else:
        # A device is releasable only when every confirmation sample observed
        # it free.  Unioning busy reasons makes one transient busy sample keep
        # the cooperative lease protected.
        combined_busy: dict[str, list[dict[str, Any]]] = {}
        common_devices = set(int(device) for device in observations[0].get("devices", []))
        for item in observations:
            common_devices.intersection_update(int(device) for device in item.get("devices", []))
            for device, reasons in item.get("busy", {}).items():
                combined_busy.setdefault(str(device), []).extend(reasons)
        latest["devices"] = sorted(common_devices)
        latest["busy"] = combined_busy
        latest["free"] = sorted(
            int(device)
            for device in latest.get("devices", [])
            if str(device) not in combined_busy
        )
    if samples > 1:
        latest["confirmation_samples"] = observations
        latest["confirmation_sample_count"] = samples
    return latest


def handle_request(
    request: dict[str, Any],
    *,
    probe: Callable[[], dict[str, Any]] = probe_npu_occupancy,
    clock: Callable[[], float] = time.time,
    listening_ports: Callable[[], dict[str, Any]] = probe_listening_ports,
    container_probe: Callable[[str], dict[str, Any]] = probe_named_container,
) -> dict[str, Any]:
    """Execute one structured coordinator request on the host."""
    action = request.get("action")
    coordinator = NpuCoordinator(
        resolve_host_state_dir(request.get("state_dir")),
        clock=clock,
        expected_epoch=request.get("coordination_epoch"),
    )
    if action == "submit":
        return coordinator.submit(request)
    if action == "acquire":
        return coordinator.acquire(
            request["task_id"],
            probe(),
            grant_ttl_seconds=int(request.get("grant_ttl_seconds") or DEFAULT_GRANT_TTL_SECONDS),
        )
    if action == "preflight":
        return coordinator.preflight(
            request["task_id"],
            int(request["fence_token"]),
            probe(),
            start_ttl_seconds=int(request.get("start_ttl_seconds") or DEFAULT_START_TTL_SECONDS),
        )
    if action == "activate":
        return coordinator.activate(
            request["task_id"],
            int(request["fence_token"]),
            pid=int(request["pid"]),
            process_guard=request.get("process_guard"),
            heartbeat_ttl_seconds=int(
                request.get("heartbeat_ttl_seconds") or DEFAULT_HEARTBEAT_TTL_SECONDS
            ),
        )
    if action == "heartbeat":
        return coordinator.heartbeat(
            request["task_id"],
            int(request["fence_token"]),
            heartbeat_ttl_seconds=int(
                request.get("heartbeat_ttl_seconds") or DEFAULT_HEARTBEAT_TTL_SECONDS
            ),
        )
    if action == "release":
        observed = _confirmed_free_probe(
            samples=int(request.get("free_samples") or 2),
            interval_seconds=float(request.get("interval_seconds") or 2.0),
            probe=probe,
        )
        return coordinator.release(request["task_id"], int(request["fence_token"]), observed,
                                   completion_confirmed=request.get("completion_confirmed") is True)
    if action == "cancel":
        return coordinator.cancel(request["task_id"], probe())
    if action == "hold-add":
        return coordinator.add_hold(request, probe())
    if action == "hold-remove":
        return coordinator.remove_hold(request["hold_id"])
    if action in {"status", "gc"}:
        observed = None if request.get("no_probe") else probe()
        return coordinator.snapshot(
            observed,
            task_id=request.get("task_id"),
            event_limit=int(request.get("event_limit") or 50),
        )
    if action == "session-reserve":
        npu_requested = request.get("devices") is not None or request.get("npu_count") is not None
        observed = (
            probe()
            if npu_requested
            else {"status": "ok", "devices": [], "busy": {}, "free": []}
        )
        return coordinator.reserve_session(request, observed, listening_ports())
    if action == "session-release":
        name = str(request.get("container_name") or "")
        return coordinator.release_session(request, probe(), container_probe(name))
    if action == "service-port-reserve":
        return coordinator.reserve_service_port(request, listening_ports())
    if action == "service-port-release":
        return coordinator.release_service_port(request)
    raise CoordinationError(f"unsupported action: {action!r}")
