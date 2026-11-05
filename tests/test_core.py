"""Stdlib unittests for NovaDroid core (no device needed). Run: python -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import ToolPaths, DEFAULTS  # noqa: E402
from core.runner import run  # noqa: E402
from core import adb as ADB  # noqa: E402


class TestConfig(unittest.TestCase):
    def test_discover(self):
        p = ToolPaths.discover()
        self.assertIsNotNone(p.android_cli, "`android` CLI should be on PATH")
        self.assertTrue(Path(p.adb).exists(), "adb should exist in SDK")
        self.assertTrue(Path(p.emulator).exists(), "emulator.exe should exist")

    def test_defaults_sane(self):
        self.assertEqual(DEFAULTS["cpus"], 4)
        self.assertGreaterEqual(DEFAULTS["ram_mb"], 4096)  # API 37 floor


class TestRunner(unittest.TestCase):
    def test_run_ok(self):
        r = run(["cmd", "/c", "echo hi"], timeout=10)
        self.assertTrue(r["ok"] and "hi" in r["out"])

    def test_run_missing(self):
        r = run(["__nope__binary__"], timeout=10)
        self.assertFalse(r["ok"] and r["code"] == 127)

    def test_run_refuses_empty_args(self):
        """An empty argv element used to spawn a half-built command."""
        self.assertFalse(run([])["ok"])
        self.assertFalse(run(["adb", ""])["ok"])
        self.assertFalse(run(["adb", None])["ok"])

    def test_png_detection(self):
        self.assertTrue(ADB._is_png(ADB.PNG_MAGIC + b"\x00" * 2000 + ADB.PNG_IEND))
        self.assertFalse(ADB._is_png(ADB.PNG_MAGIC + b"\x00" * 2000))  # truncated
        self.assertFalse(ADB._is_png(b"not a png at all" + b"\x00" * 2000))
        self.assertFalse(ADB._is_png(b""))


class TestMacros(unittest.TestCase):
    def test_save_list(self):
        r = ADB.macro_save("unittest-macro", [{"type": "tap", "x": 1, "y": 2}])
        self.assertTrue(r["ok"])
        lst = ADB.macro_list()
        self.assertTrue(any(m["name"] == "unittest-macro" for m in lst["macros"]))


if __name__ == "__main__":
    unittest.main()
