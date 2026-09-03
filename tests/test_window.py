"""Tests for window placement fixes — no device needed. Run: python -m unittest discover -s tests -v"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import window as WIN  # noqa: E402


class TestWindow(unittest.TestCase):
    def test_center_guarantees_title_bar(self):
        x, y = WIN.center_for()
        self.assertGreaterEqual(x, 0)
        self.assertEqual(y, 60)

    def test_move_ps_is_strict(self):
        # Guards so we never drag IDEs/browsers with similar words in titles.
        self.assertIn("qemu-system-x86_64", WIN._MOVE_PS)
        self.assertIn("-cmatch", WIN._MOVE_PS)  # case-SENSITIVE title match
        self.assertIn("^Android Emulator - ", WIN._MOVE_PS)
        self.assertNotIn("Antigravity", WIN._MOVE_PS)

    def test_reset_missing_avd(self):
        r = WIN.reset_saved_position("__nope__")
        self.assertFalse(r["ok"])

    def test_reset_and_state_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("USERPROFILE")
            os.environ["USERPROFILE"] = td
            try:
                avd = Path(td) / ".android" / "avd" / "fake.avd"
                avd.mkdir(parents=True)
                r = WIN.reset_saved_position("fake")
                self.assertTrue(r["ok"])
                st = WIN.window_state("fake")
                self.assertFalse(st["offscreen"])
                self.assertEqual(st["x"], str(r["x"]))
                # simulate the bug: saved while hanging off the top
                ini = avd / "emulator-user.ini"
                ini.write_text("window.x=500\nwindow.y=-400\n", encoding="utf-8")
                st2 = WIN.window_state("fake")
                self.assertTrue(st2["offscreen"], "negative y must flag offscreen")
                # fix heals it, idempotently
                WIN.reset_saved_position("fake")
                WIN.reset_saved_position("fake")
                text = ini.read_text(encoding="utf-8")
                self.assertEqual(text.count("window.y="), 1)
            finally:
                if old is None:
                    del os.environ["USERPROFILE"]
                else:
                    os.environ["USERPROFILE"] = old


if __name__ == "__main__":
    unittest.main()
