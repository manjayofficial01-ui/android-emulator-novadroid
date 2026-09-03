"""Tests for core.avd — disk scan, multi-instance serial map, list merge.

No live emulator. Verifies the fixes:
  * _scan_avd_names finds AVDs whose .ini is missing (the running-but-hidden
    bug) by scanning the AVD home, not the CLI list.
  * _serial_map resolves per-instance via `adb emu avd name` so stopping AVD
    B can't accidentally kill AVD A.
  * list_avds merges CLI + disk so a disk-only AVD still appears.
Run: python -m unittest tests.test_avd -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import avd as AVD  # noqa: E402
from core.config import ToolPaths  # noqa: E402

FAKE_CLI = ToolPaths(sdk=None, android_cli="android.exe", adb="adb.exe",
                     emulator=None, emulator_check=None, avdmanager=None)


class TestScanAvdNames(unittest.TestCase):
    def test_finds_disk_and_ini_avds(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "medium_phone.avd").mkdir()
            (Path(td) / "medium_tablet.ini").write_text("", encoding="utf-8")
            (Path(td) / "readme.txt").write_text("x", encoding="utf-8")  # ignored
            with mock.patch.object(AVD, "avd_home", return_value=Path(td)):
                names = AVD._scan_avd_names()
        self.assertEqual(names, ["medium_phone", "medium_tablet"])

    def test_missing_ini_still_found(self):
        """The headline scenario: .avd dir exists, .ini deleted -> still seen."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "ghost_avd.avd").mkdir()  # no ghost_avd.ini
            with mock.patch.object(AVD, "avd_home", return_value=Path(td)):
                names = AVD._scan_avd_names()
        self.assertIn("ghost_avd", names)


class TestSerialMap(unittest.TestCase):
    def setUp(self):
        # _serial_map caches into a module-global with a 5s TTL; clear it so
        # each test exercises its own stub instead of reading a stale cache.
        AVD._SERIAL_MAP, AVD._MAP_TS = {}, 0.0

    def _fake_run(self, cmd, **kw):
        if len(cmd) >= 2 and cmd[1] == "devices":
            return {"ok": True, "out":
                    "List of devices attached\n"
                    "emulator-5554 device\nemulator-5556 device\n", "err": ""}
        if "-s" in cmd and "avd" in cmd and "name" in cmd:
            serial = cmd[cmd.index("-s") + 1]
            name = {"emulator-5554": "medium_phone",
                    "emulator-5556": "medium_tablet"}.get(serial, "")
            return {"ok": True, "out": name + "\nOK", "err": ""}
        return {"ok": True, "out": "", "err": ""}

    def test_maps_each_instance(self):
        with mock.patch.object(AVD, "run", self._fake_run):
            m = AVD._serial_map(FAKE_CLI)
        self.assertEqual(m, {"medium_phone": "emulator-5554",
                             "medium_tablet": "emulator-5556"})

    def test_ok_ko_lines_ignored(self):
        def fr(cmd, **kw):
            if len(cmd) >= 2 and cmd[1] == "devices":
                return {"ok": True, "out": "emulator-5554 device\n", "err": ""}
            return {"ok": True, "out": "OK\nmedium_phone\n", "err": ""}
        with mock.patch.object(AVD, "run", fr):
            m = AVD._serial_map(FAKE_CLI)
        self.assertEqual(m, {"medium_phone": "emulator-5554"})

    def test_serial_for_avd(self):
        with mock.patch.object(AVD, "_serial_map",
                               return_value={"medium_phone": "emulator-5554"}):
            self.assertEqual(AVD.serial_for_avd(FAKE_CLI, "medium_phone"),
                             "emulator-5554")
            self.assertEqual(AVD.serial_for_avd(FAKE_CLI, "nope"), "")


class TestListAvdsMerge(unittest.TestCase):
    def test_cli_and_disk_merged(self):
        with mock.patch.object(AVD, "run", return_value={
                "ok": True, "out": "medium_phone\nmedium_tablet\n",
                "err": ""}):
            with mock.patch.object(AVD, "_scan_avd_names",
                                   return_value={"medium_phone",
                                                 "medium_tablet",
                                                 "ghost_avd"}):
                with mock.patch.object(AVD, "_serial_map",
                                       return_value={"medium_phone":
                                                     "emulator-5554"}):
                    res = AVD.list_avds(FAKE_CLI)
        self.assertTrue(res["ok"])
        names = {a["name"] for a in res["avds"]}
        self.assertEqual(names, {"medium_phone", "medium_tablet", "ghost_avd"})
        by = {a["name"]: a for a in res["avds"]}
        self.assertTrue(by["medium_phone"]["running"])
        self.assertFalse(by["ghost_avd"]["running"])  # disk-only, not up


if __name__ == "__main__":
    unittest.main()
