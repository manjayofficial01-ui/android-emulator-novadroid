"""v5.1.2 tests — the APK flow repair. Every test here would fail against the
v5.1.1 code:

  * A truncated quickboot snapshot (crash mid-save, ram.bin of a few hundred
    bytes) made `android emulator start` wait forever while QEMU died on
    "Error -1 from the snapshot callback" — no serial ever appeared, so no APK
    could ever install. NovaDroid now quarantines the bad snapshot and goes
    straight to a cold boot.
  * A hard-killed emulator leaves multiinstance.lock / hardware-qemu.ini.lock
    behind; the next launch could believe another instance is running and
    exit instantly. Stale locks are now cleared before every start.
  * When a quickboot still fails, start_avd retries ONCE with `--cold` inside
    the job thread and only then reports an error honestly.
Run: python -m unittest tests.test_v512 -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import apps as APPS  # noqa: E402
from core import avd as AVD  # noqa: E402
from core.config import ToolPaths  # noqa: E402

FAKE_CLI = ToolPaths(sdk=None, android_cli="android.exe", adb="adb.exe",
                     emulator=None, emulator_check=None, avdmanager=None)


def _reset_serial_cache():
    # _serial_map caches into a module-global with a 5s TTL.
    AVD._SERIAL_MAP, AVD._MAP_TS = {}, 0.0


class TestQuarantineBadSnapshot(unittest.TestCase):
    def _make_avd(self, td, ram_size=None):
        avd = Path(td) / "medium_phone.avd"
        snap = avd / "snapshots" / "default_boot"
        snap.mkdir(parents=True)
        if ram_size is not None:
            (snap / "ram.bin").write_bytes(b"x" * ram_size)
        return snap

    def test_truncated_snapshot_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            snap = self._make_avd(td, ram_size=329)
            with mock.patch.object(AVD, "avd_home", return_value=Path(td)):
                note = AVD.quarantine_bad_snapshot(FAKE_CLI, "medium_phone")
            self.assertIn("quarantined", note)
            self.assertIn("329", note)
            leftovers = list(snap.glob("ram.bin.corrupt-*"))
            self.assertEqual(len(leftovers), 1)
            self.assertEqual(leftovers[0].stat().st_size, 329)
            self.assertFalse((snap / "ram.bin").exists())

    def test_healthy_snapshot_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            snap = self._make_avd(td, ram_size=AVD._SNAPSHOT_RAM_MIN_BYTES + 1)
            with mock.patch.object(AVD, "avd_home", return_value=Path(td)):
                note = AVD.quarantine_bad_snapshot(FAKE_CLI, "medium_phone")
            self.assertEqual(note, "")
            self.assertTrue((snap / "ram.bin").exists())

    def test_missing_snapshot_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            self._make_avd(td)
            with mock.patch.object(AVD, "avd_home", return_value=Path(td)):
                self.assertEqual(
                    AVD.quarantine_bad_snapshot(FAKE_CLI, "medium_phone"), "")

    def test_invalid_name_is_noop(self):
        self.assertEqual(AVD.quarantine_bad_snapshot(FAKE_CLI, "../evil"), "")


class TestClearStaleLocks(unittest.TestCase):
    def test_removes_crash_leftovers(self):
        with tempfile.TemporaryDirectory() as td:
            avd = Path(td) / "medium_phone.avd"
            avd.mkdir()
            (avd / "multiinstance.lock").write_text("", encoding="utf-8")
            (avd / "hardware-qemu.ini.lock").mkdir()
            (avd / "hardware-qemu.ini").write_text("x", encoding="utf-8")
            with mock.patch.object(AVD, "avd_home", return_value=Path(td)):
                note = AVD.clear_stale_locks(FAKE_CLI, "medium_phone")
            self.assertIn("multiinstance.lock", note)
            self.assertIn("hardware-qemu.ini.lock", note)
            self.assertFalse((avd / "multiinstance.lock").exists())
            self.assertFalse((avd / "hardware-qemu.ini.lock").exists())
            self.assertTrue((avd / "hardware-qemu.ini").exists())

    def test_no_locks_no_note(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "medium_phone.avd").mkdir()
            with mock.patch.object(AVD, "avd_home", return_value=Path(td)):
                self.assertEqual(AVD.clear_stale_locks(FAKE_CLI, "medium_phone"), "")


class TestStartAvdHealsBeforeBoot(unittest.TestCase):
    def setUp(self):
        _reset_serial_cache()

    def tearDown(self):
        AVD._JOBS.pop("medium_phone", None)

    def _wait_terminal(self, timeout=5.0):
        """The boot state machine runs in a daemon thread; wait for it."""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if AVD._JOBS.get("medium_phone", {}).get("state") in ("ready", "error"):
                return AVD._JOBS["medium_phone"]
            time.sleep(0.02)
        return AVD._JOBS.get("medium_phone", {})

    def test_truncated_snapshot_skips_quickboot(self):
        with tempfile.TemporaryDirectory() as td:
            avd = Path(td) / "medium_phone.avd"
            snap = avd / "snapshots" / "default_boot"
            snap.mkdir(parents=True)
            (snap / "ram.bin").write_bytes(b"x" * 100)
            captured = {}

            def fake_bg(cmd, on_done=None, timeout=0):
                captured["cmd"] = list(cmd)
                return None

            with mock.patch.object(AVD, "avd_home", return_value=Path(td)), \
                 mock.patch.object(AVD, "run_in_background", fake_bg), \
                 mock.patch.object(AVD, "_await_serial", return_value=""):
                res = AVD.start_avd(FAKE_CLI, "medium_phone")
            self.assertTrue(res["ok"])
            self.assertIn("--cold", captured["cmd"])
            self.assertIn("quarantined", res["job"]["log"])
            self.assertEqual(res["mode"], "cold boot (no snapshot)")
            self.assertIn("error", self._wait_terminal()["state"])

    def test_failed_quickboot_retries_cold(self):
        """No serial after the quickboot spawn → one `--cold` retry → ready."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "medium_phone.avd").mkdir()
            spawns = []

            def fake_bg(cmd, on_done=None, timeout=0):
                spawns.append(list(cmd))
                return None

            serials = iter(["", "emulator-5554"])
            with mock.patch.object(AVD, "avd_home", return_value=Path(td)), \
                 mock.patch.object(AVD, "run_in_background", fake_bg), \
                 mock.patch.object(AVD, "_await_serial",
                                   side_effect=lambda *a, **kw: next(serials)), \
                 mock.patch.object(APPS, "wait_boot",
                                   return_value={"booted": True, "ok": True}):
                res = AVD.start_avd(FAKE_CLI, "medium_phone")
            job = self._wait_terminal()
            self.assertEqual(job.get("state"), "ready")
            self.assertTrue(job.get("booted"))
            self.assertEqual(job.get("serial"), "emulator-5554")
            self.assertIn("retrying with a cold boot", job.get("log", ""))
            self.assertEqual(spawns[0],
                             ["android.exe", "emulator", "start", "medium_phone"])
            self.assertEqual(spawns[1],
                             ["android.exe", "emulator", "start",
                              "medium_phone", "--cold"])
            self.assertTrue(res["ok"])

    def test_failed_cold_boot_errors_honestly(self):
        """Cold was already the plan — no second retry, honest error state."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "medium_phone.avd").mkdir()

            def fake_bg(cmd, on_done=None, timeout=0):
                return None

            with mock.patch.object(AVD, "avd_home", return_value=Path(td)), \
                 mock.patch.object(AVD, "run_in_background", fake_bg), \
                 mock.patch.object(AVD, "_await_serial", return_value=""):
                AVD.start_avd(FAKE_CLI, "medium_phone", cold=True)
            job = self._wait_terminal()
            self.assertEqual(job.get("state"), "error")
            self.assertFalse(job.get("booted"))
            self.assertIn("no serial appeared", job.get("log", ""))


class TestListAvdsIgnoresCliNoise(unittest.TestCase):
    """A transient CLI stderr warning ("Unable to …") once rendered a phantom
    "Unable" AVD row on the dashboard. CLI names now merge only when the AVD
    exists on disk — the disk scan is the ground truth."""

    def test_stderr_warning_never_becomes_an_avd(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "medium_phone.avd").mkdir()

            def fake_run(cmd, timeout=0):
                if len(cmd) >= 3 and cmd[1:3] == ["emulator", "list"]:
                    return {"ok": True, "out": "medium_phone\n",
                            "err": "Unable to connect to the agent daemon"}
                return {"ok": True, "out": "", "err": ""}

            with mock.patch.object(AVD, "avd_home", return_value=Path(td)), \
                 mock.patch.object(AVD, "run", fake_run), \
                 mock.patch.object(AVD, "_serial_map", return_value={}):
                res = AVD.list_avds(FAKE_CLI)
            names = {a["name"] for a in res["avds"]}
            self.assertEqual(names, {"medium_phone"})
            self.assertNotIn("Unable", names)

    def test_cli_only_name_without_disk_is_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "real.avd").mkdir()

            def fake_run(cmd, timeout=0):
                if len(cmd) >= 3 and cmd[1:3] == ["emulator", "list"]:
                    return {"ok": True, "out": "real\nghost_avd\n", "err": ""}
                return {"ok": True, "out": "", "err": ""}

            with mock.patch.object(AVD, "avd_home", return_value=Path(td)), \
                 mock.patch.object(AVD, "run", fake_run), \
                 mock.patch.object(AVD, "_serial_map", return_value={}):
                res = AVD.list_avds(FAKE_CLI)
            self.assertEqual({a["name"] for a in res["avds"]}, {"real"})


if __name__ == "__main__":
    unittest.main()
