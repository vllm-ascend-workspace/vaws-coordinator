#!/usr/bin/env python3
"""Install scoped native session hooks and the two stdio MCP entries.

This configures files only. It does not grant client trust, change approval
policies, authenticate clients, or contact a remote machine. The coordinator's
own authenticated HTTP MCP entry stays a manual private-file step: this helper
never writes a bearer token.

Two stdio servers are written, because two repositories serve two different
things and neither proxies the other:

* `vaws-task` -> this checkout's `task_server.py`, which serves the four task
  tools (`vaws_session`, `vaws_run`, `vaws_execution`, `vaws_finish`). It is
  local-first and needs no remote-dev checkout.
* `remote-dev` -> `<remote-dev checkout>/mcp/server.py` (`--remote-dev-root`
  or `$VAWS_REMOTE_DEV_ROOT`), which serves the `remote_*` substrate tools.
  It no longer registers any task tool.

Writing both in one operation is where the split's distribution cost is
absorbed. `--task-only` skips the remote-dev entry for a deployment that has
no remote-dev checkout and only needs local task identity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from vaws_agent_session import CLIENTS
from vaws_remote_dev import REMOTE_DEV_ROOT_ENV, RemoteDevUnavailable
from vaws_state_paths import agent_sessions_root

EVENTS = ("SessionStart", "SessionEnd", "SubagentStart", "SubagentStop", "PreToolUse", "UserPromptSubmit")

# The hook performs up to two `git rev-parse` calls with a 5s timeout each
# (vaws_agent_session.worktree_reference); a 3s budget would kill a healthy hook.
HOOK_TIMEOUT_SECONDS = 12

# MCP server names as clients see them (Codex/Grok TOML keys replace "-" with
# "_"). Tool ids therefore read `mcp__vaws-task__vaws_session`, not
# `mcp__remote-dev__vaws_session` as they did while remote-dev hosted them.
TASK_SERVER_NAME = "vaws-task"
REMOTE_DEV_SERVER_NAME = "remote-dev"
TASK_SERVER = ROOT / "task_server.py"


def remote_dev_server(root=None):
    configured = str(root or os.environ.get(REMOTE_DEV_ROOT_ENV, ""))
    if not configured:
        raise RemoteDevUnavailable(
            f"the remote_* substrate tools are served by remote-dev; pass --remote-dev-root or "
            f"set {REMOTE_DEV_ROOT_ENV} so that MCP entry points at an actual server, or pass "
            f"--task-only to configure only this checkout's task server"
        )
    server = Path(configured).expanduser() / "mcp/server.py"
    if not server.is_file():
        raise RemoteDevUnavailable(f"remote-dev MCP server not found: {server}")
    return server


def task_server():
    if not TASK_SERVER.is_file():
        raise FileNotFoundError(f"task server not found in this checkout: {TASK_SERVER}")
    return TASK_SERVER


def mcp_servers(remote_dev_root=None, *, task_only=False):
    """Ordered `{server name: script path}` for every stdio entry to write."""
    servers = {}
    if not task_only:
        servers[REMOTE_DEV_SERVER_NAME] = remote_dev_server(remote_dev_root)
    servers[TASK_SERVER_NAME] = task_server()
    return servers


def hook_groups(client, project):
    command = shlex.join([sys.executable, str(ROOT / "hooks/vaws_session.py"),
                          "--client", client, "--project", str(project)])
    if client == "cursor":
        return {event[0].lower() + event[1:]: [{"command": command}]
                for event in EVENTS if event not in {"PreToolUse", "UserPromptSubmit"}}
    return {event: [{"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS}]}]
            for event in EVENTS}


def merge_json(path, *, hooks=None, mcp=None):
    value = json.loads(path.read_text()) if path.exists() else {}
    if hooks:
        target = value.setdefault("hooks", {})
        for event, groups in hooks.items():
            existing = target.setdefault(event, [])
            # Replace only this adapter for this project/client, preserving all
            # other hooks, including the user's policy and knowledge hooks.
            commands = {entry.get("command", "") for group in groups for entry in group.get("hooks", [group])}
            existing[:] = [group for group in existing if not any(
                entry.get("command", "") in commands for entry in group.get("hooks", [group]))]
            existing.extend(groups)
        if path.parent.name == ".cursor":
            value.setdefault("version", 1)
    if mcp:
        servers = value.setdefault("mcpServers", {})
        for name, script in mcp.items():
            # Update only our own entries; every other server, including the
            # user's authenticated coordinator HTTP entry, is left untouched.
            entry = servers.setdefault(name, {})
            entry.update(command=sys.executable, args=[str(script)], type="stdio")
            entry.setdefault("timeout", 600000)
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def managed_toml(original, name, text):
    """Replace or append one `# BEGIN/END VAWS <name>` block in TOML text."""
    begin, end = f"# BEGIN VAWS {name}\n", f"# END VAWS {name}\n"
    if begin in original:
        before, rest = original.split(begin, 1)
        _, after = rest.split(end, 1)
        original = before + after
    result = original.rstrip() + "\n\n" + begin + text.rstrip() + "\n" + end
    tomllib.loads(result)  # Refuse conflicting tables before writing anything.
    return result


def configuration(client, project, *, kimi_config=None, remote_dev_root=None, task_only=False):
    project = project.expanduser().resolve(strict=True)
    groups = hook_groups(client, project)
    servers = mcp_servers(remote_dev_root, task_only=task_only)
    files = {}
    if client in {"claude", "cursor", "codex", "grok"}:
        relative = {"claude": ".claude/settings.local.json", "cursor": ".cursor/hooks.json",
                    "codex": ".codex/hooks.json", "grok": ".grok/hooks/vaws-session.json"}[client]
        path = project / relative
        files[path] = merge_json(path, hooks=groups)
    if client in {"claude", "cursor", "kimi"}:
        path = project / {"claude": ".mcp.json", "cursor": ".cursor/mcp.json", "kimi": ".kimi-code/mcp.json"}[client]
        files[path] = merge_json(path, mcp=servers)
    if client in {"codex", "grok"}:
        path = project / ("." + client) / "config.toml"
        text = path.read_text() if path.exists() else ""
        existing = (tomllib.loads(text) if text else {}).get("mcp_servers", {})
        changed = False
        for name, script in servers.items():
            key = name.replace("-", "_")
            # Never replace an existing server or edit credentials/policies. A
            # configuration that already has one of the two servers gains only
            # the missing one; both load after the client's normal restart.
            if any(candidate in existing for candidate in (key, name)):
                continue
            body = f"[mcp_servers.{key}]\ncommand = " + json.dumps(sys.executable) + "\n"
            body += "args = " + json.dumps([str(script)]) + "\n"
            text = managed_toml(text, name, body)
            changed = True
        if changed:
            files[path] = text
    if client == "kimi":
        path = kimi_config or Path(os.environ.get("KIMI_CODE_HOME", str(Path.home() / ".kimi-code"))) / "config.toml"
        command = groups["SessionStart"][0]["hooks"][0]["command"]
        body = "\n".join("[[hooks]]\nevent = " + json.dumps(event) + "\ncommand = " + json.dumps(command) +
                         "\ntimeout = " + str(HOOK_TIMEOUT_SECONDS) + "\n" for event in EVENTS)
        project_key = hashlib.sha256(str(project).encode()).hexdigest()[:16]
        files[path] = managed_toml(path.read_text() if path.exists() else "", "session-" + project_key, body)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=sorted(CLIENTS), required=True)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--kimi-config", type=Path, help="Kimi's actual user config if launched with --config-file")
    parser.add_argument("--remote-dev-root", type=Path, help="remote-dev checkout serving the remote_* substrate "
                                                             "tools (default: $" + REMOTE_DEV_ROOT_ENV + ")")
    parser.add_argument("--task-only", action="store_true",
                        help="Write only this checkout's task server entry; skip the remote-dev entry")
    parser.add_argument("--apply", action="store_true", help="Write with private backups; default is preview")
    args = parser.parse_args()
    files = configuration(args.client, args.project, kimi_config=args.kimi_config,
                          remote_dev_root=args.remote_dev_root, task_only=args.task_only)
    servers = mcp_servers(args.remote_dev_root, task_only=args.task_only)
    changed = []
    for path, content in files.items():
        if path.exists() and path.read_text() == content:
            continue
        item = {"path": str(path), "sha256": hashlib.sha256(content.encode()).hexdigest()}
        if args.apply:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                directory = agent_sessions_root().parent / "client-setup"
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                backup = directory / (hashlib.sha256(str(path).encode()).hexdigest()[:16] + "-" + str(time.time_ns()))
                backup.write_bytes(path.read_bytes())
                backup.chmod(0o600)
                item["backup"] = str(backup)
            temporary = path.with_name(path.name + ".vaws-" + str(time.time_ns()))
            temporary.write_text(content)
            temporary.chmod(0o600)
            os.replace(temporary, path)
        changed.append(item)
    print(json.dumps({"state": "configured" if args.apply else "preview", "files": changed,
                      "mcp_servers": {name: str(path) for name, path in servers.items()},
                      "trust_granted": False, "connected": False,
                      "next": "Review native client trust/approval prompts for each listed server, restart or "
                              "resume the client, then verify actual calls."}))


if __name__ == "__main__":
    main()
