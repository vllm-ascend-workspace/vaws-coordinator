"""Local development-task identity, independent of the remote fleet.

Native sessions are attachments, not task ids. A new native root session creates
a new task; resuming that same native session retains its task. Only an explicit
association or a child attachment joins another existing task. No operation here
allocates resources, reads transcripts, resets sources or removes worktrees.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from vaws_coordinator.state_paths import agent_sessions_root
from vaws_coordinator.client_paths import client_path

CLIENTS = {"claude", "grok", "kimi", "codex", "cursor"}


def git_common_directory(path: str | Path) -> Path:
    """Resolve repository identity with Git, including linked-worktree subdirs."""
    directory = Path(client_path(path)).expanduser().resolve(strict=True)
    result = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, encoding="utf-8", timeout=5, check=True,
    )
    return Path(client_path(result.stdout.strip())).resolve(strict=True)


def source_defaults(session: dict, attachment: dict) -> dict:
    """Select proven explicit task defaults or this native attachment's sources."""
    if session.get("source_mode") == "explicit":
        return {"origin": "explicit", "sources": session.get("sources", {})}
    if session.get("sources"):
        # Previous releases mixed automatic and explicit mappings. Neither a
        # matching path nor a one-entry map establishes which one was intended.
        return {"origin": "unknown", "sources": {},
                "reason": "Saved source defaults have no provenance; set sources explicitly before submitting, "
                          "or pass sources on this run. No legacy mapping was assumed to follow the native cwd."}
    return {"origin": attachment.get("source_mode", "none"), "sources": attachment.get("sources", {})}


def worktree_reference(path: str) -> dict:
    """Inspect an actual repository; never materialize a second source copy."""
    source = Path(client_path(path)).expanduser().resolve(strict=True)
    result = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, encoding="utf-8", timeout=5, check=True,
    )
    root = Path(result.stdout.strip()).resolve()
    info = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", timeout=5, check=True,
    ).stdout.splitlines()
    return {"path": str(root), "git_common_dir": str((root / info[0]).resolve()), "head_at_bind": info[1]}


class AgentSessions:
    def __init__(self, state_dir: Path | None = None):
        self.state_dir = Path(client_path(state_dir or agent_sessions_root())).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.state_dir / "sessions.sqlite3"
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records(kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,id))")

    @contextlib.contextmanager
    def transaction(self):
        # `with sqlite3.connect(...)` commits or rolls back but never closes.
        with contextlib.closing(sqlite3.connect(self.db_path, timeout=10)) as db:
            with db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                yield db

    @staticmethod
    def get(db, kind, key):
        row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if row is None:
            raise ValueError(f"unknown VAWS {kind}")
        return json.loads(row[0])

    @staticmethod
    def put(db, kind, value):
        db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)", (kind, value["id"], json.dumps(value, sort_keys=True)))

    @staticmethod
    def rows(db, kind):
        return [json.loads(row[0]) for row in db.execute("SELECT data FROM records WHERE kind=? ORDER BY rowid", (kind,))]

    def context(self, attachment_id: str) -> dict:
        with self.transaction() as db:
            attachment = self.get(db, "attachment", attachment_id)
            session = self.get(db, "session", attachment["session_id"])
        return {"schema_version": "vaws.agent-context.v1", "state_dir": str(self.state_dir),
                "session": session, "attachment": attachment,
                "source_defaults": source_defaults(session, attachment),
                "context_file": str(self.state_dir / "contexts" / (attachment_id + ".json"))}

    def _publish(self, attachment_id):
        context = self.context(attachment_id)
        path = Path(context["context_file"])
        path.parent.mkdir(exist_ok=True, mode=0o700)
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        with temporary.open("x") as stream:
            os.chmod(temporary, 0o600)
            json.dump({key: context[key] for key in ("schema_version", "state_dir")}
                      | {"attachment_id": attachment_id}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return context

    def attach(self, client: str, native_session_id: str, cwd: str, *, parent_context: str = "",
               association: str = "", agent_id: str = "") -> dict:
        if client not in CLIENTS or not native_session_id or len(native_session_id) > 512:
            raise ValueError("a supported client and its actual native session id are required")
        if parent_context and association:
            raise ValueError("choose child inheritance or an explicit task association")
        parent = load_context(parent_context or association) if parent_context or association else None
        if parent and Path(parent["state_dir"]) != self.state_dir:
            raise ValueError("use the associated task's local registry; do not duplicate its identity")
        now = time.time()
        # Native identity, not cwd/window/PID/history recency, distinguishes a
        # new task from resume. Subagent ids supplement clients that reuse the
        # parent's native session id for child conversations.
        identity = [client, native_session_id, agent_id]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        actual_cwd = str(Path(client_path(cwd)).expanduser().resolve())
        with self.transaction() as db:
            existing = [row for row in self.rows(db, "attachment") if row["id"] == key]
            if not existing:
                session_id = parent["session"]["id"] if parent else "vaws-" + uuid.uuid4().hex
                if parent:
                    session = self.get(db, "session", session_id)
                    if session["state"] != "open":
                        raise ValueError("task is finished; explicitly reopen it before attaching")
                else:
                    self.put(db, "session", {"id": session_id, "state": "open", "created_at": now,
                                             "sources": {}, "source_mode": "automatic"})
                self.put(db, "attachment", {
                    "id": key, "session_id": session_id, "client": client,
                    "native_session_id": native_session_id, "agent_id": agent_id or None,
                    "parent_id": parent["attachment"]["id"] if parent_context else None,
                    "association": "child" if parent_context else "explicit" if association else "new-task",
                    "cwd": actual_cwd, "state": "attached", "created_at": now,
                })
            else:
                old = existing[0]
                if parent and old["session_id"] != parent["session"]["id"]:
                    raise ValueError("native session already belongs to another task")
                session = self.get(db, "session", old["session_id"])
                if parent_context:
                    # A child resume keeps the fresh-attach invariant: never
                    # join a task whose finish has already started.
                    if session["state"] != "open":
                        raise ValueError("task is finished; explicitly reopen it before attaching")
                elif session["state"] == "finishing":
                    raise ValueError("task is finishing; wait for the coordinator to complete cleanup")
                elif session["state"] == "finished":
                    session["state"] = "open"
                    self.put(db, "session", session)
                if old.get("cwd") != actual_cwd:
                    # A native handoff can move this attachment. Never retain
                    # the previous mutable automatic source after that move.
                    old.pop("sources", None)
                    old.pop("source_mode", None)
                old.update(state="attached", resumed_at=now, cwd=actual_cwd)
                self.put(db, "attachment", old)
        return self._publish(key)

    def native_context(self, client: str, native_session_id: str, agent_id: str = "") -> dict:
        with self.transaction() as db:
            matches = [row for row in self.rows(db, "attachment")
                       if row["client"] == client and row["native_session_id"] == native_session_id
                       and (row.get("agent_id") or "") == agent_id and row["state"] == "attached"]
        if len(matches) != 1:
            raise ValueError("native session association is missing or ambiguous; recover by passing the "
                             "task context file explicitly (context_file argument or VAWS_CONTEXT_FILE)")
        return self.context(matches[0]["id"])

    def bind_sources(self, context: dict, sources: dict[str, str]) -> dict:
        references = {}
        for name, path in sources.items():
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise ValueError("source names must be single repository names")
            references[name] = worktree_reference(path)
        with self.transaction() as db:
            session = self.get(db, "session", context["session"]["id"])
            if session["state"] != "open":
                raise ValueError("resume the task before binding sources")
            # Defaults apply only to future submissions. Replace the mapping
            # so {} can deliberately clear it and removed repos do not linger.
            session["sources"] = references
            session["source_mode"] = "explicit"
            self.put(db, "session", session)
        return self.context(context["attachment"]["id"])

    def bind_native_sources(self, context: dict) -> dict:
        """Bind only this attachment's actual cwd, without changing task defaults."""
        attachment = self.context(context["attachment"]["id"])["attachment"]
        reference = worktree_reference(attachment["cwd"])
        common = Path(reference["git_common_dir"])
        name = common.parent.name if common.name == ".git" else common.stem
        with self.transaction() as db:
            current = self.get(db, "attachment", attachment["id"])
            if current["cwd"] != attachment["cwd"]:
                raise ValueError("native working directory changed while binding its source")
            current.update(sources={name: reference}, source_mode="native-cwd")
            self.put(db, "attachment", current)
        return self.context(attachment["id"])

    def detach(self, context: dict) -> dict:
        with self.transaction() as db:
            attachment = self.get(db, "attachment", context["attachment"]["id"])
            attachment.update(state="detached", detached_at=time.time())
            self.put(db, "attachment", attachment)
        # Detaching a frontend never stops a job or releases a lease.
        return self.context(attachment["id"])

    def sessions(self) -> list[dict]:
        with self.transaction() as db:
            return self.rows(db, "session")

    def executions(self, session_id: str) -> list[dict]:
        with self.transaction() as db:
            return [row for row in self.rows(db, "execution") if row["session_id"] == session_id]

    def all_executions(self) -> list[dict]:
        with self.transaction() as db:
            return self.rows(db, "execution")

    def close_if_unmanaged(self, session_id: str, *, user: str, force=False) -> dict | None:
        """Close a task locally only when it has never admitted remote work.

        The same write transaction guards admission's open-state check, so a
        concurrent submit either becomes managed first or sees a closed task.
        """
        with self.transaction() as db:
            session = self.get(db, "session", session_id)
            rows = [row for row in self.rows(db, "execution") if row["session_id"] == session_id]
            remote_facts = ("remote_session", "roles", "managed_job", "binding", "preparation_jobs")
            if any(row.get("admitted") or any(row.get(key) for key in remote_facts) for row in rows):
                return None
            if session["state"] != "finished":
                session["state"] = "finished"
                session["finish"] = {"user": user, "force": bool(force), "at": time.time()}
                self.put(db, "session", session)
            observations = []
            for row in rows:
                row.update(phase="cancelled", cancel_requested=True)
                self.put(db, "execution", row)
                observations.append({"execution_id": row["id"], "state": "cancelled", "resources_released": True})
            return {"state": "finished", "executions": observations, "worktrees_preserved": True}

    def admit_execution(self, session_id: str, request_id: str, spec: dict, *, user: str) -> dict:
        """Publish accepted execution facts atomically with the open-task check."""
        key = hashlib.sha256(json.dumps([session_id, request_id]).encode()).hexdigest()
        with self.transaction() as db:
            if self.get(db, "session", session_id)["state"] != "open":
                raise ValueError("task admission is closed; resume the task before starting another execution")
            existing = [row for row in self.rows(db, "execution") if row["id"] == key]
            if existing:
                row = existing[0]
                if row["spec"] != spec:
                    raise ValueError("execution request id reused with different arguments")
                if row.get("admitted"):
                    if row.get("user") != user:
                        raise PermissionError("execution belongs to another principal")
                    return row
            else:
                row = {"id": key, "session_id": session_id, "request_id": request_id,
                       "spec": spec, "created_at": time.time()}
            row.update(phase="queued", admitted=True, user=user)
            self.put(db, "execution", row)
            return row

    def execution(self, context: dict, request_id: str, spec: dict) -> dict:
        key = hashlib.sha256(json.dumps([context["session"]["id"], request_id]).encode()).hexdigest()
        with self.transaction() as db:
            matches = [row for row in self.rows(db, "execution") if row["id"] == key]
            if matches:
                if matches[0]["spec"] != spec:
                    raise ValueError("execution request id reused with different arguments")
                return matches[0]
            if self.get(db, "session", context["session"]["id"])["state"] != "open":
                raise ValueError("resume the task before starting another execution")
            row = {"id": key, "session_id": context["session"]["id"], "request_id": request_id,
                   "spec": spec, "phase": "planned", "created_at": time.time()}
            self.put(db, "execution", row)
            return row

    def save_execution(self, row):
        with self.transaction() as db:
            try:
                current = self.get(db, "execution", row["id"])
            except ValueError:
                current = {}
            if current.get("cancel_requested"):
                row["cancel_requested"] = True
            if current.get("force"):
                row["force"] = True
            self.put(db, "execution", row)


def load_context(context_file: str = "", *, allow_native_context: bool = True) -> dict:
    filename = context_file or os.environ.get("VAWS_CONTEXT_FILE", "")
    if not filename:
        # Codex exposes a native thread id to local commands even when the
        # session hook cannot export VAWS_CONTEXT_FILE to their environment.
        # Resolve only that identity; cwd is attachment metadata, never a key.
        native = os.environ.get("CODEX_THREAD_ID", "").strip()
        session = os.environ.get("CODEX_SESSION_ID", "").strip()
        if not native or not allow_native_context:
            raise ValueError("VAWS context is required; use the native session hook or explicit task association")
        if session and session != native:
            raise ValueError("conflicting native Codex identities; pass context_file explicitly")
        parent = os.environ.get("VAWS_PARENT_CONTEXT", "")
        association = os.environ.get("VAWS_ATTACH_CONTEXT", "")
        if parent or association:
            inherited = load_context(parent or association)
            store = AgentSessions(Path(inherited["state_dir"]))
        else:
            store = AgentSessions()
            try:
                return store.native_context("codex", native)
            except ValueError:
                pass
        return store.attach("codex", native, str(Path.cwd()),
                            parent_context=parent, association=association)
    path = Path(client_path(filename)).expanduser().resolve(strict=True)
    reference = json.loads(path.read_text())
    if reference.get("schema_version") != "vaws.agent-context.v1":
        raise ValueError("not a VAWS agent context")
    store = AgentSessions(Path(reference["state_dir"]))
    context = store.context(reference["attachment_id"])
    if Path(context["context_file"]) != path:
        raise ValueError("context path does not match its registered attachment")
    return context
