"""Provision host_ops.run_remote_script through remote-dev run_stream.

No real SSH. stream_ssh_command is replaced with a local argv so the shared
reader, deadline, capture, and on_output callback still run.
"""
from __future__ import annotations

import shlex
import base64
import sys
import time
import unittest
from unittest.mock import patch

import remote_dev.core.ssh_transport as ssh_transport

from vaws_coordinator.provision import host_ops

TARGET = host_ops.SshTarget(host="192.0.2.10", user="root", port=22)


def _local_bash(_endpoint, script, *, timeout_ms=None):
    del _endpoint, timeout_ms
    # Sending bytes avoids Windows Bash/WSL command-line quote translation.
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    source = ("import base64,subprocess,sys; "
              f"sys.exit(subprocess.run(['bash','-s'],input=base64.b64decode({encoded!r})).returncode)")
    return [sys.executable, "-c", source]


def _local_python(source: str):
    def _cmd(_endpoint, script, *, timeout_ms=None):
        del _endpoint, script, timeout_ms
        return [sys.executable, "-c", source]

    return _cmd


class RunRemoteScriptTransportTests(unittest.TestCase):
    def test_separate_channels_progress_and_sentinel(self) -> None:
        script = r"""
echo 'machine-out'
echo '__VAWS_PROGRESS__={"phase":"probe","message":"checking"}' >&2
echo 'warn-line' >&2
echo '__VAWS_JSON__={"success":true,"step":"probe"}'
"""
        with patch.object(ssh_transport, "stream_ssh_command", _local_bash):
            result = host_ops.run_remote_script(
                TARGET, script, stream_progress=False
            )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertIn("machine-out\n", result.stdout)
        self.assertIn("warn-line\n", result.stderr)
        self.assertIn(host_ops.PROGRESS_SENTINEL, result.stderr)
        self.assertEqual(result.payload, {"success": True, "step": "probe"})
        self.assertEqual(result.progress_events[0]["phase"], "probe")
        self.assertEqual(result.progress_events[0]["message"], "checking")

    def test_nonzero_status_is_preserved(self) -> None:
        script = "echo out; echo err >&2; exit 7"
        with patch.object(ssh_transport, "stream_ssh_command", _local_bash):
            result = host_ops.run_remote_script(TARGET, script, stream_progress=False)
        self.assertEqual(result.returncode, 7)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.stdout, "out\n")
        self.assertEqual(result.stderr, "err\n")
        self.assertIsNone(result.payload)

    def test_timeout_propagates_from_shared_reader(self) -> None:
        sleeper = "import sys,time; sys.stdout.buffer.write(b'partial'); sys.stdout.buffer.flush(); time.sleep(2)"
        started = time.monotonic()
        with patch.object(ssh_transport, "stream_ssh_command", _local_python(sleeper)):
            result = host_ops.run_remote_script(
                TARGET, "unused", timeout_seconds=1, stream_progress=False
            )
        elapsed = time.monotonic() - started
        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 1.5)
        self.assertIn("partial", result.stdout)
        self.assertIn("wall-clock", result.stderr)
        self.assertEqual(result.timeout_seconds, 1)

    def test_literal_multiline_quoted_and_unicode_args(self) -> None:
        args = [
            "hello world",
            "line1\nline2",
            "quote'\"and",
            "unicøde-Δ",
        ]
        script = r"""
printf '%s\n' "$1"
printf '%s\n' "$2"
printf '%s\n' "$3"
printf '%s\n' "$4"
echo '__VAWS_JSON__={"success":true,"n":4}'
"""
        captured: dict[str, str] = {}

        def standin(endpoint, script, *, timeout_ms=None):
            captured["script"] = script
            return _local_bash(endpoint, script, timeout_ms=timeout_ms)

        with patch.object(ssh_transport, "stream_ssh_command", standin):
            result = host_ops.run_remote_script(
                TARGET, script, args=args, stream_progress=False
            )
        composed = captured["script"]
        self.assertTrue(composed.startswith("set -- "))
        for item in args:
            self.assertIn(shlex.quote(item), composed)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "hello world")
        self.assertEqual(lines[1], "line1")
        self.assertEqual(lines[2], "line2")
        self.assertEqual(lines[3], "quote'\"and")
        self.assertEqual(lines[4], "unicøde-Δ")
        self.assertEqual(result.payload, {"success": True, "n": 4})


if __name__ == "__main__":
    unittest.main()
