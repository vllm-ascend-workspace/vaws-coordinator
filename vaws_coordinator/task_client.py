"""Local task facade. Hosted work is admitted to the persistent coordinator."""

from __future__ import annotations

import getpass
from pathlib import Path

from vaws_coordinator.agent_session import AgentSessions, load_context
from vaws_coordinator.placement import normalize_resources, role_plan, validate_user_env
from vaws_coordinator.ready_runtime import safe_id
from vaws_coordinator.state_paths import coordinator_state_dir

DONE = {"succeeded", "failed", "timeout", "cancelled", "inconclusive"}


def coordinator_user(explicit: str | None = None) -> str:
    if explicit:
        return safe_id(explicit)
    return safe_id(getpass.getuser())


class TaskClient:
    def __init__(self, context_file="", *, pool=None, user=None, service=None):
        self.context = load_context(context_file)
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

    def run(self, command, *, env=None, environment=None, resources=None, topology=None,
            timeout_seconds=1800, service=None, restart=False, preflight=None):
        if not command or not isinstance(command, str) or not command.strip():
            raise ValueError("command is required")
        env = validate_user_env(env)
        resources = normalize_resources(resources)
        roles = role_plan(topology, resources, command)
        if preflight is not None and (not isinstance(preflight, str) or not preflight.strip()):
            raise ValueError("preflight must be a nonempty shell command")
        spec = {
            "command": command, "env": env, "environment": environment or {},
            "resources": resources, "topology": topology or {}, "roles": roles,
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

    def observe(self, execution_id, action="status", force=False, role=None):
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
                                        action=action, force=force, role=role)

    def finish(self, force=False):
        return self.coordinator.finish(str(self.store.state_dir), self.user,
                                       self.context["session"]["id"], force=force)
