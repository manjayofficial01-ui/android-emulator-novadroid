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
    def test_status_creates_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            s = SHARE.status()
            self.assertTrue(s["ok"] and Path(s["dir"]).exists())
            self.assertEqual(s["device_dir"], "/sdcard/NovaDroid-Shared")

    def test_upload_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            raw = b"hello-shared-world" * 100
            r = SHARE.upload("note.txt", base64.b64encode(raw).decode())
            self.assertTrue(r["ok"] and r["bytes"] == len(raw))
            self.assertTrue((Path(SHARE.shared_dir()) / "note.txt").exists())

    def test_upload_rejects(self):
        import tempfile
        # path traversal is neutralized to a bare file name inside the folder
        with tempfile.TemporaryDirectory() as td, TempHome(td):
            r = SHARE.upload("../evil.txt", "eA==")
            self.assertTrue(r["ok"] and r["file"] == "evil.txt")
            self.assertTrue((Path(SHARE.shared_dir()) / "evil.txt").exists())
            self.assertFalse(Path(td, "evil.txt").exists())
        self.assertFalse(SHARE.upload("x.bin", "!!!not-b64!!!")["ok"])

    def test_push_pull_need_device(self):
        self.assertFalse(SHARE.push(PATHS, "")["ok"])
        self.assertFalse(SHARE.pull(PATHS, "")["ok"])


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


if __name__ == "__main__":
    unittest.main()
