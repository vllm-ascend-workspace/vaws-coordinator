"""Private helpers for the coordinator-owned parity implementation.

Git, state, and SSH used by materialization live here so the package does not
reach into a consumer working tree. SSH option construction and attached
streams belong to ``vaws-remote-dev``.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

WORKSPACE_ID_PATTERN = re.compile(r'[^A-Za-z0-9._-]+')
STATE_SUBDIR = Path('.vaws-local/remote-code-parity')
DEFAULT_DENYLIST = (
    '.vaws-local/',
    '.vaws-runtime/',
    '.remote-code-parity/',
    '.workspace.local/',
    '.machine-inventory.json',
    '.codex/',
    '.claude/settings.local.json',
    '.env',
    '.env.*',
    '.venv/',
    'venv/',
    '__pycache__/',
    '.pytest_cache/',
    '.mypy_cache/',
    '.ruff_cache/',
    '*.log',
    '*.out',
    '.DS_Store',
    '._*',
    'Thumbs.db',
)

PROGRESS_SENTINEL = '__VAWS_PARITY_PROGRESS__='
STATE_LOCK_SUFFIX = '.lock'
DEFAULT_STATE_LOCK_TIMEOUT_SECONDS = 15.0
DEFAULT_STATE_LOCK_POLL_SECONDS = 0.05
DEFAULT_STATE_LOCK_STALE_SECONDS = 60 * 60 * 6


@dataclass(frozen=True)
class SshEndpoint:
    host: str
    port: int
    user: str

    def destination(self) -> str:
        return f'{self.user}@{self.host}'


@dataclass(frozen=True)
class SshStreamingResult:
    returncode: int
    stdout: str
    stderr: str
    progress_events: list[dict[str, Any]]


class LocalCommandError(RuntimeError):
    """A local child process completed with a nonzero exit code."""


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture_output: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            check=False,
            capture_output=capture_output,
            text=True, encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"command timed out after {timeout:.0f}s: "
            f"{' '.join(shlex.quote(part) for part in cmd)}\n"
            f'stdout:\n{exc.stdout or ""}\n'
            f'stderr:\n{exc.stderr or ""}'
        ) from exc
    if check and result.returncode != 0:
        raise LocalCommandError(
            f"command failed ({result.returncode}): {' '.join(shlex.quote(part) for part in cmd)}\n"
            f'stdout:\n{result.stdout}\n'
            f'stderr:\n{result.stderr}'
        )
    return result


def git(
    repo: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    options = ['-c', 'core.longpaths=true'] if os.name == 'nt' else []
    return run(['git', *options, '-C', str(repo), *args], env=env, check=check, timeout=timeout)


def repo_root_from(path: Path) -> Path:
    current = path.resolve()
    while True:
        if (current / '.git').exists():
            return current
        if current.parent == current:
            raise RuntimeError(f'could not find git repo root above {path}')
        current = current.parent


def state_dir(repo_root: Path) -> Path:
    target = repo_root / STATE_SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def canonical_state_path(repo_root: Path, filename: str) -> Path:
    return state_dir(repo_root) / filename


def load_state(repo_root: Path, filename: str, default: Any) -> Any:
    canonical = canonical_state_path(repo_root, filename)
    if canonical.exists():
        return json.loads(canonical.read_text(encoding='utf-8'))
    return default


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=True) + '\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def save_state(repo_root: Path, filename: str, data: Any) -> Path:
    path = canonical_state_path(repo_root, filename)
    _atomic_write_json(path, data)
    return path


@contextlib.contextmanager
def state_lock(
    repo_root: Path,
    filename: str,
    *,
    timeout_seconds: float = DEFAULT_STATE_LOCK_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_STATE_LOCK_POLL_SECONDS,
    stale_after_seconds: float = DEFAULT_STATE_LOCK_STALE_SECONDS,
):
    lock_path = canonical_state_path(repo_root, filename + STATE_LOCK_SUFFIX)
    deadline = time.monotonic() + timeout_seconds
    owner = {
        "pid": os.getpid(),
        "hostname": os.uname().nodename if hasattr(os, "uname") else None,
        "created_at": now_utc(),
    }
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, (json.dumps(owner, sort_keys=True) + "\n").encode('utf-8'))
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age >= stale_after_seconds:
                with contextlib.suppress(FileNotFoundError):
                    lock_path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(f'timed out waiting for state lock {lock_path}')
            time.sleep(poll_seconds)
    try:
        yield lock_path
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


def update_state(repo_root: Path, filename: str, default: Any, updater: Any) -> tuple[Any, Path, Any]:
    with state_lock(repo_root, filename):
        state = load_state(repo_root, filename, default)
        result = updater(state)
        path = save_state(repo_root, filename, state)
    return state, path, result


def now_utc() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def sanitize_repo_id(relpath: str) -> str:
    return 'workspace' if relpath in ('', '.') else relpath.replace('/', '__')


def json_dump(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def quoted(script: str) -> str:
    return shlex.quote(script)


def parse_progress_event(line: str) -> dict[str, Any] | None:
    if not line.startswith(PROGRESS_SENTINEL):
        return None
    try:
        payload = json.loads(line[len(PROGRESS_SENTINEL) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _remote_endpoint(endpoint: SshEndpoint, *, long_stream: bool = False):
    from remote_dev.core.endpoint import Endpoint

    if long_stream:
        return Endpoint.for_long_stream(host=endpoint.host, port=endpoint.port, user=endpoint.user)
    return Endpoint(host=endpoint.host, port=endpoint.port, user=endpoint.user)


class RemoteCommandError(RuntimeError):
    """A completed SSH command status, distinct from a lost transport."""

    def __init__(self, returncode: int, message: str):
        super().__init__(message)
        self.returncode = returncode


def _ssh_failure(returncode: int, stdout: str, stderr: str, *, what: str) -> RemoteCommandError:
    return RemoteCommandError(returncode,
        f'command failed ({returncode}): {what}\n'
        f'stdout:\n{stdout}\n'
        f'stderr:\n{stderr}'
    )


def ssh_exec(
    endpoint: SshEndpoint,
    script: str,
    *,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    from remote_dev.core.ssh_transport import run_script

    del capture_output
    completed = run_script(_remote_endpoint(endpoint), script)
    stdout = completed.stdout or ''
    stderr = completed.stderr or ''
    if completed.timed_out:
        returncode = 255
        stderr = stderr or 'remote command timed out'
    else:
        returncode = 0 if completed.returncode is None else int(completed.returncode)
    result = subprocess.CompletedProcess(
        ['ssh', endpoint.destination(), str(endpoint.port)],
        returncode,
        stdout,
        stderr,
    )
    if check and result.returncode != 0:
        raise _ssh_failure(result.returncode, stdout, stderr, what=f'ssh_exec {endpoint.destination()}')
    return result


def ssh_exec_stream(
    endpoint: SshEndpoint,
    script: str,
    *,
    check: bool = True,
    stream_progress: bool = True,
    on_progress=None,
    log_path=None,
    process=None,
) -> SshStreamingResult:
    from remote_dev.core.ssh_transport import run_stream

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    progress_events: list[dict[str, Any]] = []

    def on_output(channel: str, text: str) -> None:
        if log_path is not None:
            with Path(log_path).open('a', encoding='utf-8') as log:
                log.write(text)
        if channel == 'stdout':
            stdout_parts.append(text)
            return
        event = parse_progress_event(text)
        if event is not None:
            progress_events.append(event)
            if on_progress is not None:
                on_progress(event)
            if stream_progress:
                sys.stderr.write(text if text.endswith('\n') else text + '\n')
                sys.stderr.flush()
            return
        stderr_parts.append(text)

    completed = (process.run(script, on_output=on_output) if process is not None else run_stream(
        _remote_endpoint(endpoint, long_stream=True), script,
        merge_stderr=False, on_output=on_output,
    ))
    stdout = ''.join(stdout_parts)
    stderr = ''.join(stderr_parts)
    if completed.timed_out:
        returncode = 255
        if completed.stderr and completed.stderr not in stderr:
            stderr = f'{stderr}{completed.stderr}' if stderr else completed.stderr
    else:
        returncode = 0 if completed.returncode is None else int(completed.returncode)
    if check and returncode != 0:
        raise _ssh_failure(returncode, stdout, stderr, what=f'ssh_exec_stream {endpoint.destination()}')
    return SshStreamingResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        progress_events=progress_events,
    )


def ssh_stream_to_file(endpoint: SshEndpoint, remote_path: str, payload: str) -> None:
    from remote_dev.core.ssh_transport import run_bytes

    script = f'mkdir -p {quoted(str(PurePosixPath(remote_path).parent))} && cat > {quoted(remote_path)}'
    try:
        result = run_bytes(_remote_endpoint(endpoint, long_stream=True), script,
                           stdin=payload.encode('utf-8'), timeout_ms=120000)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f'SSH upload to {remote_path} timed out; remote outcome is unknown; payload was not replayed') from exc
    stdout = (result.stdout or b'').decode('utf-8', errors='replace')
    stderr = (result.stderr or b'').decode('utf-8', errors='replace')
    if result.returncode != 0:
        raise RuntimeError(
            f'failed to stream payload to {remote_path}\nstdout:\n{stdout}\nstderr:\n{stderr}'
        )


def ssh_stream_bytes_to_file(endpoint: SshEndpoint, remote_path: str, payload: bytes) -> None:
    from remote_dev.core.ssh_transport import run_bytes

    script = (
        f'mkdir -p {quoted(str(PurePosixPath(remote_path).parent))} && '
        f'head -c {len(payload)} > {quoted(remote_path)}'
    )
    try:
        result = run_bytes(_remote_endpoint(endpoint, long_stream=True), script,
                           stdin=payload, timeout_ms=1800000)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f'SSH binary upload to {remote_path} timed out; remote outcome is unknown; payload was not replayed') from exc
    if result.returncode != 0:
        stdout = (result.stdout or b'').decode('utf-8', errors='replace')
        stderr = (result.stderr or b'').decode('utf-8', errors='replace')
        raise RuntimeError(
            f'failed to stream binary payload to {remote_path}\n'
            f'stdout:\n{stdout}\n'
            f'stderr:\n{stderr}'
        )


def is_git_worktree(path: Path) -> bool:
    result = git(path, ['rev-parse', '--is-inside-work-tree'], check=False)
    if result.returncode != 0 or result.stdout.strip() != 'true':
        return False
    top = git(path, ['rev-parse', '--show-toplevel'], check=False)
    if top.returncode != 0:
        return False
    try:
        return Path(top.stdout.strip()).resolve() == path.resolve()
    except FileNotFoundError:
        return False


def ensure_local_git_identity(repo: Path) -> tuple[str | None, str | None]:
    # Despite the name this is read-only: it never sets git config, it only
    # reports the identity (if any) that snapshot commits would record.
    name = git(repo, ['config', '--get', 'user.name'], check=False).stdout.strip() or None
    email = git(repo, ['config', '--get', 'user.email'], check=False).stdout.strip() or None
    return name, email


def glob_match_any(path: str, patterns: Iterable[str]) -> bool:
    import fnmatch

    normalized = path.replace('\\', '/')
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)
