"""One persistent coordinator process per user/state directory.

CLI, MCP and TaskClient call this process. It owns the runtime pool AND
admitted task-execution progression (placement, preparation, sync, group
leases, launch). Tick runs on its own thread so an idle client cannot freeze it.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any
from remote_dev.runtime import process_identity, runtime_status

from vaws_coordinator.agent_session import AgentSessions
from vaws_coordinator.backend import RemoteBackend
from vaws_coordinator.build_inputs import BUILD_INPUT_ENV_KEYS
from vaws_coordinator.managed_execution import ExecutionRequestError, JOB_TERMINAL
from vaws_coordinator.parity import materialize_command
from vaws_coordinator.placement import (
    SUPPORTED_RECIPES,
    can_prepare,
    distinct_hosts_required,
    host_key,
    role_plan,
    runtime_matches,
    select_runtimes,
)
from vaws_coordinator.provision.task_environment import TaskRootBusy, checkout_identity
from vaws_coordinator.ready_runtime import RuntimePool, user_container_name
from vaws_coordinator.state_paths import coordinator_state_dir

SOCKET_NAME = "coordinator.sock"
LOCK_NAME = "coordinator.lock"
IPC_NAME = "coordinator.ipc"
TICK_SECONDS = 2.0
DONE = {"succeeded", "failed", "timeout", "cancelled", "inconclusive"}
LIVE = {"running"}
LEASE_READY = {"granted", "starting", "active"}
PERMANENT_ERRORS = (ValueError, PermissionError, ExecutionRequestError, TaskRootBusy)
CLIENT_TIMEOUT_SECONDS = 60.0
STOP_WAIT_SECONDS = 30.0
LOADED_RUNTIMES = [process_identity(name) for name in ("vaws-coordinator", "vaws-remote-dev")]


def socket_path(state_dir: Path) -> Path:
    """Short per-user/state socket. macOS AF_UNIX paths cap near 104 bytes."""
    resolved = str(Path(state_dir).expanduser().resolve())
    digest = hashlib.sha256(resolved.encode()).hexdigest()[:16]
    user = "".join(ch if ch.isalnum() else "-" for ch in getpass.getuser())[:12] or "user"
    return Path("/tmp") / f"vc-{user}-{digest}.sock"


def lock_path(state_dir: Path) -> Path:
    return Path(state_dir) / LOCK_NAME


def _service_spec_key(spec: dict) -> dict:
    return {key: spec.get(key) for key in ("command", "env", "environment", "resources", "topology",
                                           "timeout_seconds", "service", "preflight")}


def aggregate_job_states(states: list[str | None]) -> str:
    """One policy for run/observe/stop. Mixed terminal is failed, not states[0]."""
    present = [state for state in states if state]
    if not present:
        return "waiting"
    if any(state == "uncertain" for state in present):
        return "uncertain"
    if any(state == "stopping" for state in present):
        return "stopping"
    if all(state == "running" for state in present):
        return "running"
    terminal = [state for state in present if state in JOB_TERMINAL]
    live = [state for state in present if state not in JOB_TERMINAL]
    if terminal and not live:
        if all(state == "succeeded" for state in terminal):
            return "succeeded"
        if all(state == "cancelled" for state in terminal):
            return "cancelled"
        if all(state == "timeout" for state in terminal):
            return "timeout"
        if all(state == "inconclusive" for state in terminal):
            return "inconclusive"
        return "failed"
    if any(state == "queued" for state in live):
        return "queued"
    if any(state in {"waiting", "pending", "granted", "starting", "prepared"} for state in live):
        return "waiting"
    if any(state == "running" for state in live):
        return "running"
    return live[0]


class CoordinatorService:
    def __init__(self, state_dir: Path, *, pool: RuntimePool | None = None, backend=None,
                 sessions: AgentSessions | None = None):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.backend = backend or (pool.backend if pool is not None else RemoteBackend())
        self.pool = pool or RuntimePool(self.state_dir, self.backend)
        self._sessions = sessions
        self._session_dirs: set[str] = set()
        self._lock_registry: dict[tuple[str, str], threading.Lock] = {}
        self._registry_guard = threading.Lock()
        self._stopped = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._active_requests = 0
        self._async_progress = False
        self._load_session_dirs()
        if sessions is not None:
            self._remember_session_dir(str(sessions.state_dir))
        default_sessions = self.state_dir.parent / "agent-sessions"
        if default_sessions.is_dir():
            self._remember_session_dir(str(default_sessions))

    def _lock_for(self, kind: str, key: str) -> threading.Lock:
        with self._registry_guard:
            return self._lock_registry.setdefault((kind, key), threading.Lock())

    def _load_session_dirs(self) -> None:
        try:
            with self.pool.transaction() as db:
                row = self.pool.get(db, "meta", "session_dirs")
            self._session_dirs.update(row.get("paths") or [])
        except ValueError:
            return

    def _remember_session_dir(self, directory: str) -> None:
        path = str(Path(directory))
        if path in self._session_dirs:
            return
        self._session_dirs.add(path)
        with self.pool.lock, self.pool.transaction() as db:
            self.pool.put(db, "meta", {"id": "session_dirs", "paths": sorted(self._session_dirs)})

    def store(self, sessions_dir: str | Path | None = None) -> AgentSessions:
        if sessions_dir:
            self._remember_session_dir(str(sessions_dir))
            if self._sessions is not None and Path(self._sessions.state_dir) == Path(sessions_dir):
                return self._sessions
            return AgentSessions(Path(sessions_dir))
        if self._sessions is not None:
            return self._sessions
        raise ValueError("sessions directory is required")

    def reconcile(self) -> None:
        self.pool.tick()
        for directory in list(self._session_dirs):
            try:
                store = self.store(directory)
            except Exception as exc:
                self._record_daemon_error(f"sessions {directory}: {exc}")
                continue
            finishing = {session["id"] for session in store.sessions() if session.get("state") == "finishing"}
            for row in store.all_executions():
                if row.get("session_id") in finishing:
                    continue
                if not row.get("admitted") or row.get("phase") in DONE:
                    continue
                try:
                    self.advance(directory, row.get("user") or "", row["id"], action="progress")
                except Exception as exc:
                    self._record_execution_error(store, row, exc)
            self._resume_finishing_sessions(directory, store)

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("op") == "ping":
            return {"ok": True, "value": {"runtime": [runtime_status(item) for item in LOADED_RUNTIMES]}}
        if request.get("op") == "restart_if_idle":
            return {"ok": True, "value": self.restart_if_idle()}
        with self._lifecycle_lock:
            if self._stopped.is_set():
                raise RuntimeError("coordinator is restarting; retry with the new daemon")
            self._active_requests += 1
        try:
            return self._handle(request)
        finally:
            with self._lifecycle_lock:
                self._active_requests -= 1

    def restart_if_idle(self):
        with self._lifecycle_lock:
            if self._active_requests or any(lock.locked() for lock in self._lock_registry.values()):
                return {"status": "busy", "reason": "coordinator work is in progress"}
            for directory in self._session_dirs:
                if any(row.get("admitted") and row.get("phase") not in DONE
                       for row in self.store(directory).all_executions()):
                    return {"status": "busy", "reason": "nonterminal executions remain"}
            from vaws_coordinator.ready_runtime import TERMINAL
            with self.pool.transaction() as db:
                if any(row.get("state") not in TERMINAL for row in self.pool.rows(db, "run")):
                    return {"status": "busy", "reason": "unreleased resource leases remain"}
            self._stopped.set()
            return {"status": "stopping"}

    def _handle(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if op == "tick":
            self.reconcile()
            return {"ok": True}
        if op == "admit":
            value = self.admit(request["sessions_dir"], request["user"], request["session_id"],
                               request["spec"], restart=bool(request.get("restart")),
                               wait=not self._async_progress)
            return {"ok": True, "value": value}
        if op == "advance":
            value = self.advance(request["sessions_dir"], request["user"], request["execution_id"],
                                 action=request.get("action", "status"), force=bool(request.get("force")),
                                 role=request.get("role"))
            return {"ok": True, "value": value}
        if op == "finish":
            value = self.finish(request["sessions_dir"], request["user"], request["session_id"],
                                force=bool(request.get("force")))
            return {"ok": True, "value": value}
        if op == "pool":
            method = getattr(self.pool, request["method"])
            value = method(*request.get("args", []), **request.get("kwargs", {}))
            return {"ok": True, "value": value}
        if op == "provision":
            from vaws_coordinator.provision import provision_user_container
            return {"ok": True, "value": provision_user_container(**request.get("kwargs", {}))}
        raise ValueError(f"unsupported coordinator operation: {op!r}")

    def admit(self, sessions_dir, user, session_id, spec, restart=False, wait=None) -> dict[str, Any]:
        store = self.store(sessions_dir)
        with store.transaction() as db:
            session = store.get(db, "session", session_id)
        if session["state"] != "open":
            raise ValueError("task admission is closed; resume the task before starting another execution")
        sources = {name: source["path"] for name, source in session.get("sources", {}).items()}
        if not {"vllm", "vllm-ascend"}.issubset(sources):
            raise ValueError("bind the actual vllm and vllm-ascend worktrees before an Ascend execution")
        execution_id = None
        created = False
        with self._lock_for("session", session_id):
            if spec.get("service"):
                live = [row for row in store.executions(session_id)
                        if row.get("spec", {}).get("service") == spec["service"]
                        and row.get("phase") not in DONE]
                if live:
                    existing = live[0]
                    same = _service_spec_key(existing.get("spec") or {}) == _service_spec_key(spec)
                    if same and not restart:
                        execution_id = existing["id"]
                    elif not same and not restart:
                        raise ValueError(
                            "service is already running with a different command/environment/resources; "
                            "pass restart=True to replace it"
                        )
                    else:
                        stopped = self._stop_and_wait(store, sessions_dir, user, existing)
                        if stopped.get("state") not in DONE:
                            return stopped
            if execution_id is None:
                request_id = uuid.uuid4().hex
                row = store.execution({"session": {"id": session_id}}, request_id, spec)
                row.update(phase="queued", admitted=True, user=user)
                store.save_execution(row)
                execution_id = row["id"]
                created = True
        if wait is None:
            wait = not self._async_progress
        if not wait:
            if created:
                self._schedule_progress(sessions_dir, user, execution_id)
            return self._observe(sessions_dir, user, execution_id, "status")
        return self.advance(sessions_dir, user, execution_id, action="progress")

    def _schedule_progress(self, sessions_dir, user, execution_id) -> None:
        lock = self._lock_for("execution", execution_id)
        if lock.locked():
            return
        threading.Thread(
            target=self._tick_one,
            args=(sessions_dir, {"id": execution_id, "user": user}, lock),
            name=f"vaws-progress-{execution_id[:12]}",
            daemon=True,
        ).start()

    def advance(self, sessions_dir, user, execution_id, action="status", force=False, role=None) -> dict[str, Any]:
        if action in {"status", "target", "tail"}:
            return self._observe(sessions_dir, user, execution_id, action, role=role)
        if action == "stop":
            return self._request_stop(sessions_dir, user, execution_id, force)
        with self._lock_for("execution", execution_id):
            return self._advance_locked(sessions_dir, user, execution_id, action=action, force=force)

    def _observe(self, sessions_dir, user, execution_id, action, role=None) -> dict[str, Any]:
        store = self.store(sessions_dir)
        with store.transaction() as db:
            row = store.get(db, "execution", execution_id)
        if user and row.get("user") and row["user"] != user:
            raise PermissionError("execution belongs to another principal")
        user = user or row.get("user")
        lock = self._lock_for("execution", execution_id)
        acquired = lock.acquire(blocking=False)
        try:
            if acquired and row.get("roles"):
                row = self._refresh_jobs(store, user, row)
            if action == "tail":
                return self._tail(store, user, row, role=role)
            return self._reply(row, role=role)
        finally:
            if acquired:
                lock.release()

    def _request_stop(self, sessions_dir, user, execution_id, force) -> dict[str, Any]:
        store = self.store(sessions_dir)
        with store.transaction() as db:
            row = store.get(db, "execution", execution_id)
        if user and row.get("user") and row["user"] != user:
            raise PermissionError("execution belongs to another principal")
        user = user or row.get("user")
        row["cancel_requested"] = True
        if force:
            row["force"] = True
        store.save_execution(row)
        lock = self._lock_for("execution", execution_id)
        if lock.acquire(blocking=False):
            try:
                with store.transaction() as db:
                    row = store.get(db, "execution", execution_id)
                return self._stop(store, user, row, force)
            finally:
                lock.release()
        with store.transaction() as db:
            latest = store.get(db, "execution", execution_id)
        latest["cancel_requested"] = True
        if force:
            latest["force"] = True
        return self._reply(latest)

    def _advance_locked(self, sessions_dir, user, execution_id, action="progress", force=False) -> dict[str, Any]:
        store = self.store(sessions_dir)
        with store.transaction() as db:
            row = store.get(db, "execution", execution_id)
        if user and row.get("user") and row["user"] != user:
            raise PermissionError("execution belongs to another principal")
        user = user or row.get("user")
        if row.get("phase") in DONE:
            return self._reply(row)
        with store.transaction() as db:
            session = store.get(db, "session", row["session_id"])
        if session.get("state") == "finishing" or row.get("cancel_requested"):
            force = force or bool(row.get("force")) or bool((session.get("finish") or {}).get("force"))
            return self._stop(store, user, row, force)
        try:
            return self._progress(store, user, row)
        except Exception as exc:
            return self._record_execution_error(store, row, exc)

    def _adopt_cancel(self, store, row) -> bool:
        with store.transaction() as db:
            latest = store.get(db, "execution", row["id"])
            session = store.get(db, "session", row["session_id"])
        if latest.get("cancel_requested"):
            row["cancel_requested"] = True
        if latest.get("force"):
            row["force"] = True
        if session.get("state") == "finishing":
            row["cancel_requested"] = True
            if (session.get("finish") or {}).get("force"):
                row["force"] = True
        return bool(row.get("cancel_requested"))

    def _halt_if_cancelled(self, store, user, row):
        if self._adopt_cancel(store, row):
            return self._stop(store, user, row, bool(row.get("force")))
        return None

    def _progress(self, store, user, row) -> dict[str, Any]:
        halted = self._halt_if_cancelled(store, user, row)
        if halted is not None:
            return halted
        spec = row["spec"]
        session = None
        with store.transaction() as db:
            session = store.get(db, "session", row["session_id"])
        if session["state"] != "open" and not row.get("roles"):
            if session["state"] == "finishing":
                return self._stop(
                    store, user, row,
                    bool(row.get("force")) or bool((session.get("finish") or {}).get("force")),
                )
            raise ValueError("task admission is closed; resume the task before starting another execution")
        sources = {name: source["path"] for name, source in session.get("sources", {}).items()}
        if not {"vllm", "vllm-ascend"}.issubset(sources):
            raise ValueError("bind the actual vllm and vllm-ascend worktrees before an Ascend execution")
        if "remote_session" not in row:
            row["remote_session"] = self.pool.session_open(user, session["id"], sources)
            row["sources"] = sources
            store.save_execution(row)
        row["sources"] = sources

        roles = spec.get("roles") or role_plan(spec.get("topology"), spec.get("resources") or {}, spec["command"])
        environment = spec.get("environment") or {}
        if not row.get("roles"):
            row["phase"] = "preparing"
            store.save_execution(row)
            placed = self._place_or_prepare(store, user, row, roles, environment)
            halted = self._halt_if_cancelled(store, user, row)
            if halted is not None:
                return halted
            if placed.get("status") == "waiting":
                row["phase"] = "waiting"
                row["error"] = placed.get("reason")
                store.save_execution(row)
                return self._reply(row)
            if placed.get("status") == "cache_miss":
                row["phase"] = "waiting_for_runtime"
                row["placement"] = placed
                store.save_execution(row)
                return self._reply(row)
            role_rows = []
            existing_bindings = placed.get("bindings") or []
            catalog = {item["runtime_id"]: item for item in self.pool.catalog()}
            for index, role in enumerate(roles):
                runtime_id = placed["runtime_ids"][index]
                catalog_item = catalog.get(runtime_id)
                if catalog_item is None or not runtime_matches(catalog_item, environment, role):
                    row["phase"] = "waiting_for_runtime"
                    row["placement"] = {
                        "reason": "prepared environment does not match requested constraints",
                        "provisioning_started": bool(placed.get("provisioning_started")),
                    }
                    store.save_execution(row)
                    return self._reply(row)
                if index < len(existing_bindings) and existing_bindings[index]:
                    binding = existing_bindings[index]
                else:
                    request_id = checkout_identity(runtime_id, role["name"])
                    binding = self.pool.checkout(user, row["remote_session"]["id"], catalog_item["profile_key"],
                                                 request_id, runtime_id)
                if binding.get("status") == "cache_miss":
                    row["phase"] = "waiting_for_runtime"
                    row["placement"] = binding
                    store.save_execution(row)
                    return self._reply(row)
                role_rows.append({"name": role["name"], "command": role["command"], "runtime_id": runtime_id,
                                  "preflight": role.get("preflight") or spec.get("preflight"),
                                  "binding": binding, "npu_count": role.get("npu_count"),
                                  "devices": role.get("devices") or [],
                                  "service_port": role.get("service_port"),
                                  "env": dict(role.get("env") or {})})
            row["roles"] = role_rows
            row["assignment"] = {"runtime_ids": placed["runtime_ids"],
                                 "roles": [{"name": item["name"], "runtime_id": item["runtime_id"],
                                            "host": item["binding"]["endpoint"]["host"],
                                            "root": item["binding"]["endpoint"]["cwd"],
                                            "rank": index}
                                           for index, item in enumerate(role_rows)]}
            row["binding"] = role_rows[0]["binding"]
            row["phase"] = "bound"
            store.save_execution(row)

        for role in row["roles"]:
            if role.get("snapshots"):
                continue
            cwd = role["binding"]["endpoint"]["cwd"]
            with self._lock_for("root", cwd):
                if self.pool.runtime_busy(role["runtime_id"]):
                    row["phase"] = "waiting"
                    row["error"] = "task root is in use by another execution; not overwriting sources"
                    store.save_execution(row)
                    return self._reply(row)
                self._save_progress(store, row, role["name"], {
                    "step": "sync-sources", "log_ref": str(self.state_dir / "runs" / row["id"] / role["runtime_id"] / "parity.log")})
                role["snapshots"] = self.sync_binding(role["binding"], row["sources"], row["id"])
                halted = self._halt_if_cancelled(store, user, row)
                if halted is not None:
                    return halted
        halted = self._halt_if_cancelled(store, user, row)
        if halted is not None:
            return halted
        for role in row["roles"]:
            if role.get("preflight") and not role.get("preflight_passed"):
                self._save_progress(store, row, role["name"], {"step": "preflight"})
                self.backend.preflight(role["binding"], role["preflight"],
                                       {**(spec.get("env") or {}), **(role.get("env") or {})})
                role["preflight_passed"] = True
                store.save_execution(row)
        halted = self._halt_if_cancelled(store, user, row)
        if halted is not None:
            return halted
        if any(not role.get("managed_job") for role in row["roles"]):
            row["phase"] = "launch_pending"
            self._save_progress(store, row, None, {"step": "allocate-and-launch"})
        halted = self._halt_if_cancelled(store, user, row)
        if halted is not None:
            return halted

        hold_go = len(row["roles"]) > 1
        for role in row["roles"]:
            if role.get("managed_job"):
                continue
            halted = self._halt_if_cancelled(store, user, row)
            if halted is not None:
                return halted
            merged_env = {**(spec.get("env") or {}), **(role.get("env") or {})}
            job = self.pool.managed_start(
                user, role["binding"]["id"], row["id"] + "-" + role["name"] if hold_go else row["id"],
                role["snapshots"], role["binding"]["build_key"],
                role.get("devices") or [], role.get("npu_count") or 0,
                role["command"], merged_env, spec.get("timeout_seconds"),
                service_port=role.get("service_port"), hold_go=hold_go,
            )
            role["managed_job"] = job["id"]
            role["observation"] = job
        store.save_execution(row)

        jobs = []
        for role in row["roles"]:
            job = self.pool.managed_control(user, role["managed_job"], "status")
            role["observation"] = job
            jobs.append(job)

        halted = self._halt_if_cancelled(store, user, row)
        if halted is not None:
            return halted
        if hold_go:
            leases = [job.get("lease_state") or job["state"] for job in jobs]
            failed = [job for job in jobs if job["state"] in {"failed", "timeout", "inconclusive"}]
            if failed:
                self._stop_jobs(user, row, False)
                row["phase"] = "failed"
                store.save_execution(row)
                return self._reply(row)
            if all(state in LEASE_READY or job["state"] == "running" for state, job in zip(leases, jobs)):
                if all(job["state"] == "running" for job in jobs):
                    pass
                elif all(job.get("lease_state") == "granted" or job["state"] == "waiting" for job in jobs):
                    halted = self._halt_if_cancelled(store, user, row)
                    if halted is not None:
                        return halted
                    jobs = []
                    for role in row["roles"]:
                        job = self.pool.managed_release_gate(user, role["managed_job"])
                        role["observation"] = job
                        jobs.append(job)
            elif not all(job["state"] in JOB_TERMINAL for job in jobs):
                row["phase"] = "queued" if any(job["state"] == "queued" for job in jobs) else "waiting"
                row["managed_job"] = jobs[0]["id"]
                row["observation"] = jobs[0]
                store.save_execution(row)
                return self._reply(row)

        row["managed_job"] = jobs[0]["id"]
        row["observation"] = jobs[0]
        row["phase"] = aggregate_job_states([job["state"] for job in jobs])
        if row["phase"] == "running":
            self._record_assignment(row, jobs)
            self._save_progress(store, row, None, {"step": "running"})
        store.save_execution(row)
        return self._reply(row)

    def _place_or_prepare(self, store, user, row, roles, environment) -> dict[str, Any]:
        catalog = self.pool.catalog()
        existing = self.pool.session_bindings(user, row["remote_session"]["id"])
        reused_ids = []
        reused_bindings = []
        for role in roles:
            found = None
            found_binding = None
            for binding in existing:
                item = next((entry for entry in catalog if entry["runtime_id"] == binding["runtime_id"]), None)
                if item is None or binding["runtime_id"] in reused_ids:
                    continue
                if not runtime_matches(item, environment, role):
                    continue
                cwd = binding["endpoint"]["cwd"]
                if (self.pool.runtime_busy(binding["runtime_id"])
                        or self._lock_for("root", cwd).locked()
                        or self._other_execution_using_runtime(store, row, binding["runtime_id"])):
                    return {"status": "waiting",
                            "reason": "task root is in use; waiting before rematerializing changed code"}
                found = binding["runtime_id"]
                found_binding = binding
                break
            if not found:
                reused_ids = []
                reused_bindings = []
                break
            reused_ids.append(found)
            reused_bindings.append(found_binding)
        if reused_ids:
            return {"runtime_ids": reused_ids, "bindings": reused_bindings, "reason": None}

        busy = {item["runtime_id"] for item in catalog if self.pool.runtime_busy(item["runtime_id"])}
        topology = (row.get("spec") or {}).get("topology") or {}
        need_distinct = distinct_hosts_required(roles, topology)
        selected = select_runtimes(catalog, user=user, roles=roles, environment=environment,
                                   busy_runtime_ids=busy, topology=topology)
        if selected["runtime_ids"]:
            catalog_map = {item["runtime_id"]: item for item in catalog}
            for index, role in enumerate(roles):
                item = catalog_map.get(selected["runtime_ids"][index])
                if item is None or not runtime_matches(item, environment, role):
                    return {"status": "cache_miss",
                            "reason": "prepared environment does not match requested constraints",
                            "provisioning_started": False}
            return selected

        if need_distinct:
            known = {host_key(item) for item in catalog if item.get("user") == user and host_key(item)}
            for record in self._configured_machines():
                ip = (record.get("host") or {}).get("ip")
                if ip:
                    known.add(ip)
            if len(known) < len(roles):
                return {"status": "cache_miss", "reason": selected["reason"]
                        or "not enough distinct hosts with a matching prepared environment for this topology",
                        "provisioning_started": False}
        ids = []
        used_hosts: set[str] = set()
        for role in roles:
            donor = self._donor_for_role(user, environment, role, used_hosts, need_distinct)
            if donor is None:
                donor = self._ensure_user_container(user, environment, role, used_hosts, need_distinct)
            if donor is None:
                return {"status": "cache_miss", "reason": selected["reason"], "provisioning_started": False}
            host = host_key(donor)
            if need_distinct and host:
                used_hosts.add(host)
            try:
                prepared = self._prepare_role(store, user, row, role, environment, donor)
            except TaskRootBusy as exc:
                return {"status": "waiting", "reason": str(exc)}
            catalog = {item["runtime_id"]: item for item in self.pool.catalog()}
            item = catalog.get(prepared["id"])
            if item is None or not runtime_matches(item, environment, role):
                return {"status": "cache_miss",
                        "reason": "prepared environment does not match requested constraints",
                        "provisioning_started": True}
            ids.append(prepared["id"])
        return {"runtime_ids": ids, "reason": None, "provisioning_started": True}

    def _other_execution_using_runtime(self, store, row, runtime_id) -> bool:
        for other in store.executions(row["session_id"]):
            if other["id"] == row["id"] or other.get("phase") in DONE:
                continue
            for role in other.get("roles") or []:
                if role.get("runtime_id") == runtime_id:
                    return True
        return False

    def _donor_for_role(self, user, environment, role, used_hosts, require_distinct=False):
        catalog = self.pool.catalog()
        for item in catalog:
            if item.get("user") != user or not item.get("host"):
                continue
            if require_distinct and item["host"] in used_hosts:
                continue
            if not can_prepare(environment, item, role):
                continue
            return item
        return None

    def _configured_machines(self):
        machines = getattr(self.backend, "machines", None)
        if machines is None:
            return []
        try:
            return list(machines.machines())
        except Exception:
            return []

    def _ensure_user_container(self, user, environment, role, used_hosts, require_distinct=False):
        recipe = environment.get("recipe") or environment.get("image")
        if not recipe or recipe not in SUPPORTED_RECIPES:
            return None
        wanted_host = str(role["host"]) if role.get("host") else None
        for record in self._configured_machines():
            host_info = record.get("host") or {}
            host_ip = host_info.get("ip")
            if not host_ip or (require_distinct and host_ip in used_hosts):
                continue
            if wanted_host and host_ip != wanted_host:
                continue
            machine_type = host_info.get("machine_type")
            if environment.get("machine_type") and machine_type != environment["machine_type"]:
                continue
            ssh_port = (record.get("container") or {}).get("ssh_port")
            host_endpoint = {
                "host": host_ip,
                "port": int(host_info.get("port") or 22),
                "user": host_info.get("user") or "root",
            }
            if not ssh_port:
                from vaws_coordinator.provision import provision_user_container

                def reserve_port(*, user, container_name, port):
                    stub = {
                        "host_endpoint": host_endpoint,
                        "container_name": container_name,
                        "endpoint": {"host": host_ip, "port": port, "user": "root"},
                    }
                    return self.backend.host(stub, {
                        "action": "container-ssh-reserve", "user": user,
                        "container_name": container_name, "port": port,
                    })

                result = provision_user_container(
                    host=host_ip, image=recipe, user=user,
                    host_user=host_endpoint["user"], host_port=host_endpoint["port"],
                    machine_type=machine_type or environment.get("machine_type"),
                    machines=getattr(self.backend, "machines", None),
                    reserve_port=reserve_port,
                )
                ssh_port = result["ssh_port"]
            else:
                stub = {
                    "host_endpoint": host_endpoint,
                    "container_name": user_container_name(user),
                    "endpoint": {"host": host_ip, "port": int(ssh_port), "user": "root"},
                }
                self.backend.host(stub, {
                    "action": "container-ssh-reserve", "user": user,
                    "container_name": user_container_name(user), "port": int(ssh_port),
                })
            return {
                "user": user,
                "host": host_ip,
                "host_endpoint": host_endpoint,
                "ssh_port": int(ssh_port),
                "endpoint": {"host": host_ip, "port": int(ssh_port), "user": "root"},
                "container_name": user_container_name(user),
                "recipe": recipe,
                "machine_type": machine_type,
                "service_ports": [],
            }
        return None

    def _save_progress(self, store, row, role, event):
        now = time.time()
        previous = row.get("progress") or {}
        same_step = (previous.get("step"), previous.get("role")) == (event.get("step"), role)
        row["progress"] = {**(previous if same_step else {}), **event, "role": role,
                           "started_at": previous["started_at"] if same_step else now,
                           "updated_at": now}
        store.save_execution(row)

    def _prepare_role(self, store, user, row, role, environment, donor):
        from vaws_coordinator.provision import prepare_task_environment
        return prepare_task_environment(
            self.pool, user=user, session_id=row["session_id"], role_name=role["name"],
            environment=environment, donor=donor, sources=row.get("sources") or {},
            on_progress=lambda event: self._save_progress(store, row, role["name"], event),
            log_dir=self.state_dir / "runs" / row["id"] / role["name"],
        )

    def sync_binding(self, binding, sources, execution_id):
        directory = self.state_dir / "runs" / execution_id / binding["runtime_id"]
        directory.mkdir(parents=True, exist_ok=True)
        endpoint = binding["endpoint"]
        args = materialize_command(workspace_id=binding["intent"]["session"],
                                   runtime_id=binding["runtime_id"], endpoint=endpoint,
                                   sources={name: sources[name] for name in ("vllm", "vllm-ascend")})
        environment = {key: value for key, value in os.environ.items() if key not in BUILD_INPUT_ENV_KEYS}
        environment.update(binding.get("build_env", {}))
        environment.update(binding["environment"])
        with (directory / "parity.json").open("w") as stdout, (directory / "parity.log").open("w") as stderr:
            result = subprocess.run(args, stdout=stdout, stderr=stderr,
                                    env=environment, timeout=600, check=False)
        if result.returncode:
            raise RuntimeError(f"source synchronization failed; inspect {directory / 'parity.log'}")
        payload = json.loads((directory / "parity.json").read_text())
        if payload.get("status") not in {"ready", "materialized"}:
            raise RuntimeError("source staging alone does not authorize execution")
        return payload["snapshot_commits"]

    def _refresh_jobs(self, store, user, row):
        if not row.get("roles"):
            return row
        for role in row["roles"]:
            if not role.get("managed_job"):
                continue
            job = self.pool.managed_control(user, role["managed_job"], "status")
            role["observation"] = job
        if row["roles"]:
            row["observation"] = row["roles"][0].get("observation")
            row["managed_job"] = row["roles"][0].get("managed_job")
            states = [role.get("observation", {}).get("state") for role in row["roles"] if role.get("observation")]
            if states:
                row["phase"] = aggregate_job_states(states)
        store.save_execution(row)
        return row

    def _stop(self, store, user, row, force):
        row["cancel_requested"] = True
        if force:
            row["force"] = True
        self._stop_jobs(user, row, force)
        row = self._refresh_jobs(store, user, row)
        job_states = [role.get("observation", {}).get("state")
                      for role in row.get("roles") or [] if role.get("managed_job")]
        if not job_states:
            row["phase"] = "cancelled"
            store.save_execution(row)
        return self._reply(row)

    def _stop_and_wait(self, store, sessions_dir, user, row, timeout=None):
        if timeout is None:
            timeout = STOP_WAIT_SECONDS
        deadline = time.time() + timeout
        reply = self._request_stop(sessions_dir, user, row["id"], False)
        while reply.get("state") not in DONE and time.time() < deadline:
            time.sleep(0.05)
            reply = self._request_stop(sessions_dir, user, row["id"], False)
        return reply

    def _stop_jobs(self, user, row, force):
        for role in row.get("roles") or []:
            if role.get("managed_job"):
                try:
                    role["observation"] = self.pool.managed_control(user, role["managed_job"], "stop", force)
                except Exception:
                    continue

    def _record_assignment(self, row, jobs):
        assignment = row.get("assignment") or {"roles": []}
        for index, (role, job) in enumerate(zip(row["roles"], jobs)):
            devices = (job.get("environment") or {}).get("ASCEND_RT_VISIBLE_DEVICES")
            entry = (assignment.get("roles") or [{}] * len(jobs))
            if index < len(entry):
                entry[index].update({
                    "name": role["name"],
                    "runtime_id": role["runtime_id"],
                    "host": role["binding"]["endpoint"]["host"],
                    "root": role["binding"]["endpoint"]["cwd"],
                    "rank": index,
                    "devices": devices,
                    "service_port": job.get("service_port"),
                    "state": job["state"],
                })
        row["assignment"] = assignment

    def _record_execution_error(self, store, row, exc) -> dict[str, Any]:
        permanent = isinstance(exc, PERMANENT_ERRORS)
        path = self.state_dir / "runs" / row["id"] / "error.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) + "\n")
        row["error_ref"] = str(path)
        row["error"] = f"{type(exc).__name__}: {exc}"[:500]
        row["phase"] = "failed" if permanent else "uncertain"
        store.save_execution(row)
        return self._reply(row)

    def _record_daemon_error(self, message: str) -> None:
        path = self.state_dir / "daemon.log"
        try:
            with path.open("a") as stream:
                stream.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ") + message[:500] + "\n")
        except OSError:
            return

    def _role_view(self, row, role) -> dict[str, Any]:
        job = role.get("observation") or {}
        binding = role.get("binding") or {}
        endpoint = dict(binding.get("endpoint") or {})
        view = {
            "name": role.get("name"),
            "state": job.get("state") or row.get("phase"),
            "runtime_id": role.get("runtime_id"),
            "service_port": job.get("service_port") if job.get("service_port") is not None else role.get("service_port"),
            "host": endpoint.get("host"),
            "root": endpoint.get("cwd") or endpoint.get("root"),
            "endpoint": endpoint or None,
            "env": dict(role.get("env") or {}),
            "error": job.get("error"),
            "lease_state": job.get("lease_state"),
            "quiet": (job.get("remote") or {}).get("quiet"),
            "descendants_drained": (job.get("remote") or {}).get("descendants_drained"),
        }
        if binding.get("endpoint"):
            target = self._target(row, binding, job)
            role_state = job.get("state") or row.get("phase")
            target = {**target, "state": role_state, "live": role_state in LIVE}
            view["target"] = target
            view["environment"] = target.get("environment") or {}
            if view["service_port"] is None:
                view["service_port"] = target.get("service_port")
        return view

    def _reply(self, row, role=None) -> dict[str, Any]:
        job = row.get("observation") or {}
        state = row.get("phase") or job.get("state")
        payload = {"execution_id": row["id"], "state": state, "service": (row.get("spec") or {}).get("service"),
                   "assignment": row.get("assignment"), "observed_at": time.time(),
                   "progress": row.get("progress")}
        if row.get("cancel_requested"):
            payload["cancel_requested"] = True
        if row.get("error"):
            payload["error"] = str(row["error"])[:500]
            payload["error_ref"] = row.get("error_ref")
        if row.get("placement") and state == "waiting_for_runtime":
            payload.update({k: row["placement"].get(k) for k in ("reason", "provisioning_started") if k in (row["placement"] or {})})
        roles = row.get("roles") or []
        from vaws_coordinator.ready_runtime import TERMINAL
        payload["resources_released"] = state in DONE and all(
            not item.get("managed_job") or (item.get("observation") or {}).get("lease_state") in TERMINAL
            for item in roles)
        role_views = [self._role_view(row, item) for item in roles]
        if role:
            role_views = [item for item in role_views if item.get("name") == role]
        if len(roles) > 1 or role_views:
            payload["roles"] = role_views
        binding = row.get("binding") or (roles[0]["binding"] if roles else None)
        first_job = (roles[0].get("observation") if roles else None) or job
        if binding and state in LIVE | DONE:
            payload["target"] = self._target(row, binding, first_job)
            payload["service_port"] = payload["target"]["service_port"]
        return payload

    def _tail(self, store, user, row, role=None) -> dict[str, Any]:
        payload = self._reply(row, role=role)
        selected = row.get("roles") or []
        if role:
            selected = [item for item in selected if item.get("name") == role]
        collected = []
        for item in selected:
            if not item.get("managed_job"):
                continue
            observed = self.pool.managed_control(user, item["managed_job"], "tail")
            remote = observed.get("remote") or {}
            stdout = remote.get("stdout") or ""
            stderr = remote.get("stderr") or ""
            text = stdout or stderr or remote.get("tail") or ""
            collected.append({"name": item.get("name"), "stdout": stdout, "stderr": stderr, "tail": text})
        if len(collected) == 1:
            payload["stdout"] = collected[0]["stdout"]
            payload["stderr"] = collected[0]["stderr"]
            payload["tail"] = collected[0]["tail"] or collected[0]["stdout"]
        if payload.get("roles"):
            by_name = {item["name"]: item for item in collected}
            for view in payload["roles"]:
                extra = by_name.get(view.get("name"))
                if extra:
                    view.update(extra)
        return payload

    def _target(self, row, binding, job) -> dict[str, Any]:
        state = row.get("phase") or (job or {}).get("state")
        environment = {}
        lease_env = (job or {}).get("environment") or {}
        if lease_env.get("ASCEND_RT_VISIBLE_DEVICES"):
            environment["ASCEND_RT_VISIBLE_DEVICES"] = lease_env["ASCEND_RT_VISIBLE_DEVICES"]
        service_port = (job or {}).get("service_port")
        if service_port is None and lease_env.get("VAWS_SERVICE_PORT"):
            service_port = int(lease_env["VAWS_SERVICE_PORT"])
        if service_port is not None:
            environment["VAWS_SERVICE_PORT"] = str(service_port)
        return {
            "execution_id": row["id"], "session_id": row["session_id"],
            "runtime_id": binding["runtime_id"], "binding_id": binding["id"],
            "user": binding.get("user"), "container_name": binding.get("container_name"),
            "endpoint": dict(binding["endpoint"]), "host_endpoint": binding.get("host_endpoint"),
            "container_id": binding.get("container_id"), "python": binding.get("python"),
            "profile_key": binding.get("profile_key"), "build_key": binding.get("build_key"),
            "launch_env": binding.get("launch_env") or {},
            "launch_preamble": binding.get("launch_preamble") or "",
            "environment": environment, "service_port": service_port,
            "state": state, "live": state in LIVE,
            "assignment": row.get("assignment"),
        }

    def finish(self, sessions_dir, user, session_id, force=False) -> dict[str, Any]:
        store = self.store(sessions_dir)
        with store.transaction() as db:
            session = store.get(db, "session", session_id)
            session["state"] = "finishing"
            session["finish"] = {"user": user, "force": bool(force), "at": time.time()}
            store.put(db, "session", session)
        states = []
        for row in store.executions(session_id):
            if row.get("phase") in DONE:
                continue
            states.append(self.advance(sessions_dir, user, row["id"], action="stop", force=force))
        completed = self._complete_finishing_session(sessions_dir, store, session_id)
        if completed["state"] == "finished":
            return {**completed, "executions": states or completed.get("executions") or []}
        return {"state": "finishing", "executions": states, "worktrees_preserved": True}

    def _resume_finishing_sessions(self, sessions_dir, store) -> None:
        for session in store.sessions():
            if session.get("state") != "finishing":
                continue
            intent = session.get("finish") or {}
            user = intent.get("user") or ""
            force = bool(intent.get("force"))
            for row in store.executions(session["id"]):
                if not row.get("admitted") or row.get("phase") in DONE:
                    continue
                if self._lock_for("execution", row["id"]).locked():
                    continue
                try:
                    self.advance(sessions_dir, user or row.get("user") or "", row["id"],
                                 action="stop", force=force)
                except Exception as exc:
                    self._record_daemon_error(f"finish stop {row.get('id')}: {exc}")
            try:
                self._complete_finishing_session(sessions_dir, store, session["id"])
            except Exception as exc:
                self._record_daemon_error(f"finish {session.get('id')}: {exc}")

    def _complete_finishing_session(self, sessions_dir, store, session_id) -> dict[str, Any]:
        with store.transaction() as db:
            session = store.get(db, "session", session_id)
        if session["state"] == "finished":
            return {"state": "finished", "executions": [self._reply(row) for row in store.executions(session_id)],
                    "worktrees_preserved": True}
        if session["state"] != "finishing":
            return {"state": session["state"], "executions": [], "worktrees_preserved": True}
        rows = store.executions(session_id)
        finishing = {"state": "finishing", "executions": [self._reply(row) for row in rows],
                     "worktrees_preserved": True}
        if any(row.get("admitted") and row.get("phase") not in DONE for row in rows):
            return finishing
        acquired = []
        try:
            for row in sorted(rows, key=lambda item: item["id"]):
                lock = self._lock_for("execution", row["id"])
                if not lock.acquire(blocking=False):
                    return finishing
                acquired.append(lock)
            rows = store.executions(session_id)
            finishing = {"state": "finishing", "executions": [self._reply(row) for row in rows],
                         "worktrees_preserved": True}
            if any(row.get("admitted") and row.get("phase") not in DONE for row in rows):
                return finishing
            user = (session.get("finish") or {}).get("user") or next(
                (row.get("user") for row in rows if row.get("user")), "")
            seen = set()
            for row in rows:
                for role in row.get("roles") or []:
                    binding = role.get("binding")
                    if not binding or binding["id"] in seen:
                        continue
                    seen.add(binding["id"])
                    try:
                        self.pool.return_runtime(user, binding["id"])
                    except ValueError:
                        return finishing
                    except PermissionError:
                        continue
            with store.transaction() as db:
                session = store.get(db, "session", session_id)
                session["state"] = "finished"
                store.put(db, "session", session)
            return {"state": "finished", "executions": [self._reply(row) for row in rows],
                    "worktrees_preserved": True}
        finally:
            for lock in acquired:
                lock.release()

    def serve(self) -> int:
        import fcntl

        lock = lock_path(self.state_dir).open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError(f"coordinator already running for {self.state_dir}")
        path = socket_path(self.state_dir)
        if path.exists():
            path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(path))
        except OSError as exc:
            lock.close()
            raise RuntimeError(f"coordinator socket bind failed at {path} ({len(str(path))} bytes): {exc}") from exc
        os.chmod(path, 0o700)
        (self.state_dir / IPC_NAME).write_text(
            json.dumps({"socket": str(path), "state_dir": str(self.state_dir.resolve())}) + "\n"
        )
        print(f"vaws-coordinator listening socket={path} state={self.state_dir} lock={lock_path(self.state_dir)}",
              flush=True)
        server.listen(16)
        server.settimeout(1.000_000)
        self._async_progress = True
        ticker = threading.Thread(target=self._tick_loop, name="vaws-coordinator-tick", daemon=True)
        ticker.start()
        try:
            while not self._stopped.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()
        finally:
            self._stopped.set()
            server.close()
            if path.exists():
                path.unlink()
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        return 0

    def _tick_loop(self) -> None:
        while not self._stopped.wait(TICK_SECONDS):
            try:
                self._dispatch_progress()
            except Exception as exc:
                self._record_daemon_error(f"tick: {type(exc).__name__}: {exc}")

    def _dispatch_progress(self) -> None:
        with self._lifecycle_lock:
            if self._stopped.is_set():
                return
            self._active_requests += 1
        try:
            self._dispatch_progress_active()
        finally:
            with self._lifecycle_lock:
                self._active_requests -= 1

    def _dispatch_progress_active(self) -> None:
        self.pool.tick()
        for directory in list(self._session_dirs):
            try:
                store = self.store(directory)
            except Exception as exc:
                self._record_daemon_error(f"sessions {directory}: {exc}")
                continue
            finishing = {session["id"] for session in store.sessions() if session.get("state") == "finishing"}
            for row in store.all_executions():
                if row.get("session_id") in finishing:
                    continue
                if not row.get("admitted") or row.get("phase") in DONE:
                    continue
                lock = self._lock_for("execution", row["id"])
                if lock.locked():
                    continue
                thread = threading.Thread(
                    target=self._tick_one, args=(directory, row, lock),
                    name=f"vaws-progress-{row['id'][:12]}", daemon=True,
                )
                thread.start()
            self._resume_finishing_sessions(directory, store)

    def _tick_one(self, directory, row, lock: threading.Lock) -> None:
        if not lock.acquire(blocking=False):
            return
        try:
            self._advance_locked(directory, row.get("user") or "", row["id"])
        except Exception as exc:
            try:
                store = self.store(directory)
                self._record_execution_error(store, row, exc)
            except Exception as record_exc:
                self._record_daemon_error(f"execution {row.get('id')}: {exc}; persist {record_exc}")
        finally:
            lock.release()

    def _serve_conn(self, conn: socket.socket) -> None:
        conn.settimeout(CLIENT_TIMEOUT_SECONDS)
        try:
            data = b""
            while True:
                chunk = conn.recv(1 << 16)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
            if not data.strip():
                return
            try:
                request = json.loads(data.decode())
                reply = self.handle(request)
            except Exception as exc:
                reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            conn.sendall((json.dumps(reply, default=str) + "\n").encode())
        except (OSError, socket.timeout, json.JSONDecodeError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass


class CoordinatorClient:
    def __init__(self, state_dir: Path, *, timeout=120):
        self.state_dir = Path(state_dir)
        self.timeout = timeout

    def call(self, op: str, **payload) -> Any:
        path = socket_path(self.state_dir)
        if not path.exists():
            raise RuntimeError(f"coordinator daemon is not running at {path}")
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(self.timeout)
        try:
            conn.connect(str(path))
            conn.sendall((json.dumps({"op": op, **payload}) + "\n").encode())
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(1 << 16)
                if not chunk:
                    break
                data += chunk
        except OSError:
            try:
                conn.close()
            except OSError:
                pass
            raise
        try:
            reply = json.loads(data.decode() or "{}")
        finally:
            try:
                conn.close()
            except OSError:
                pass
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error") or "coordinator request failed")
        return reply.get("value")

    def admit(self, sessions_dir, user, session_id, spec, restart=False):
        return self.call("admit", sessions_dir=str(sessions_dir), user=user,
                         session_id=session_id, spec=spec, restart=restart)

    def advance(self, sessions_dir, user, execution_id, action="status", force=False, role=None):
        payload = dict(sessions_dir=str(sessions_dir), user=user,
                       execution_id=execution_id, action=action, force=force)
        if role:
            payload["role"] = role
        return self.call("advance", **payload)

    def finish(self, sessions_dir, user, session_id, force=False):
        return self.call("finish", sessions_dir=str(sessions_dir), user=user,
                         session_id=session_id, force=force)


def ensure_daemon(state_dir: Path) -> CoordinatorClient:
    client = CoordinatorClient(state_dir)
    try:
        client.runtime = (client.call("ping") or {}).get("runtime")
        return client
    except (RuntimeError, FileNotFoundError, ConnectionError, OSError):
        pass
    Path(state_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = Path(state_dir) / "daemon.log"
    log = log_path.open("ab")
    subprocess.Popen(
        [sys.executable, "-m", "vaws_coordinator", "daemon", "--state-dir", str(state_dir)],
        stdout=log, stderr=log, start_new_session=True,
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            client.runtime = (client.call("ping") or {}).get("runtime")
            return client
        except (RuntimeError, FileNotFoundError, ConnectionError, OSError):
            time.sleep(0.05)
    raise RuntimeError(f"could not start coordinator daemon at {state_dir}")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the persistent local VAWS coordinator")
    parser.add_argument("--state-dir", default="", help="Coordinator state directory")
    parser.add_argument("--action", choices=("serve", "status", "restart-if-idle"), default="serve")
    args = parser.parse_args(argv)
    state = Path(args.state_dir).expanduser() if args.state_dir else coordinator_state_dir()
    if args.action == "status":
        print(json.dumps(CoordinatorClient(state).call("ping")))
        return 0
    if args.action == "restart-if-idle":
        reply = CoordinatorClient(state).call("restart_if_idle")
        if reply["status"] == "busy":
            print(json.dumps(reply))
            return 1
        deadline = time.monotonic() + 5
        while socket_path(state).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if socket_path(state).exists():
            raise RuntimeError("daemon shutdown is still pending")
        print(json.dumps({"status": "restarted", "runtime": ensure_daemon(state).runtime}))
        return 0
    return CoordinatorService(state).serve()
