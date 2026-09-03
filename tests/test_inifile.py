"""Tests for core.inifile — the single, comment-aware ini upsert.

No device, no SDK needed. Verifies the contract that fixed three
divergent copies: comments are never rewritten, key order is preserved,
missing keys append, and nothing is ever duplicated.
Run: python -m unittest tests.test_inifile -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import inifile  # noqa: E402


class TestInifile(unittest.TestCase):
    def test_upsert_rewrites_in_place_keeps_order(self):
        p = Path("__t.ini")
        try:
            p.write_text("a=1\nb=2\nc=3\n", encoding="utf-8")
            out = inifile.upsert(p, {"b": "20"})
            self.assertEqual(out, "a=1\nb=20\nc=3\n")
        finally:
            p.unlink(missing_ok=True)

    def test_upsert_appends_missing_key(self):
        p = Path("__t.ini")
        try:
            p.write_text("a=1\n", encoding="utf-8")
            out = inifile.upsert(p, {"z": "9"})
            self.assertEqual(out, "a=1\nz=9\n")
        finally:
            p.unlink(missing_ok=True)

    def test_commented_key_is_preserved_not_matched(self):
        # The bug: one old copy rewrote commented-out `window.y`.
        p = Path("__t.ini")
        try:
            p.write_text("#window.y=999\nwindow.x=10\n", encoding="utf-8")
            out = inifile.upsert(p, {"window.y": "100"})
            # comment stays verbatim; the real key is appended (not a dup rewrite)
            self.assertIn("#window.y=999", out)
            self.assertIn("window.x=10", out)
            self.assertIn("window.y=100", out)
            # count only REAL (non-comment) `window.y=` assignments
            real = [ln for ln in out.splitlines()
                    if not ln.strip().startswith("#") and "window.y=" in ln]
            self.assertEqual(len(real), 1)
        finally:
            p.unlink(missing_ok=True)

    def test_no_duplicates_on_repeat_upsert(self):
        p = Path("__t.ini")
        try:
            p.write_text("k=v\n", encoding="utf-8")
            inifile.upsert(p, {"k": "a"})
            inifile.upsert(p, {"k": "b"})
            text = p.read_text(encoding="utf-8")
            self.assertEqual(text.count("k="), 1)
            self.assertIn("k=b", text)
        finally:
            p.unlink(missing_ok=True)

    def test_parse_ignores_comments_and_blanks(self):
        p = Path("__t.ini")
        try:
            p.write_text("# c\nx=1\n\ny=2\n", encoding="utf-8")
            d = inifile.parse(p)
            self.assertEqual(d, {"x": "1", "y": "2"})
        finally:
            p.unlink(missing_ok=True)

    def test_delete_removes_key_keeps_comment(self):
        p = Path("__t.ini")
        try:
            p.write_text("# keep\nwindow.x=1\nwindow.y=2\n", encoding="utf-8")
            out = inifile.delete(p, ["window.y"])
            self.assertIn("# keep", out)
            self.assertIn("window.x=1", out)
            self.assertNotIn("window.y=", out)
        finally:
            p.unlink(missing_ok=True)

    def test_trailing_newline_single(self):
        p = Path("__t.ini")
        try:
            p.write_text("a=1", encoding="utf-8")
            out = inifile.upsert(p, {"b": "2"})
            self.assertTrue(out.endswith("\n"))
            self.assertFalse(out.endswith("\n\n"))
        finally:
            p.unlink(missing_ok=True)

    def test_missing_file_parse_returns_empty(self):
        self.assertEqual(inifile.parse(Path("__does_not_exist_123.ini")), {})


if __name__ == "__main__":
    unittest.main()
