"""Durable remote-dev ownership for coordinator preparation commands."""
from __future__ import annotations

import time
import uuid

from remote_dev.core.ssh_transport import RemoteCompleted
from remote_dev.processes import control


class PreparationCancelled(RuntimeError):
    """Cancellation completed with a verified quiet process family."""


class PreparationUncertain(RuntimeError):
    """Retained process facts require observation or stop, never command replay."""


def _remember(record, observation, save):
    # Output belongs to the step log, not the execution database. Keep the
    # receipt, outcome and cursors needed to observe/stop after daemon restart.
    record.update({key: value for key, value in observation.items()
                   if key not in {"stdout", "stderr", "processes", "timings"}})
    record["observed_at"] = time.time()
    save(record)


def stop_preparation_process(record, save, *, force=False, drain_seconds=10):
    """Stop only the persisted owned job; uncertainty never means completion."""
    try:
        observation = control(record["endpoint"], record["job_id"], "stop", force=force)
        _remember(record, observation, save)
        deadline = time.monotonic() + drain_seconds
        while not observation.get("quiet") and not observation.get("unknown") and time.monotonic() < deadline:
            time.sleep(0.1)
            observation = control(record["endpoint"], record["job_id"], "status")
            _remember(record, observation, save)
        return bool(observation.get("quiet"))
    except Exception as exc:
        _remember(record, {"state": "uncertain", "quiet": False, "error": str(exc)[:500]}, save)
        return False


class PreparationProcess:
    def __init__(self, endpoint, step, save, cancel_requested, *, timeout_seconds=7200):
        self.endpoint = dict(endpoint)
        self.step = step
        self.save = save
        self.cancel_requested = cancel_requested
        self.timeout_seconds = timeout_seconds

    def run(self, script, *, on_output):
        if self.cancel_requested():
            raise PreparationCancelled("preparation cancelled before command launch")
        record = {"endpoint": self.endpoint, "job_id": "prepare-" + uuid.uuid4().hex,
                  "step": self.step, "state": "pending", "quiet": False,
                  "stdout_offset": 0, "stderr_offset": 0}
        # Persist BEFORE the first remote side effect. A lost launch reply can
        # always be observed or stopped by this exact job id without replay.
        self.save(record)
        pending = {"stdout": "", "stderr": ""}

        def emit(observation, final=False):
            for channel in pending:
                pending[channel] += observation.get(channel, "")
                lines = pending[channel].splitlines(keepends=True)
                pending[channel] = ""
                for line in lines:
                    if final or line.endswith(("\n", "\r")):
                        on_output(channel, line)
                    else:
                        pending[channel] = line

        def exchange(wait=1000):
            observation = control(self.endpoint, record["job_id"], "exchange",
                                  stdout_offset=record["stdout_offset"], stderr_offset=record["stderr_offset"],
                                  max_bytes=32768, yield_time_ms=wait)
            emit(observation)
            _remember(record, observation, self.save)
            return observation

        try:
            observation = control(self.endpoint, record["job_id"], "launch", spec={
                "command": script, "cwd": self.endpoint["cwd"], "env": {},
                "timeout_seconds": self.timeout_seconds, "interactive": False,
            }, authorization={}, stdout_offset=record["stdout_offset"],
                stderr_offset=record["stderr_offset"], max_bytes=32768, yield_time_ms=1000)
            emit(observation)
            _remember(record, observation, self.save)
            while True:
                if self.cancel_requested():
                    if not stop_preparation_process(record, self.save):
                        raise PreparationUncertain("preparation stop has not verified quiet; retained job can be stopped again")
                    while True:
                        observation = exchange(0)
                        if not any(observation.get(channel + "_bytes_remaining") for channel in pending):
                            break
                    emit({}, final=True)
                    raise PreparationCancelled("preparation command stopped with verified quiet")
                if observation.get("unknown") or observation.get("state") in {"uncertain", "lost_outcome", "absent"}:
                    raise PreparationUncertain("preparation process outcome is unknown; command was not replayed")
                if observation.get("quiet") and not any(observation.get(channel + "_bytes_remaining") for channel in pending):
                    emit({}, final=True)
                    result = observation.get("result") or {}
                    code = result.get("exit_code")
                    if code is None:
                        raise PreparationUncertain("quiet preparation process has no command exit receipt")
                    return RemoteCompleted(int(code), "", "", timed_out=False)
                observation = exchange()
        except (PreparationCancelled, PreparationUncertain):
            raise
        except Exception as exc:
            _remember(record, {"state": "uncertain", "quiet": False, "error": str(exc)[:500]}, self.save)
            raise PreparationUncertain("preparation transport failed; retained owned job was not replayed") from exc
