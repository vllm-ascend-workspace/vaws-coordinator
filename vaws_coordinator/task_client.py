"""Local task facade. Hosted work is admitted to the persistent coordinator."""

from __future__ import annotations

import getpass
import math
import time
from pathlib import Path

from vaws_coordinator.agent_session import AgentSessions, load_context
from vaws_coordinator.placement import normalize_environment, normalize_resources, role_plan, validate_user_env
from vaws_coordinator.ready_runtime import safe_id
from vaws_coordinator.state_paths import coordinator_state_dir

DONE = {"succeeded", "failed", "timeout", "cancelled", "inconclusive"}


def coordinator_user(explicit: str | None = None) -> str:
    if explicit:
        return safe_id(explicit)
    return safe_id(getpass.getuser())


class TaskClient:
    def __init__(self, context_file="", *, pool=None, user=None, service=None, allow_native_context=True):
        self.context = load_context(context_file, allow_native_context=allow_native_context)
        self.store = AgentSessions(Path(self.context["state_dir"]))
        self.user = coordinator_user(user)
        self._pool = pool
        self._service = service

    @property
    def coordinator(self):
        if self._service is not None:
            return self._service
        if self._pool is not None:
            from vaws_coordinator.service import CoordinatorService
            self._service = CoordinatorService(
                coordinator_state_dir(self.store.state_dir),
                pool=self._pool, backend=self._pool.backend, sessions=self.store,
            )
            return self._service
        from vaws_coordinator.service import ensure_daemon
        self._service = ensure_daemon(coordinator_state_dir(self.store.state_dir))
        return self._service

    @property
    def pool(self):
        owner = self.coordinator
        return owner.pool if hasattr(owner, "pool") else self._pool

    def status(self):
        context = self.store.context(self.context["attachment"]["id"])
        with self.store.transaction() as db:
            attachments = [row for row in self.store.rows(db, "attachment") if row["session_id"] == context["session"]["id"]]
        return {**context, "attachments": attachments, "executions": self.store.executions(context["session"]["id"])}

    def sources(self, sources):
        self.context = self.store.bind_sources(self.context, sources)
        return self.context

    def run(self, command, *, sources=None, env=None, environment=None, resources=None, topology=None,
            timeout_seconds=1800, service=None, restart=False, preflight=None):
        """Admit fixed inputs and resources for one supervised execution.

        ``resources={"devices": [id], "allow_external_busy": True}`` explicitly
        shares one physical NPU with external processes. Other managed leases
        remain exclusive; stopping this execution only stops its own family.
        """
        if not command or not isinstance(command, str) or not command.strip():
            raise ValueError("command is required")
        env = validate_user_env(env)
        environment = normalize_environment(environment)
        resources = normalize_resources(resources)
        roles = role_plan(topology, resources, command)
        if preflight is not None and (not isinstance(preflight, str) or not preflight.strip()):
            raise ValueError("preflight must be a nonempty shell command")
        from vaws_coordinator.execution_sources import capture_sources
        if sources is None:
            # Resolve this attachment's automatic sources or explicit task
            # override once. Accepted work never consults either mapping again.
            context = self.store.context(self.context["attachment"]["id"])
            defaults = context["source_defaults"]
            if defaults["origin"] == "unknown":
                raise ValueError(defaults["reason"])
            sources = {name: source["path"] for name, source in defaults["sources"].items()}
        source_snapshot = capture_sources(sources, self.store.state_dir)
        spec = {
            "command": command, "env": env, "environment": environment or {},
            "resources": resources, "topology": topology or {}, "roles": roles,
            "source_snapshot": source_snapshot,
            "timeout_seconds": timeout_seconds, "service": service,
            "preflight": preflight,
        }
        return self.coordinator.admit(str(self.store.state_dir), self.user,
                                      self.context["session"]["id"], spec, restart=restart)

    def _require_execution_id(self, execution_id):
        if not isinstance(execution_id, str) or len(execution_id) != 64 or any(
                char not in "0123456789abcdef" for char in execution_id):
            raise ValueError("invalid local execution id")
        with self.store.transaction() as db:
            row = self.store.get(db, "execution", execution_id)
        if row.get("session_id") != self.context["session"]["id"]:
            raise ValueError("execution belongs to another VAWS task")

    def target(self, execution_id):
        self._require_execution_id(execution_id)
        reply = self.coordinator.advance(str(self.store.state_dir), self.user, execution_id, action="target")
        if "target" in reply:
            return reply["target"]
        raise ValueError("execution has no runtime binding; no guessed target")

    def resolve_execution(self, execution_id=None, *, service=None):
        """Resolve one owned reference without starting a daemon or allocating resources."""
        if bool(execution_id) == bool(service):
            raise ValueError("provide exactly one of execution_id or service")
        if execution_id:
            self._require_execution_id(execution_id)
            return execution_id
        if not isinstance(service, str) or not service.strip():
            raise ValueError("service must be a nonempty string")
        rows = [row for row in self.store.executions(self.context["session"]["id"])
                if (row.get("spec") or {}).get("service") == service]
        live = [row for row in rows if row.get("phase") not in DONE]
        if len(live) > 1:
            raise ValueError("service has multiple live executions; use an execution_id")
        selected = live or sorted(rows, key=lambda row: (row.get("created_at", 0), row["id"]))[-1:]
        return selected[0]["id"] if selected else None

    def observe(self, execution_id=None, action="status", force=False, role=None, refresh=True, *, service=None):
        if action not in {"status", "tail", "stop", "target"}:
            raise ValueError("unsupported execution action")
        execution_id = self.resolve_execution(execution_id, service=service)
        if execution_id is None:
            return {"state": "not_found", "service": service}
        self._require_execution_id(execution_id)
        if action == "target":
            target = self.target(execution_id)
            reply = {"execution_id": execution_id, "state": target["state"], "target": target,
                     "service_port": target.get("service_port"), "live": target.get("live")}
            if role:
                reply = self.coordinator.advance(str(self.store.state_dir), self.user, execution_id,
                                                 action="target", force=force, role=role)
            return reply
        return self.coordinator.advance(str(self.store.state_dir), self.user, execution_id,
                                        action=action, force=force, role=role,
                                        **({"refresh": False} if action == "status" and not refresh else {}))

    def finish(self, force=False):
        local = self.store.close_if_unmanaged(self.context["session"]["id"], user=self.user, force=force)
        if local is not None:
            return local
        return self.coordinator.finish(str(self.store.state_dir), self.user,
                                       self.context["session"]["id"], force=force)

    def wait(self, execution_id, *, until="running", timeout_seconds=30, poll_interval=1):
        """Wait on one owned execution; return the last facts on bounded timeout.

        A terminal failure ends a running wait. Release waits end only when
        the coordinator confirms both termination and resource release.
        """
        if until not in {"running", "released"}:
            raise ValueError("until must be running or released")
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError("timeout_seconds must be finite and nonnegative")
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval must be finite and positive")
        self._require_execution_id(execution_id)
        deadline = time.monotonic() + timeout_seconds
        while True:
            reply = self.observe(execution_id)
            terminal = reply.get("state") in DONE
            if until == "running" and (terminal or reply.get("state") == "running"):
                return reply
            if until == "released" and terminal and reply.get("resources_released") is True:
                return reply
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {**reply, "wait_timed_out": True}
            time.sleep(min(poll_interval, remaining))
