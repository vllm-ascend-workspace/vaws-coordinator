"""Local task facade over this process's runtime pool.

Identity, source binding and native resume stay local. Remote execution talks
to an in-process RuntimePool for this user — never to a hosted manager.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
from pathlib import Path

from vaws_coordinator.agent_session import AgentSessions, load_context
from vaws_coordinator.build_inputs import BUILD_INPUT_ENV_KEYS
from vaws_coordinator.parity import materialize_command
from vaws_coordinator.state_paths import coordinator_state_dir

DONE = {"succeeded", "failed", "timeout", "cancelled", "inconclusive"}
LOCAL_OWNER = "local"


def _default_pool(sessions_dir: Path):
    from vaws_coordinator.backend import RemoteBackend
    from vaws_coordinator.ready_runtime import RuntimePool

    return RuntimePool(coordinator_state_dir(sessions_dir), RemoteBackend())


class TaskClient:
    def __init__(self, context_file="", *, pool=None):
        self.context = load_context(context_file)
        self.store = AgentSessions(Path(self.context["state_dir"]))
        self._pool = pool

    @property
    def pool(self):
        if self._pool is None:
            self._pool = _default_pool(self.store.state_dir)
        return self._pool

    def status(self):
        context = self.store.context(self.context["attachment"]["id"])
        with self.store.transaction() as db:
            attachments = [row for row in self.store.rows(db, "attachment") if row["session_id"] == context["session"]["id"]]
        return {**context, "attachments": attachments, "executions": self.store.executions(context["session"]["id"])}

    def sources(self, sources):
        self.context = self.store.bind_sources(self.context, sources)
        return self.context

    @contextlib.contextmanager
    def execution_lock(self, execution_id):
        if len(execution_id) != 64 or any(char not in "0123456789abcdef" for char in execution_id):
            raise ValueError("invalid local execution id")
        with (self.store.state_dir / ("execution-" + execution_id + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def run(self, request_id, command, profile_key="", runtime_id="", devices=None, npu_count=1,
            env=None, timeout_seconds=1800):
        if not request_id or len(request_id) > 128:
            raise ValueError("use one stable request_id per intended execution; retain it when retrying")
        spec = {"command": command, "profile_key": profile_key, "runtime_id": runtime_id,
                "devices": devices or [], "npu_count": 0 if devices else npu_count,
                "env": env or {}, "timeout_seconds": timeout_seconds}
        row = self.store.execution(self.context, request_id, spec)
        with self.execution_lock(row["id"]):
            with self.store.transaction() as db:
                row = self.store.get(db, "execution", row["id"])
            if row["phase"] in DONE:
                return {"execution_id": row["id"], "state": row["phase"], **row.get("observation", {})}
            if row.get("managed_job"):
                return self.observe(row["id"], _locked=True)
            context = self.store.context(self.context["attachment"]["id"])
            sources = {name: source["path"] for name, source in context["session"]["sources"].items()}
            if not {"vllm", "vllm-ascend"}.issubset(sources):
                raise ValueError("bind the actual vllm and vllm-ascend worktrees before an Ascend execution")
            if "remote_session" not in row:
                row["remote_session"] = self.pool.session_open(LOCAL_OWNER, context["session"]["id"], sources)
                self.store.save_execution(row)
            if "binding" not in row:
                if not profile_key:
                    candidates = [item for item in self.pool.catalog()
                                  if item["state"] == "ready" and (not runtime_id or item["runtime_id"] == runtime_id)]
                    profiles = {item["profile_key"] for item in candidates}
                    if len(profiles) != 1:
                        return {"state": "waiting_for_runtime", "execution_id": row["id"], "provisioning_started": False,
                                "reason": "no unique ready profile; select the required environment"}
                    profile_key = next(iter(profiles))
                binding = self.pool.checkout(LOCAL_OWNER, row["remote_session"]["id"], profile_key,
                                             row["id"], runtime_id)
                if binding.get("status") == "cache_miss":
                    return {**binding, "state": "waiting_for_runtime", "execution_id": row["id"]}
                row.update(binding=binding, phase="bound", sources=sources)
                self.store.save_execution(row)
            if "snapshots" not in row:
                row["snapshots"] = self._sync(row)
                row["phase"] = "launch_pending"
                self.store.save_execution(row)
            binding = row["binding"]
            job = self.pool.managed_start(
                LOCAL_OWNER, binding["id"], row["id"], row["snapshots"], binding["build_key"],
                spec["devices"], spec["npu_count"], command, env or {}, timeout_seconds,
            )
            row.update(managed_job=job["id"], phase=job["state"], observation=job)
            self.store.save_execution(row)
            return {"execution_id": row["id"], **job}

    def _sync(self, row):
        binding = row["binding"]
        endpoint = binding["endpoint"]
        directory = self.store.state_dir / "runs" / row["id"]
        directory.mkdir(parents=True, exist_ok=True)
        args = materialize_command(workspace_id=self.context["session"]["id"],
                                   runtime_id=binding["runtime_id"], endpoint=endpoint,
                                   sources={name: row["sources"][name] for name in ("vllm", "vllm-ascend")})
        environment = {key: value for key, value in os.environ.items() if key not in BUILD_INPUT_ENV_KEYS}
        environment.update(binding.get("build_env", {}))
        environment.update(binding["environment"])
        with (directory / "parity.json").open("w") as stdout, (directory / "parity.log").open("w") as stderr:
            result = subprocess.run(args, stdout=stdout, stderr=stderr,
                                    env=environment, timeout=600, check=False)
        if result.returncode:
            raise RuntimeError(f"source synchronization failed; no job launched; inspect {directory / 'parity.log'}")
        payload = json.loads((directory / "parity.json").read_text())
        if payload.get("status") not in {"ready", "materialized"}:
            raise RuntimeError("source staging alone does not authorize execution")
        return payload["snapshot_commits"]

    def observe(self, execution_id, action="status", force=False, *, _locked=False):
        if not _locked:
            with self.execution_lock(execution_id):
                return self.observe(execution_id, action, force, _locked=True)
        with self.store.transaction() as db:
            row = self.store.get(db, "execution", execution_id)
        if row["session_id"] != self.context["session"]["id"]:
            raise PermissionError("execution belongs to another VAWS task")
        if not row.get("managed_job"):
            if row.get("phase") == "launch_pending":
                matches = [job for job in self.pool.status(LOCAL_OWNER)["jobs"]
                           if job["binding_id"] == row["binding"]["id"]
                           and job["request"]["request_id"] == row["id"]]
                if len(matches) == 1:
                    row["managed_job"] = matches[0]["id"]
                    self.store.save_execution(row)
                    return self.observe(execution_id, action, force, _locked=True)
                if action != "stop":
                    raise RuntimeError("launch outcome requires retrying the same vaws_run request_id")
            if action == "stop" and row.get("binding"):
                self.pool.return_runtime(LOCAL_OWNER, row["binding"]["id"])
                row["phase"] = "cancelled"
                self.store.save_execution(row)
            elif action == "stop":
                row["phase"] = "cancelled"
                self.store.save_execution(row)
            return {"execution_id": row["id"], "state": row["phase"]}
        job = self.pool.managed_control(LOCAL_OWNER, row["managed_job"], action, force)
        row.update(phase=job["state"], observation=job)
        self.store.save_execution(row)
        return {"execution_id": row["id"], **job}

    def finish(self, force=False):
        with self.store.transaction() as db:
            session = self.store.get(db, "session", self.context["session"]["id"])
            session["state"] = "finishing"
            self.store.put(db, "session", session)
        executions = self.store.executions(self.context["session"]["id"])
        states = []
        for row in executions:
            if row["phase"] in DONE:
                continue
            states.append(self.observe(row["id"], "stop", force))
        finished = all(row["state"] in DONE for row in states)
        with self.store.transaction() as db:
            session = self.store.get(db, "session", self.context["session"]["id"])
            session["state"] = "finished" if finished else "finishing"
            self.store.put(db, "session", session)
        return {"state": session["state"], "executions": states, "worktrees_preserved": True}
