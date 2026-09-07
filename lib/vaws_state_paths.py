"""Local state locations for this coordinator deployment (stdlib only).

The scaffold's `vaws_local_state` stays scaffold-owned: it also holds machine
profiles, workspace identity and inventory paths that this component has no
authority over. Only the two path resolvers the task registry actually needs
are re-homed here, and both accept an explicit override so an operator can
place task identity outside this checkout.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

STATE_DIRNAME = ".vaws-local"
ROOT = Path(__file__).resolve().parents[1]


def shared_workspace_root(repo_root: Path = ROOT) -> Path:
    """Resolve the primary worktree of `repo_root`, or `repo_root` itself.

    Linked Git worktrees share one Git common dir, so they share one local
    task registry. An unrelated clone resolves to itself and never discovers
    another clone's registry implicitly.
    """
    repo_root = repo_root.expanduser().resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return repo_root
    if result.returncode != 0 or not result.stdout.strip():
        return repo_root
    common_dir = Path(result.stdout.strip()).expanduser()
    if not common_dir.is_absolute():
        common_dir = repo_root / common_dir
    common_dir = common_dir.resolve()
    if common_dir.name.lower() == ".git" and common_dir.is_dir():
        return common_dir.parent
    return repo_root


def agent_sessions_root(repo_root: Path = ROOT) -> Path:
    """Return the local native-attachment registry directory.

    `VAWS_AGENT_SESSIONS_DIR` is the explicit deployment answer: several
    clients that must share one task identity have to name the same directory.
    Without it this falls back to this checkout's primary worktree, which is a
    single-installation default and never a cross-installation guess.
    """
    override = os.environ.get("VAWS_AGENT_SESSIONS_DIR", "")
    if override:
        return Path(override).expanduser()
    return shared_workspace_root(repo_root) / STATE_DIRNAME / "agent-sessions"
