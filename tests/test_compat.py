"""Tests for core.compat — the aapt2 version-sort fix.

The bug: lexicographic sort put "9.0.0" AFTER "36.0.0", so the newest
aapt2 (in 36.0.0) was skipped in favour of a decade-old one. The sort is
now numeric.
Run: python -m unittest tests.test_compat -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import compat  # noqa: E402


class TestVersionKey(unittest.TestCase):
    def test_numeric_not_lexicographic(self):
        # 9 must sort BELOW 36 (the old string bug)
        self.assertLess(compat._version_key("9.0.0"), compat._version_key("36.0.0"))

    def test_rc_suffix_sorts_below_release(self):
        # rc must rank strictly below its own release
        self.assertLess(compat._version_key("36.0.0-rc1"),
                        compat._version_key("36.0.0"))
        # and a release always beats an older pre-release
        self.assertLess(compat._version_key("34.0.0-rc3"),
                        compat._version_key("36.0.0"))

    def test_pads_to_four(self):
        self.assertEqual(compat._version_key("34"), (34, 0, 0, 0, 1))

    def test_find_aapt2_picks_newest(self):
        # Was `Path("__fakesdk")` in the CWD with a best-effort rmtree: if a
        # run ever died mid-way (or an indexer held the dir on Windows) the
        # leftover made the NEXT run fail with FileExistsError on mkdir.
        # tempfile is self-cleaning and immune to that.
        with tempfile.TemporaryDirectory() as td:
            sdk = Path(td) / "fakesdk"
            bt = sdk / "build-tools"
            for v in ("9.0.0", "36.0.0", "34.0.0-rc3", "30.0.3"):
                d = bt / v
                d.mkdir(parents=True)
                (d / "aapt2.exe").write_text("bin", encoding="utf-8")
            picked = compat.find_aapt2(sdk)
            self.assertEqual(Path(picked).parts[-3:],
                             ("build-tools", "36.0.0", "aapt2.exe"),
                             f"expected 36.0.0, got {picked}")

    def test_find_aapt2_none_without_sdk(self):
        self.assertIsNone(compat.find_aapt2(None))


if __name__ == "__main__":
    unittest.main()
