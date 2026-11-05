"""Tests for the AI-era layer (core/ai.py). No device needed."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ai as AI  # noqa: E402


# --------------------------------------------------------------------------
# logcat triage
# --------------------------------------------------------------------------
LOGCAT = [
    "09-09 13:37:20.123  1234  1234 D ActivityManager: normal chatter",
    "09-09 13:37:20.124  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main",
    "09-09 13:37:20.125  1234  1234 E AndroidRuntime: Process: com.example.app,"
    " PID: 1234",
    "09-09 13:37:20.126  1234  1234 E AndroidRuntime: java.lang.NullPointerException:"
    " attempt to invoke",
    "09-09 13:37:21.000  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main",
    "09-09 13:37:22.000  1234  1234 E AndroidRuntime: java.lang.NullPointerException:"
    " different object",
    "09-09 13:37:23.000    99   999 E ActivityManager: ANR in com.example.app",
    "09-09 13:37:24.000   777   777 E libc: Fatal signal 11 (SIGSEGV), code 1",
    "09-09 13:37:25.000  1234  1234 E art: OutOfMemoryError: Failed to allocate",
    "09-09 13:37:26.000  1234  1234 W System: low memory warning",
]


class TestTriage(unittest.TestCase):
    def test_finds_every_class(self):
        r = AI.triage_logcat(LOGCAT)
        kinds = {f["kind"] for f in r["findings"]}
        self.assertIn("crash", kinds)
        self.assertIn("anr", kinds)
        self.assertIn("native", kinds)
        self.assertIn("oom", kinds)
        self.assertIn("error", kinds)

    def test_deduplicates_by_exception(self):
        r = AI.triage_logcat(LOGCAT)
        npe = [f for f in r["findings"] if f["exception"] == "java.lang.NullPointerException"]
        self.assertEqual(len(npe), 1)
        self.assertEqual(npe[0]["count"], 2)

    def test_ranks_worst_first(self):
        r = AI.triage_logcat(LOGCAT)
        self.assertIn(r["findings"][0]["kind"], ("crash", "anr", "native", "oom"))
        self.assertTrue(r["findings"][0]["hint"])

    def test_clean_log_is_clean(self):
        r = AI.triage_logcat(["D App: hello", "I App: world"])
        self.assertEqual(r["issues"], 0)
        self.assertEqual(r["findings"], [])
        self.assertEqual(r["worst"], "none")
        self.assertIn("Clean", r["summary"])

    def test_empty_input_does_not_explode(self):
        r = AI.triage_logcat([])
        self.assertTrue(r["ok"])
        self.assertEqual(r["issues"], 0)
        r2 = AI.triage_logcat(None)
        self.assertTrue(r2["ok"])

    def test_signature_collapses_variables(self):
        a = AI._signature("09-09 13:37:20.123  1234  1234 E Tag: boom pid=999")
        b = AI._signature("09-10 01:02:03.456  5678  5678 E Tag: boom pid=111")
        self.assertEqual(a, b)
        self.assertNotIn("1234", a)


# --------------------------------------------------------------------------
# natural language
# --------------------------------------------------------------------------
class TestInterpret(unittest.TestCase):
    def test_orientation(self):
        p = AI.interpret("rotate to landscape")
        self.assertEqual(p["intent"], "display.orientation")
        self.assertEqual(p["args"]["mode"], "landscape")
        self.assertTrue(p["ok"])

    def test_portrait(self):
        self.assertEqual(AI.interpret("set portrait")["args"]["mode"], "portrait")

    def test_battery_with_level_and_status(self):
        p = AI.interpret("set battery to 20% charging")
        self.assertEqual(p["intent"], "device.battery")
        self.assertEqual(p["args"]["level"], 20)
        self.assertEqual(p["args"]["status"], "charging")

    def test_gps_coordinates(self):
        p = AI.interpret("set gps to 27.7172, 85.3240")
        self.assertEqual(p["intent"], "device.gps")
        self.assertEqual(p["args"]["lat"], 27.7172)
        self.assertEqual(p["args"]["lng"], 85.3240)

    def test_package_launch(self):
        p = AI.interpret("launch com.example.myapp")
        self.assertEqual(p["intent"], "app.launch")
        self.assertEqual(p["args"]["package"], "com.example.myapp")

    def test_install_path(self):
        p = AI.interpret(r"install C:\Users\me\app.apk")
        self.assertEqual(p["intent"], "apk.install")
        self.assertEqual(p["args"]["apk_path"], r"C:\Users\me\app.apk")

    def test_tap_coordinates(self):
        p = AI.interpret("tap 540, 1200")
        self.assertEqual(p["intent"], "input.tap")
        self.assertEqual((p["args"]["x"], p["args"]["y"]), (540, 1200))

    def test_swipe_with_arrow(self):
        p = AI.interpret("swipe 540,1400 -> 540,400")
        self.assertEqual(p["intent"], "input.swipe")
        self.assertEqual(p["args"]["y2"], 400)

    def test_long_press(self):
        p = AI.interpret("long press at 300 900")
        self.assertEqual(p["intent"], "input.long_press")
        self.assertEqual((p["args"]["x"], p["args"]["y"]), (300, 900))

    def test_nav_keys(self):
        self.assertEqual(AI.interpret("press home")["args"]["key"], "home")
        self.assertEqual(AI.interpret("go back")["args"]["key"], "back")

    def test_perf_diagnose(self):
        self.assertEqual(AI.interpret("why is it so laggy?")["intent"],
                         "perf.diagnose")

    def test_autotune(self):
        self.assertEqual(AI.interpret("optimise my avd")["intent"],
                         "perf.autotune")

    def test_ai_status(self):
        self.assertEqual(AI.interpret("is gemini nano available?")["intent"],
                         "ai.status")

    def test_triage(self):
        self.assertEqual(AI.interpret("why did it crash")["intent"], "ai.triage")

    def test_screenshot(self):
        self.assertEqual(AI.interpret("take a screenshot")["intent"],
                         "screen.capture")

    def test_incomplete_command_reports_missing_args(self):
        p = AI.interpret("tap somewhere")
        self.assertEqual(p["intent"], "input.tap")
        self.assertFalse(p["ok"])
        self.assertEqual(sorted(p["missing"]), ["x", "y"])
        self.assertIn("I need", p["reply"])

    def test_unknown_is_honest(self):
        p = AI.interpret("please make me a sandwich")
        self.assertFalse(p["ok"])
        self.assertIn("did not understand", p["err"])

    def test_empty(self):
        self.assertFalse(AI.interpret("")["ok"])
        self.assertFalse(AI.interpret(None)["ok"])

    def test_device_requirements_flagged(self):
        self.assertTrue(AI.interpret("rotate landscape")["needs_device"])
        self.assertFalse(AI.interpret("ai status")["needs_device"])

    def test_every_understood_intent_describes_itself(self):
        for phrase in ("rotate landscape", "battery 20%", "gps 27.7 85.3",
                       "launch com.a.b", "tap 1 2", "screenshot", "home",
                       "why is it laggy"):
            p = AI.interpret(phrase)
            self.assertTrue(p["reply"], phrase)
            self.assertGreater(p["confidence"], 0)


class TestAssist(unittest.TestCase):
    def test_refuses_device_command_with_no_device(self):
        r = AI.assist(mock.Mock(), "", "rotate to landscape")
        self.assertFalse(r["ok"])
        self.assertIn("needs a device", r["err"])

    def test_unknown_command_does_not_run(self):
        r = AI.assist(mock.Mock(), "emulator-5554", "sing a song")
        self.assertFalse(r["ok"])
        self.assertFalse(r.get("ran", False))

    def test_dispatches_tap(self):
        with mock.patch("core.adb.tap", return_value={"ok": True}) as t:
            r = AI.assist(mock.Mock(adb="adb"), "emulator-5554", "tap 10 20")
        self.assertTrue(r["ran"])
        self.assertTrue(t.called)
        self.assertEqual(t.call_args[0][2:], (10, 20))

    def test_dispatches_orientation(self):
        with mock.patch("core.display.set_orientation",
                        return_value={"ok": True}) as f:
            r = AI.assist(mock.Mock(adb="adb"), "emulator-5554",
                          "rotate landscape")
        self.assertTrue(r["ok"])
        self.assertEqual(f.call_args[0][2], "landscape")

    def test_exception_never_escapes(self):
        with mock.patch("core.adb.tap", side_effect=RuntimeError("boom")):
            r = AI.assist(mock.Mock(adb="adb"), "emulator-5554", "tap 1 2")
        self.assertFalse(r["ok"])
        self.assertIn("RuntimeError", r["err"])


if __name__ == "__main__":
    unittest.main()
