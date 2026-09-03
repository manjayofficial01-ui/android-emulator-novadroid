"""Tests for core.adb — fixes verified WITHOUT a live device (mocked run).

Covers:
  * battery numeric-status fix (BATTERY_STATUS codes + "Bad value" detection)
  * geo_fix range validation
  * screenshot exec-out-first pipeline
  * macro step validation (drops malformed instead of KeyError)
Run: python -m unittest tests.test_adb -v
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import adb as ADB  # noqa: E402
from core.config import ToolPaths  # noqa: E402

FAKE = ToolPaths(sdk=None, android_cli=None, adb="adb.exe",
                 emulator=None, emulator_check=None, avdmanager=None)


class TestBatteryNumeric(unittest.TestCase):
    def test_charging_maps_to_numeric_code(self):
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return {"ok": True, "out": "", "err": ""}
        with mock.patch.object(ADB, "run", fake_run):
            r = ADB.battery(FAKE, "emulator-5554", level=42, status="charging")
        self.assertTrue(r["ok"])
        self.assertEqual(r["status_code"], 2)  # BATTERY_STATUS["charging"]
        # the SET script must carry the numeric code, not the word "charging"
        set_script = " ".join(calls[0])
        self.assertIn("dumpsys battery set status 2", set_script)
        self.assertNotIn("dumpsys battery set status charging", set_script)

    def test_bad_value_is_detected_not_swallowed(self):
        # Old bug: device replied "Bad value: charging" but code reported success.
        def fake_run(cmd, **kw):
            return {"ok": True, "out": "Bad value: charging", "err": ""}
        with mock.patch.object(ADB, "run", fake_run):
            r = ADB.battery(FAKE, "emulator-5554", level=80, status="charging")
        self.assertFalse(r["ok"])
        self.assertIn("Bad value", r["err"])

    def test_unknown_status_defaults_safely(self):
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return {"ok": True, "out": "", "err": ""}
        with mock.patch.object(ADB, "run", fake_run):
            r = ADB.battery(FAKE, "emulator-5554", level=50, status="weird")
        self.assertTrue(r["ok"])
        # falls back to not-charging (4) rather than crashing
        self.assertEqual(r["status_code"], 4)

    def test_level_clamped(self):
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return {"ok": True, "out": "", "err": ""}
        with mock.patch.object(ADB, "run", fake_run):
            ADB.battery(FAKE, "emulator-5554", level=999, status="full")
        set_script = " ".join(calls[0])
        self.assertIn("dumpsys battery set level 100", set_script)


class TestGeoFix(unittest.TestCase):
    def test_rejects_out_of_range(self):
        with mock.patch.object(ADB, "run") as m:
            r = ADB.geo_fix(FAKE, "emulator-5554", lat=200, lng=0)
        self.assertFalse(r["ok"])
        m.assert_not_called()

    def test_valid_calls_emu_geo_fix(self):
        captured = {}
        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return {"ok": True, "out": "", "err": ""}
        with mock.patch.object(ADB, "run", fake_run):
            r = ADB.geo_fix(FAKE, "emulator-5554", lat=40.0, lng=-70.0)
        self.assertTrue(r["ok"])
        # emulator console takes longitude FIRST
        self.assertEqual(captured["cmd"][-2:], ["-70.0", "40.0"])


class TestScreenshot(unittest.TestCase):
    def test_exec_out_first(self):
        png = ADB.PNG_MAGIC + b"\x00" * 2000
        with mock.patch.object(ADB, "run_bytes",
                               return_value={"ok": True, "out": png, "err": ""}) as rb:
            r = ADB.screenshot(FAKE, "emulator-5554")
        self.assertTrue(r["ok"])
        self.assertEqual(r["via"], "exec-out")
        self.assertIn("png_b64", r)
        # the fast path used exec-out, not screencap+pull
        self.assertIn("exec-out", rb.call_args[0][0])

    def test_falls_through_when_no_png(self):
        with mock.patch.object(ADB, "run_bytes",
                               return_value={"ok": True, "out": b"garbage", "err": ""}):
            with mock.patch.object(ADB, "run",
                                   return_value={"ok": True, "out": "", "err": ""}):
                r = ADB.screenshot(FAKE, "emulator-5554")
        self.assertFalse(r["ok"])


class TestMacroValidation(unittest.TestCase):
    def test_drops_malformed(self):
        good, bad = ADB._validate_steps([
            {"type": "tap", "x": 10, "y": 20},
            {"type": "bogus"},
            {"type": "text"},            # empty text
            "not an object",
            {"type": "sleep", "s": 2},
            {"type": "key", "code": 3},
        ])
        self.assertEqual(len(good), 3)
        self.assertEqual(len(bad), 3)
        types = {g["type"] for g in good}
        self.assertEqual(types, {"tap", "sleep", "key"})

    def test_coordinates_clamped(self):
        good, _ = ADB._validate_steps([{"type": "tap", "x": 999999, "y": -5}])
        self.assertEqual(good[0]["x"], 20000)
        self.assertEqual(good[0]["y"], 0)

    def test_save_list_delete_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(ADB, "MACRO_DIR", Path(td)):
                r = ADB.macro_save("ut-macro", [{"type": "tap", "x": 1, "y": 2}])
                self.assertTrue(r["ok"])
                lst = ADB.macro_list()
                self.assertTrue(any(m["name"] == "ut-macro" for m in lst["macros"]))
                self.assertTrue(ADB.macro_delete("ut-macro")["ok"])
                self.assertFalse(ADB.macro_list()["macros"])


if __name__ == "__main__":
    unittest.main()
