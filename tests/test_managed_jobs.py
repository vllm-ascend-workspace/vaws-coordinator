"""Generic process supervision lives in remote_dev.processes.control."""

from __future__ import annotations

import unittest
from pathlib import Path


class WorkerOwnershipTests(unittest.TestCase):
    def test_coordinator_no_longer_ships_a_container_supervisor(self):
        from vaws_coordinator import backend
        self.assertFalse(hasattr(backend, "worker_source"))
        self.assertFalse((Path(backend.__file__).resolve().parent / "workers" / "managed_jobs.py").is_file())
