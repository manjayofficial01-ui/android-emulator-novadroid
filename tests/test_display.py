"""Tests for tablet/display module — no device needed. Run: python -m unittest discover -s tests -v"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import ToolPaths  # noqa: E402
from core import display as DISP  # noqa: E402

FAKE = ToolPaths(sdk=None, android_cli="android", adb="C:\\fake\\adb.exe",
                 emulator=None, emulator_check=None, avdmanager=None)


class TestPresets(unittest.TestCase):
    def test_ids_unique_and_sane(self):
        allp = DISP.TABLET_PRESETS + DISP.PHONE_PRESETS
        ids = [p["id"] for p in allp]
        self.assertEqual(len(ids), len(set(ids)), "preset ids must be unique")
        for p in allp:
            self.assertIsNone(DISP._validate(p["w"], p["h"], p["dpi"]),
                              f"preset {p['id']} fails validation")

    def test_validation_rejects_garbage(self):
        for bad in [(0, 0, 0), (100, 100, 100), (99999, 100, 320),
                    (1920, 1080, 50), ("a", "b", "c")]:
            r = DISP.apply_display(FAKE, "emulator-5554", *bad)
            self.assertFalse(r["ok"], f"{bad} should be rejected")

    def test_orientation_rejects_unknown(self):
        r = DISP.set_orientation(FAKE, "emulator-5554", "sideways")
        self.assertFalse(r["ok"])

    def test_needs_device(self):
        r = DISP.apply_display(FAKE, "", 2560, 1600, 276)
        self.assertFalse(r["ok"])


class TestSaveToAvd(unittest.TestCase):
    def test_save_writes_lcd_keys(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("USERPROFILE")
            os.environ["USERPROFILE"] = td
            try:
                avd = Path(td) / ".android" / "avd" / "fake.avd"
                avd.mkdir(parents=True)
                (avd / "config.ini").write_text("hw.cpu.ncore=4\n", encoding="utf-8")
                r = DISP.save_to_avd("fake", 2560, 1600, 276)
                self.assertTrue(r["ok"])
                text = (avd / "config.ini").read_text(encoding="utf-8")
                for k in ("hw.lcd.width=2560", "hw.lcd.height=1600", "hw.lcd.density=276"):
                    self.assertIn(k, text)
                # idempotent overwrite
                r2 = DISP.save_to_avd("fake", 1920, 1200, 240)
                self.assertTrue(r2["ok"])
                text2 = (avd / "config.ini").read_text(encoding="utf-8")
                self.assertEqual(text2.count("hw.lcd.width="), 1)
            finally:
                if old is None:
                    del os.environ["USERPROFILE"]
                else:
                    os.environ["USERPROFILE"] = old

    def test_save_missing_avd(self):
        r = DISP.save_to_avd("__nope__", 1920, 1200, 240)
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
