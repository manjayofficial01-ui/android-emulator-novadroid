"""Tests for shared folder + APK compat — no device needed. Run: python -m unittest discover -s tests -v"""
import base64
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import shared as SHARE  # noqa: E402
from core import compat as COMPAT  # noqa: E402
from core.config import ToolPaths  # noqa: E402

PATHS = ToolPaths.discover()


class TempHome:
    """Redirect Path.home() via USERPROFILE for isolated tests."""
    def __init__(self, td):
        self.td, self.old = td, os.environ.get("USERPROFILE")

    def __enter__(self):
        os.environ["USERPROFILE"] = self.td
        return self

    def __exit__(self, *a):
        if self.old is None:
            del os.environ["USERPROFILE"]
        else:
            os.environ["USERPROFILE"] = self.old


class TestShared(unittest.TestCase):
    def test_status_creates_folders(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            s = SHARE.status()
            self.assertTrue(s["ok"] and Path(s["dir"]).exists())
            self.assertTrue((Path(s["dir"]) / "Desktop").is_dir())
            self.assertTrue((Path(s["dir"]) / "Device").is_dir())
            self.assertIn("desktop", s)
            self.assertIn("deviceFolder", s)
            self.assertEqual(s["device_dir"], "/sdcard/NovaDroid-Shared")

    def test_migration_moves_loose_files_to_desktop(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            root = Path(td) / "NovaDroid-Shared"
            root.mkdir()
            (root / "loose.apk").write_bytes(b"x")
            s = SHARE.status()
            self.assertTrue((root / "Desktop" / "loose.apk").exists())
            self.assertFalse((root / "loose.apk").exists())
            self.assertTrue(any(f["name"] == "loose.apk" for f in s["desktop"]))

    def test_upload_goes_to_desktop(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            raw = b"hello-shared-world" * 100
            r = SHARE.upload("note.txt", base64.b64encode(raw).decode())
            self.assertTrue(r["ok"] and r["bytes"] == len(raw)
                            and r["folder"] == "Desktop")
            self.assertTrue((Path(SHARE.shared_dir()) / "Desktop" / "note.txt").exists())

    def test_upload_rejects(self):
        import tempfile
        # path traversal is neutralized to a bare file name inside Desktop/
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            r = SHARE.upload("../evil.txt", "eA==")
            self.assertTrue(r["ok"] and r["file"] == "evil.txt")
            self.assertTrue((Path(SHARE.shared_dir()) / "Desktop" / "evil.txt").exists())
            self.assertFalse(Path(td, "evil.txt").exists())
        self.assertFalse(SHARE.upload("x.bin", "!!!not-b64!!!")["ok"])

    def test_push_pull_need_device(self):
        self.assertFalse(SHARE.push(PATHS, "")["ok"])
        self.assertFalse(SHARE.pull(PATHS, "")["ok"])


class TestSync(unittest.TestCase):
    def test_autosync_needs_serial(self):
        r = SHARE.autosync_set(PATHS, "", True)
        self.assertFalse(r["ok"])

    def test_autosync_off(self):
        r = SHARE.autosync_set(PATHS, "", False)
        self.assertTrue(r["ok"] and r["autosync"] is False)

    def test_autosync_arms_when_offline(self):
        try:
            r = SHARE.autosync_set(PATHS, "emulator-9999", True, 5)
            self.assertTrue(r["ok"] and "waiting" in r["note"])
            st = SHARE.autosync_status()
            self.assertIn("emulator-9999", st["jobs"])
            self.assertTrue(st["jobs"]["emulator-9999"]["waiting"])
        finally:
            SHARE.autosync_set(PATHS, "emulator-9999", False)
        self.assertNotIn("emulator-9999", SHARE.autosync_status()["jobs"])

    def test_sync_now_offline_is_loud(self):
        r = SHARE.sync_now(PATHS, "emulator-9999")
        self.assertFalse(r["ok"])
        self.assertIn("offline", r["err"])

    def test_job_tick_telemetry(self):
        job: dict = {}
        SHARE._job_tick(PATHS, "emulator-9999", job)
        self.assertIn("last_run", job)
        self.assertTrue(job["waiting"])
        self.assertIn("waiting", job["last_result"])


class TestControl(unittest.TestCase):
    def test_guide_write_once(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            SHARE.status()
            g = Path(SHARE.shared_dir()) / SHARE.GUIDE_NAME
            self.assertTrue(g.exists())
            g.unlink()  # user deletes it…
            SHARE.status()  # …it must NOT resurrect (or deletes feel broken)
            self.assertFalse(g.exists())

    def test_delete_pc_confined(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            base = SHARE.desktop_dir()
            (base / "gone.txt").write_text("x", encoding="utf-8")
            r = SHARE.delete_pc(["gone.txt"])
            self.assertTrue(r["ok"] and not (base / "gone.txt").exists())
            evil = Path(td) / "outside.txt"
            evil.write_text("keep", encoding="utf-8")
            r2 = SHARE.delete_pc(["../outside.txt", "..", ""])
            self.assertFalse(r2["ok"])
            self.assertTrue(evil.exists(), "must never delete outside the folder")

    def test_delete_everywhere_covers_all_three(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            (SHARE.desktop_dir() / "x.txt").write_text("x", encoding="utf-8")
            (SHARE.device_mirror_dir() / "x.txt").write_text("x", encoding="utf-8")
            r = SHARE.delete_everywhere(PATHS, "", ["x.txt"])  # no device
            self.assertTrue(r["ok"] and r["deleted_pc"] == ["x.txt"])
            self.assertFalse((SHARE.desktop_dir() / "x.txt").exists())
            self.assertFalse((SHARE.device_mirror_dir() / "x.txt").exists())

    def test_delete_device_validation(self):
        self.assertFalse(SHARE.delete_device(PATHS, "", ["a"])["ok"])
        r = SHARE.delete_device(PATHS, "emulator-9999", ["../x", "a/b", ""])
        self.assertFalse(r["ok"])  # offline device; traversal never reaches adb

    def test_mirror_validation(self):
        self.assertFalse(SHARE.mirror(PATHS, "", "push")["ok"])
        r = SHARE.mirror(PATHS, "emulator-9999", "sideways")
        self.assertFalse(r["ok"])
        r2 = SHARE.mirror(PATHS, "emulator-9999", "push")
        self.assertFalse(r2["ok"] and "offline" in r2["err"])

    def test_dev_name_rules(self):
        self.assertEqual(SHARE._dev_name("My Video.mp4"), "My Video.mp4")  # spaces OK
        self.assertEqual(SHARE._dev_name("Put APKs, photos & files here.txt"),
                         "Put APKs, photos & files here.txt")  # & OK inside quotes
        for bad in ["a/b", "..", "", "it's", "a\nb"]:
            self.assertIsNone(SHARE._dev_name(bad), bad)
        self.assertEqual(SHARE._q("/sdcard/d/a b.txt"), "'/sdcard/d/a b.txt'")


class TestCompat(unittest.TestCase):
    def test_aapt2_present(self):
        self.assertIsNotNone(COMPAT.find_aapt2(PATHS.sdk), "SDK build-tools must ship aapt2")

    def test_missing_apk(self):
        r = COMPAT.apk_badging(PATHS, "C:\\nope\\no.apk")
        self.assertFalse(r["ok"])

    def test_verdict_pure_java(self):
        v = COMPAT.verdict({"minSdk": 21, "abis": []}, {"sdk": 35})
        self.assertEqual(v["level"], "OK")

    def test_verdict_native_x86(self):
        v = COMPAT.verdict({"minSdk": 21, "abis": ["x86_64"]},
                           {"sdk": 35, "abilist": "x86_64,x86,armeabi-v7a,arm64-v8a"})
        self.assertEqual(v["level"], "OK")

    def test_verdict_arm_translated(self):
        v = COMPAT.verdict({"minSdk": 24, "abis": ["arm64-v8a", "armeabi-v7a"]},
                           {"sdk": 35, "abilist": "x86_64,x86",
                            "bridge": "libndk_translation.so"})
        self.assertEqual(v["level"], "OK")
        self.assertIn("translation", v["reason"])

    def test_verdict_blocked_sdk(self):
        v = COMPAT.verdict({"minSdk": 36, "abis": []}, {"sdk": 35})
        self.assertEqual(v["level"], "BLOCKED")
        self.assertFalse(v["install_anyway"])


class TestDeleteBoth(unittest.TestCase):
    def test_pc_only_without_device(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            (SHARE.desktop_dir() / "bye.txt").write_text("x", encoding="utf-8")
            r = SHARE.delete_everywhere(PATHS, "", ["bye.txt"])
            self.assertTrue(r["ok"] and r["deleted_pc"] == ["bye.txt"])
            self.assertEqual(r["deleted_device"], [])

    def test_offline_device_still_deletes_pc(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            (SHARE.desktop_dir() / "bye2.txt").write_text("x", encoding="utf-8")
            (SHARE.device_mirror_dir() / "bye2.txt").write_text("x", encoding="utf-8")
            r = SHARE.delete_everywhere(PATHS, "emulator-9999", ["bye2.txt"])
            self.assertTrue(r["ok"] and r["deleted_pc"] == ["bye2.txt"])
            self.assertIn("offline", r["device_note"])
            self.assertFalse((SHARE.device_mirror_dir() / "bye2.txt").exists())

    def test_missing_file(self):
        r = SHARE.delete_everywhere(PATHS, "", ["nope.txt"])
        self.assertFalse(r["ok"])


class TestTidy(unittest.TestCase):
    def _root(self, td):
        SHARE.shared_dir()  # ensure layout
        return Path(td) / "NovaDroid-Shared"

    def test_stray_filed_to_desktop(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            root = self._root(td)
            (root / "stray.apk").write_bytes(b"data")
            SHARE.shared_dir()
            self.assertTrue((root / "Desktop" / "stray.apk").exists())
            self.assertFalse((root / "stray.apk").exists())

    def test_smaller_dupe_deleted_bigger_kept(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            root = self._root(td)
            (root / "Desktop" / "f.bin").write_bytes(b"0123456789")
            (root / "f.bin").write_bytes(b"0123")  # interrupted twin
            SHARE.shared_dir()
            self.assertFalse((root / "f.bin").exists())
            self.assertEqual((root / "Desktop" / "f.bin").stat().st_size, 10)

    def test_bigger_stray_kept_with_suffix(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            root = self._root(td)
            (root / "Desktop" / "f.bin").write_bytes(b"01")
            (root / "f.bin").write_bytes(b"0123456789")
            SHARE.shared_dir()
            self.assertTrue((root / "Desktop" / "f (root copy).bin").exists())
            self.assertEqual((root / "Desktop" / "f.bin").stat().st_size, 2)

    def test_guide_and_folders_untouched(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            root = self._root(td)
            SHARE.shared_dir()
            self.assertTrue((root / SHARE.GUIDE_NAME).exists())
            self.assertTrue((root / "Desktop").is_dir())
            self.assertTrue((root / "Device").is_dir())


if __name__ == "__main__":
    unittest.main()
