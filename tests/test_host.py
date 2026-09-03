"""Tests for core.host — host-PC telemetry (the "is my PC keeping up?" half).

Best-effort by design: probes never raise. On Windows they hit kernel32;
this just asserts the snapshot shape is always sane and never 500s the
dashboard. No device required.
Run: python -m unittest tests.test_host -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import host as HOST  # noqa: E402


class TestHostSnapshot(unittest.TestCase):
    def test_snapshot_shape(self):
        snap = HOST.snapshot()
        self.assertTrue(snap["ok"])
        self.assertIn("cpu_pct", snap)
        self.assertIsInstance(snap["cpu_count"], int)
        self.assertIn("mem", snap)
        self.assertIn("disk", snap)
        self.assertIsInstance(snap["emulators"], list)
        self.assertIsInstance(snap["emulator_count"], int)
        self.assertIsInstance(snap["emulator_mem_mb"], int)
        # cpu_pct is either a real number or gracefully None
        self.assertTrue(snap["cpu_pct"] is None or isinstance(snap["cpu_pct"], float))

    def test_memory_returns_total(self):
        mem = HOST.memory()
        if mem:  # Windows will populate this
            self.assertIn("total_mb", mem)
            self.assertGreater(mem["total_mb"], 0)

    def test_emulator_processes_is_list(self):
        procs = HOST.emulator_processes()
        self.assertIsInstance(procs, list)


if __name__ == "__main__":
    unittest.main()
