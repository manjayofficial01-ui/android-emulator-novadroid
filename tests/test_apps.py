"""Tests for core.apps — clipboard capability-awareness + boot_state parsing.

Both were real bugs:
  * clipboard_get/set reported success on images that print
    "No shell command implementation." and exit 0 — a silent no-op.
  * boot_state dropped empty getprop lines, shifting fields so
    sys.usb.state was read as bootanim and wait_boot never completed.
Run: python -m unittest tests.test_apps -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import apps as APPS  # noqa: E402
from core.config import ToolPaths  # noqa: E402

FAKE = ToolPaths(sdk=None, android_cli=None, adb="adb.exe",
                 emulator=None, emulator_check=None, avdmanager=None)


class TestClipboardOk(unittest.TestCase):
    def test_success(self):
        self.assertTrue(APPS._clipboard_ok({"ok": True, "out": "some text"}))

    def test_unsupported_string_in_out(self):
        r = {"ok": True, "out": "No shell command implementation.", "err": ""}
        self.assertFalse(APPS._clipboard_ok(r))

    def test_unsupported_in_err(self):
        r = {"ok": True, "out": "", "err": "unknown command: clipboard"}
        self.assertFalse(APPS._clipboard_ok(r))

    def test_failure_not_ok(self):
        self.assertFalse(APPS._clipboard_ok({"ok": False, "out": "x"}))


class TestClipboardCapabilityAware(unittest.TestCase):
    def test_get_reports_unsupported_honestly(self):
        with mock.patch.object(APPS, "run",
                                return_value={"ok": True,
                                              "out": "No shell command implementation.",
                                              "err": ""}):
            r = APPS.clipboard_get(FAKE, "emulator-5554")
        self.assertFalse(r["ok"])
        self.assertFalse(r["supported"])
        self.assertIn("Send", r["err"])  # tells the UI the fallback

    def test_get_success(self):
        with mock.patch.object(APPS, "run",
                                return_value={"ok": True,
                                              "out": "hello from device",
                                              "err": ""}):
            r = APPS.clipboard_get(FAKE, "emulator-5554")
        self.assertTrue(r["ok"])
        self.assertTrue(r["supported"])
        self.assertEqual(r["text"], "hello from device")

    def test_set_reports_unsupported_honestly(self):
        with mock.patch.object(APPS, "run",
                                return_value={"ok": True,
                                              "out": "No shell command implementation.",
                                              "err": ""}):
            r = APPS.clipboard_set(FAKE, "emulator-5554", "payload")
        self.assertFalse(r["ok"])
        self.assertFalse(r["supported"])


class TestBootState(unittest.TestCase):
    def test_padded_empty_lines_booted(self):
        # getprop emits an empty line for unset properties; padding keeps
        # fields aligned so bootanim (idx 2) is read correctly.
        with mock.patch.object(APPS, "run",
                                return_value={"ok": True, "out": "1\n\nstopped\n",
                                              "err": ""}):
            r = APPS.boot_state(FAKE, "emulator-5554")
        self.assertTrue(r["booted"])
        self.assertTrue(r["bootanim_stopped"])
        self.assertTrue(r["ready"])

    def test_not_booted_when_empty(self):
        with mock.patch.object(APPS, "run",
                                return_value={"ok": True, "out": "\n\n\n", "err": ""}):
            r = APPS.boot_state(FAKE, "emulator-5554")
        self.assertFalse(r["booted"])
        self.assertFalse(r["ready"])

    def test_extra_empty_lines_no_crash(self):
        # the exact failure mode: three blank lines between signals
        with mock.patch.object(APPS, "run",
                                return_value={"ok": True, "out": "1\n\n\nstopped\n",
                                              "err": ""}):
            r = APPS.boot_state(FAKE, "emulator-5554")
        self.assertTrue(r["booted"])
        self.assertTrue(r["ready"])


if __name__ == "__main__":
    unittest.main()
