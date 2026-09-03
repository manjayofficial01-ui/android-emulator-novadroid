"""Stdlib unittests for NovaDroid core (no device needed). Run: python -m unittest discover -s tests -v"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import ToolPaths, DEFAULTS, find_sdk  # noqa: E402
from core.runner import run, parse_kv_block  # noqa: E402
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

    def test_kv(self):
        d = parse_kv_block("sdk: C:\\x\nversion: 1.0\n")
        self.assertEqual(d["version"], "1.0")


class TestMacros(unittest.TestCase):
    def test_save_list(self):
        r = ADB.macro_save("unittest-macro", [{"type": "tap", "x": 1, "y": 2}])
        self.assertTrue(r["ok"])
        lst = ADB.macro_list()
        self.assertTrue(any(m["name"] == "unittest-macro" for m in lst["macros"]))


if __name__ == "__main__":
    unittest.main()
