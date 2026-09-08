from __future__ import annotations

from typing import Any


def remote_bash(endpoint, *, command, timeout_ms=None, runtime_env=None, **rest) -> dict[str, Any]:
    return {
        "result": {
            "outcome": "success",
            "status": "ok",
            "exit_code": 0,
            "refs": {"stdout": "", "stderr": ""},
        }
    }
