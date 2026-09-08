from __future__ import annotations

import unittest
from unittest import mock

from vaws_coordinator import ops as vaws_ops
from vaws_coordinator import task_client as vaws_task_client


class FakeTaskClient:
    state = "finishing"

    def __init__(self, *_args, **_kwargs):
        self.context = {"session": {"id": "sess-test"}}

    def finish(self, _force=False):
        return {"state": self.state, "executions": [], "worktrees_preserved": True}


class VawsOpsTests(unittest.TestCase):
    def test_finish_non_terminal_state_is_blocked_not_success(self) -> None:
        with mock.patch.object(vaws_task_client, "TaskClient", FakeTaskClient):
            payload = vaws_ops.vaws_call("vaws.finish", {})
        self.assertEqual(payload["result"]["status"], "finishing")
        self.assertEqual(payload["result"]["outcome"], "blocked")

    def test_finish_terminal_state_stays_success(self) -> None:
        class DoneClient(FakeTaskClient):
            state = "finished"

        with mock.patch.object(vaws_task_client, "TaskClient", DoneClient):
            payload = vaws_ops.vaws_call("vaws.finish", {})
        self.assertEqual(payload["result"]["status"], "finished")
        self.assertEqual(payload["result"]["outcome"], "success")


if __name__ == "__main__":
    unittest.main()
