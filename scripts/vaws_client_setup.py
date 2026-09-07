#!/usr/bin/env python3
"""Install scoped native session hooks and the common remote-dev MCP entry.

This configures files only. It does not grant client trust, change approval
policies, authenticate clients, or contact a remote machine. The coordinator's
own authenticated HTTP MCP entry stays a manual private-file step: this helper
never writes a bearer token.

The stdio MCP entry points at a remote-dev checkout (`--remote-dev-root` or
`$VAWS_REMOTE_DEV_ROOT`), which is where the task tools are registered.
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


def remote_dev_server(root=None):
    configured = str(root or os.environ.get(REMOTE_DEV_ROOT_ENV, ""))
    if not configured:
        raise RemoteDevUnavailable(
            f"the task tools are served by remote-dev; pass --remote-dev-root or set "
            f"{REMOTE_DEV_ROOT_ENV} so the MCP entry points at an actual server"
        )
    server = Path(configured).expanduser() / "mcp/server.py"
    if not server.is_file():
        raise RemoteDevUnavailable(f"remote-dev MCP server not found: {server}")
    return server


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
        entry = value.setdefault("mcpServers", {}).setdefault("remote-dev", {})
        entry.update(command=sys.executable, args=[str(mcp)], type="stdio")
        entry.setdefault("timeout", 600000)
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def managed_toml(path, name, text):
    original = path.read_text() if path.exists() else ""
    begin, end = f"# BEGIN VAWS {name}\n", f"# END VAWS {name}\n"
    if begin in original:
        before, rest = original.split(begin, 1)
        _, after = rest.split(end, 1)
        original = before + after
    result = original.rstrip() + "\n\n" + begin + text.rstrip() + "\n" + end
    tomllib.loads(result)  # Refuse conflicting tables before writing anything.
    return result


def configuration(client, project, *, kimi_config=None, remote_dev_root=None):
    project = project.expanduser().resolve(strict=True)
    groups = hook_groups(client, project)
    server = remote_dev_server(remote_dev_root)
    files = {}
    if client in {"claude", "cursor", "codex", "grok"}:
        relative = {"claude": ".claude/settings.local.json", "cursor": ".cursor/hooks.json",
                    "codex": ".codex/hooks.json", "grok": ".grok/hooks/vaws-session.json"}[client]
        path = project / relative
        files[path] = merge_json(path, hooks=groups)
    if client in {"claude", "cursor", "kimi"}:
        path = project / {"claude": ".mcp.json", "cursor": ".cursor/mcp.json", "kimi": ".kimi-code/mcp.json"}[client]
        files[path] = merge_json(path, mcp=server)
    if client in {"codex", "grok"}:
        path = project / ("." + client) / "config.toml"
        original = tomllib.loads(path.read_text()) if path.exists() else {}
        servers = original.get("mcp_servers", {})
        # Never replace an existing server or edit credentials/policies. The
        # shared server gains the task tools after its normal client restart.
        if not any(name in servers for name in ("remote_dev", "remote-dev")):
            body = "[mcp_servers.remote_dev]\ncommand = " + json.dumps(sys.executable) + "\n"
            body += "args = " + json.dumps([str(server)]) + "\n"
            files[path] = managed_toml(path, "remote-dev", body)
    if client == "kimi":
        path = kimi_config or Path(os.environ.get("KIMI_CODE_HOME", str(Path.home() / ".kimi-code"))) / "config.toml"
        command = groups["SessionStart"][0]["hooks"][0]["command"]
        body = "\n".join("[[hooks]]\nevent = " + json.dumps(event) + "\ncommand = " + json.dumps(command) +
                         "\ntimeout = " + str(HOOK_TIMEOUT_SECONDS) + "\n" for event in EVENTS)
        project_key = hashlib.sha256(str(project).encode()).hexdigest()[:16]
        files[path] = managed_toml(path, "session-" + project_key, body)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=sorted(CLIENTS), required=True)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--kimi-config", type=Path, help="Kimi's actual user config if launched with --config-file")
    parser.add_argument("--remote-dev-root", type=Path, help="remote-dev checkout serving the task tools "
                                                             "(default: $" + REMOTE_DEV_ROOT_ENV + ")")
    parser.add_argument("--apply", action="store_true", help="Write with private backups; default is preview")
    args = parser.parse_args()
    files = configuration(args.client, args.project, kimi_config=args.kimi_config,
                          remote_dev_root=args.remote_dev_root)
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
                      "trust_granted": False, "connected": False,
                      "next": "Review native client trust/approval prompts, restart or resume the client, then verify actual calls."}))


if __name__ == "__main__":
    main()
