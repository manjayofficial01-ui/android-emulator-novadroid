"""Tests for core.shared.export_to_desktop — the 'Export files to Desktop' feature.

No real device, no real Desktop writes. windows_desktop() is redirected to a
temp dir so the copy is exercised without polluting the user's actual Desktop.
Run: python -m unittest tests.test_shared_export -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import shared as SHARE  # noqa: E402
from core.config import ToolPaths  # noqa: E402

PATHS = ToolPaths.discover()


class TempHome:
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


class TestExportToDesktop(unittest.TestCase):
    def test_rejects_empty_selection(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(SHARE.export_to_desktop(PATHS, "", [], "pc")["ok"])

    def test_export_pc_copies_to_real_desktop(self):
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            desk = SHARE.desktop_dir()
            (desk / "shot.png").write_bytes(b"PNGDATA")
            real = Path(td) / "RealDesktop"
            real.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(SHARE, "windows_desktop", return_value=real):
                r = SHARE.export_to_desktop(PATHS, "", ["shot.png"], "pc")
            self.assertTrue(r["ok"])
            self.assertEqual(r["copied"], ["shot.png"])
            self.assertTrue((real / "shot.png").exists())
            # source is untouched (copy, never move/delete)
            self.assertTrue((desk / "shot.png").exists())

    def test_export_device_pulls_then_copies(self):
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            # pre-stage what the device mirror would hold after `pull`
            (SHARE.device_mirror_dir() / "clip.mp4").write_bytes(b"VIDEO")
            real = Path(td) / "RealDesktop"
            real.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(SHARE, "windows_desktop", return_value=real), \
                 mock.patch.object(SHARE, "_online", return_value=True), \
                 mock.patch.object(SHARE, "pull", return_value={"ok": True}):
                r = SHARE.export_to_desktop(PATHS, "emulator-5554",
                                            ["clip.mp4"], "device")
            self.assertTrue(r["ok"])
            self.assertEqual(r["copied"], ["clip.mp4"])
            self.assertTrue((real / "clip.mp4").exists())

    def test_export_device_offline_is_rejected(self):
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            real = Path(td) / "RealDesktop"
            real.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(SHARE, "windows_desktop", return_value=real), \
                 mock.patch.object(SHARE, "_online", return_value=False):
                r = SHARE.export_to_desktop(PATHS, "emulator-5554",
                                            ["x.png"], "device")
            self.assertFalse(r["ok"])
            self.assertIn("offline", r["err"])

    def test_export_conflict_renames_not_clobbers(self):
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            (SHARE.desktop_dir() / "dup.txt").write_bytes(b"new")
            real = Path(td) / "RealDesktop"
            real.mkdir(parents=True, exist_ok=True)
            (real / "dup.txt").write_bytes(b"OLD")  # already on the Desktop
            with mock.patch.object(SHARE, "windows_desktop", return_value=real):
                r = SHARE.export_to_desktop(PATHS, "", ["dup.txt"], "pc")
            self.assertTrue(r["ok"])
            self.assertEqual(r["copied"], ["dup (NovaDroid).txt"])
            self.assertTrue((real / "dup (NovaDroid).txt").exists())
            self.assertEqual((real / "dup.txt").read_bytes(), b"OLD")  # untouched

    def test_export_missing_source_file_fails_named(self):
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            real = Path(td) / "RealDesktop"
            real.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(SHARE, "windows_desktop", return_value=real):
                r = SHARE.export_to_desktop(PATHS, "", ["ghost.apk"], "pc")
            self.assertFalse(r["ok"])
            self.assertIn("ghost.apk", r["err"])


if __name__ == "__main__":
    unittest.main()
