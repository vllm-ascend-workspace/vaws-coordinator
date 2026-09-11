"""Actual Git output for Unicode local worktrees; no remote or device work."""
import json
import os
import subprocess
import sys
from pathlib import Path

from vaws_coordinator.agent_session import worktree_reference
from vaws_coordinator.code_identity import code_identity
from vaws_coordinator.git_sources import ensure_populated_worktree
from vaws_coordinator.parity import build_snapshot_records
from vaws_coordinator.state_paths import shared_workspace_root


def test_remote_upload_uses_posix_parent_and_exact_bytes(monkeypatch):
    from types import SimpleNamespace
    from vaws_coordinator.parity_support import SshEndpoint, ssh_stream_to_file, ssh_stream_bytes_to_file
    calls = []

    def capture(endpoint, script, *, stdin):
        calls.append((script, stdin))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("remote_dev.core.ssh_transport.run_bytes", capture)
    endpoint = SshEndpoint(host="example.invalid", port=22, user="test")
    ssh_stream_to_file(endpoint, "/tmp/nested path/manifests/test.json", "中文\n")
    ssh_stream_bytes_to_file(endpoint, "/tmp/nested path/bundles/test.bundle", bytes(range(256)))
    assert calls[0] == ("mkdir -p '/tmp/nested path/manifests' && cat > '/tmp/nested path/manifests/test.json'", "中文\n".encode())
    assert calls[1] == ("mkdir -p '/tmp/nested path/bundles' && head -c 256 > '/tmp/nested path/bundles/test.bundle'", bytes(range(256)))


def test_unicode_worktree_binding_snapshot_and_cli(tmp_path):
    root = tmp_path / "中文 🧪 source"
    root.mkdir()
    for args in [["init", "-q"], ["config", "user.name", "Test"],
                 ["config", "user.email", "test@example.invalid"], ["config", "core.autocrlf", "false"]]:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "中文.txt").write_bytes("原始\n".encode())
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True, capture_output=True)
    assert worktree_reference(str(root))["path"] == str(root.resolve())
    assert shared_workspace_root(root) == root.resolve()
    ensure_populated_worktree(root, "vllm")
    (root / "中文.txt").write_bytes("修改\n".encode())
    assert code_identity(root)["dirty"]
    rows = build_snapshot_records(root, "unicode", "test", ())
    assert rows and rows[0].commit
    proc = subprocess.run([sys.executable, "-m", "vaws_coordinator.vaws", "attach", "--client", "codex",
                           "--native-session-id", "unicode-test", "--cwd", str(root)],
                          capture_output=True, timeout=15,
                          env={**os.environ, "VAWS_AGENT_SESSIONS_DIR": str(tmp_path / "sessions"),
                               "PYTHONIOENCODING": "cp936" if os.name == "nt" else "utf-8"})
    assert proc.returncode == 0, proc.stderr
    assert "中文 🧪" in json.dumps(json.loads(proc.stdout.decode("utf-8")), ensure_ascii=False)
