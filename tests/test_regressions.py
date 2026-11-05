"""Regression tests for bugs found in the v3.0.0 deep audit.

Every test here fails against the pre-audit code. They are grouped by the
defect they pin down so a future refactor cannot quietly reintroduce one.
"""
from __future__ import annotations

import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import adb as ADB  # noqa: E402
from core import avd as AVD  # noqa: E402
from core import host as HOST  # noqa: E402
from core.config import ToolPaths  # noqa: E402

NO_TOOLS = ToolPaths(sdk=None, android_cli=None, adb=None, emulator=None,
                     emulator_check=None, avdmanager=None)
#: Mirror of core.safe's C0+DEL class, kept here on purpose so the contract
#: test does not silently follow a change to the production constant.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _fake_run(stdout: str = "", ok: bool = True):
    return mock.Mock(side_effect=lambda *a, **k: {"ok": ok, "code": 0,
                                                  "out": stdout, "err": ""})


class TestListAvdsWithNoCli(unittest.TestCase):
    """`list_avds` referenced `r` before assignment when neither the `android`
    CLI nor emulator.exe was discoverable but AVDs existed on disk — an
    UnboundLocalError that 500'd every dashboard load."""

    def test_no_tools_but_avds_on_disk(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / ".android" / "avd" / "medium_phone.avd").mkdir(parents=True)
            (home / ".android" / "avd" / "medium_phone.ini").write_text(
                f"path={home / '.android' / 'avd' / 'medium_phone.avd'}\n")
            with mock.patch.object(AVD, "avd_home",
                                   lambda: home / ".android" / "avd"):
                r = AVD.list_avds(NO_TOOLS)
        self.assertTrue(r["ok"])
        self.assertEqual([a["name"] for a in r["avds"]], ["medium_phone"])

    def test_empty_avd_home_is_a_clean_error(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / ".android" / "avd").mkdir(parents=True)
            with mock.patch.object(AVD, "avd_home",
                                   lambda: home / ".android" / "avd"):
                r = AVD.list_avds(NO_TOOLS)
        self.assertFalse(r["ok"])
        self.assertEqual(r["avds"], [])


class TestStopAvdHonesty(unittest.TestCase):
    """`stop_avd` returned ok=True even when nothing was stopped."""

    def test_reports_failure_when_cli_fails_and_no_serial(self):
        paths = ToolPaths(sdk=None, android_cli="android", adb=None,
                          emulator=None, emulator_check=None, avdmanager=None)
        with mock.patch.object(AVD, "run", _fake_run("", ok=False)):
            with mock.patch.object(AVD, "serial_for_avd", lambda *a, **k: ""):
                r = AVD.stop_avd(paths, "medium_phone")
        self.assertFalse(r["ok"])

    def test_reports_success_when_cli_stops_it(self):
        paths = ToolPaths(sdk=None, android_cli="android", adb=None,
                          emulator=None, emulator_check=None, avdmanager=None)
        with mock.patch.object(AVD, "run", _fake_run("stopped", ok=True)):
            r = AVD.stop_avd(paths, "medium_phone")
        self.assertTrue(r["ok"])
        self.assertEqual(r["via"], "android-cli")


class TestHeadlessQemuDetection(unittest.TestCase):
    """`qemu-system-x86_64-headless.exe` was invisible to both the process
    scanner and the window fixer because both matched on exact names."""

    TASKLIST = (
        '"Image Name","PID","Session Name","Session#","Mem Usage"\r\n'
        '"qemu-system-x86_64-headless.exe","2112","Console","1","1,234,568 K"\r\n'
        '"emulator.exe","900","Console","1","100,000 K"\r\n'
        '"notepad.exe","42","Console","1","4,000 K"\r\n'
    )

    def test_headless_qemu_is_counted(self):
        with mock.patch.object(HOST, "run", _fake_run(self.TASKLIST)):
            with mock.patch.object(HOST, "IS_WINDOWS", True):
                procs = HOST.emulator_processes()
        names = [p["name"] for p in procs]
        self.assertIn("qemu-system-x86_64-headless.exe", names)
        self.assertIn("emulator.exe", names)
        self.assertNotIn("notepad.exe", names)
        # 1,234,568 K -> ~1205 MB
        self.assertGreater(procs[0]["mem_mb"], 1000)

    def test_window_script_matches_headless(self):
        from core import window as WIN
        ps = WIN._MOVE_PS.replace("__X__", "0").replace("__Y__", "0") \
                         .replace("__MAXW__", "100").replace("__MAXH__", "100")
        self.assertIn("qemu-system-*", ps)
        self.assertNotIn("-in 'emulator','qemu-system-x86_64'", ps)
        # the strict, case-sensitive title guard must survive
        self.assertIn("-cmatch '^Android Emulator - '", ps)


class TestScreenshotIntegrity(unittest.TestCase):
    def test_truncated_frame_is_rejected(self):
        head = ADB.PNG_MAGIC + b"\x00" * 4096
        with mock.patch.object(ADB, "run_bytes",
                               return_value={"ok": True, "out": head, "err": ""}):
            with mock.patch.object(ADB, "run",
                                   return_value={"ok": False, "out": "", "err": ""}):
                r = ADB.screenshot(mock.Mock(adb="adb", android_cli=None),
                                   "emulator-5554")
        self.assertFalse(r["ok"])


class TestGetpropsBatching(unittest.TestCase):
    """Property reads must be one round-trip, and must not shift fields when
    a property is unset."""

    def test_one_shell_call_for_many_props(self):
        calls = []
        paths = mock.Mock(adb="adb")

        def fake_run(cmd, timeout=0):
            calls.append(cmd)
            return {"ok": True, "code": 0, "err": "",
                    "out": "ro.product.model=sdk_gphone64\n"
                           "ro.build.version.sdk=36\n"
                           "ro.product.cpu.abilist=\n"}
        with mock.patch.object(ADB, "run", fake_run):
            props = ADB.getprops(paths, "emulator-5554",
                                 ["ro.product.model", "ro.build.version.sdk",
                                  "ro.product.cpu.abilist"])
        self.assertEqual(len(calls), 1, "must be a single adb round-trip")
        self.assertEqual(props["ro.product.model"], "sdk_gphone64")
        self.assertEqual(props["ro.build.version.sdk"], "36")
        # unset prop -> absent, NOT shifted into another key
        self.assertNotIn("ro.product.cpu.abilist", props)

    def test_no_device_returns_empty(self):
        self.assertEqual(ADB.getprops(mock.Mock(adb="adb"), "", ["a"]), {})


class TestSerialMapLocking(unittest.TestCase):
    def test_force_refresh_bypasses_ttl(self):
        AVD._SERIAL_MAP.clear()
        AVD._MAP_TS = 0.0
        paths = mock.Mock(adb="adb")
        with mock.patch.object(AVD, "run", _fake_run("List of devices attached\n")):
            self.assertEqual(AVD.serial_for_avd(paths, "x"), "")
        AVD._SERIAL_MAP.clear()
        AVD._MAP_TS = 0.0

    def test_serial_for_avd_refresh_kwarg(self):
        with mock.patch.object(AVD, "_serial_map",
                               side_effect=lambda p, force=False: (
                                   {"x": "emulator-5554"} if force else {})):
            self.assertEqual(AVD.serial_for_avd(mock.Mock(), "x", refresh=True),
                             "emulator-5554")


# ---------------------------------------------------------------------------
# v5.0.0 security regressions — every one of these fails against v4.
# ---------------------------------------------------------------------------
class _FakeReq:
    """Bare minimum an auth/helper function needs — no socket, no server."""
    def __init__(self, headers=None, ip="127.0.0.1", path="/api/avds"):
        self.headers = headers or {}
        self.client_address = (ip, 5555)
        self.command = "GET"
        self.path = path


class _BodyReq(_FakeReq):
    def __init__(self, headers, data=b""):
        super().__init__(headers)
        import io
        self.rfile = io.BytesIO(data)
        self.out = []

    def _json(self, obj, code=200):
        self.out.append((code, obj))


def _bare_handler(headers=None, path="/api/avds"):
    """A Handler instance without running __init__ (no socket needed)."""
    import novadroid as ND
    h = object.__new__(ND.Handler)
    h.headers = headers or {}
    h.path = path
    h.command = "GET"
    h._headers_sent = False
    h.out = []
    h._json = lambda obj, code=200: h.out.append((code, obj))
    h._unauthorized = lambda: h.out.append((401, {"ok": False}))
    h._guard = lambda q, p: True
    return h


class TestV5AuthRegressions(unittest.TestCase):
    """The v4 loopback exemption was a complete auth bypass over the tunnel."""

    def setUp(self):
        import novadroid as ND
        self.ND = ND
        self.token_patch = mock.patch.object(ND, "TOKEN", "s3cr3t")
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)
        with ND._TICKETS_LOCK:
            ND._TICKETS.clear()

    def test_loopback_no_longer_bypasses_the_token(self):
        h = _FakeReq(ip="127.0.0.1")
        self.assertFalse(self.ND._auth_ok(h, "/api/avds"))
        self.assertFalse(self.ND._auth_ok(h, "/api/avds"), "IPv6 loopback too")
        h2 = _FakeReq(ip="::1")
        self.assertFalse(self.ND._auth_ok(h2, "/api/avds"))

    def test_query_string_token_is_dead(self):
        h = _FakeReq()
        self.assertFalse(self.ND._auth_ok(h, "/api/screen",
                                          {"token": "s3cr3t"}))
        self.assertFalse(self.ND._auth_ok(h, "/api/avds",
                                          {"token": "s3cr3t"}))

    def test_bearer_token_is_the_only_general_auth(self):
        h = _FakeReq({"Authorization": "Bearer s3cr3t"})
        self.assertTrue(self.ND._auth_ok(h, "/api/avds"))
        self.assertFalse(self.ND._auth_ok(
            _FakeReq({"Authorization": "Bearer wrong"}), "/api/avds"))

    def test_ticket_scoped_to_three_binary_paths_only(self):
        t = self.ND._mint_ticket()
        h = _FakeReq()
        for p in ("/api/screen", "/api/screen/stream", "/api/record/download"):
            self.assertTrue(self.ND._auth_ok(h, p, {"ticket": t}), p)
        for p in ("/api/avds", "/api/info", "/api/session", "/api/health"):
            self.assertFalse(self.ND._auth_ok(h, p, {"ticket": t}), p)

    def test_expired_or_unknown_ticket_rejected(self):
        t = self.ND._mint_ticket()
        self.assertTrue(self.ND._ticket_ok(t))
        with self.ND._TICKETS_LOCK:
            self.ND._TICKETS[t] = time.time() - 1
        self.assertFalse(self.ND._ticket_ok(t))
        self.assertFalse(self.ND._ticket_ok("never-issued"))

    def test_local_open_mode_stays_open(self):
        with mock.patch.object(self.ND, "TOKEN", ""):
            self.assertTrue(self.ND._auth_ok(_FakeReq(), "/api/avds"))

    def test_cors_is_never_wildcard(self):
        with mock.patch.object(self.ND, "ALLOWED_ORIGINS",
                               frozenset({"https://good.example"})):
            h = _bare_handler({"Origin": "https://evil.example"})
            sent = []
            h.send_header = lambda k, v: sent.append((k, v))
            h._cors()
            self.assertEqual(sent, [], "an unlisted origin must get no ACAO")
            ok = _bare_handler({"Origin": "https://good.example"})
            sent2 = []
            ok.send_header = lambda k, v: sent2.append((k, v))
            ok._cors()
            self.assertIn(("Access-Control-Allow-Origin", "https://good.example"),
                          sent2)

    def test_cross_origin_and_same_origin_posts(self):
        bad = _bare_handler({"Origin": "https://evil.example",
                             "Host": "localhost:8080"})
        self.assertFalse(bad._origin_ok())
        good = _bare_handler({"Origin": "http://localhost:8080",
                              "Host": "localhost:8080"})
        self.assertTrue(good._origin_ok(), "same-origin POST must still work")
        with mock.patch.object(self.ND, "ALLOWED_ORIGINS",
                               frozenset({"https://dash.example"})):
            remote = _bare_handler({"Origin": "https://dash.example",
                                    "Host": "tunnel.example"})
            self.assertTrue(remote._origin_ok())

    def test_sec_fetch_cross_site_without_origin_is_refused(self):
        h = _bare_handler({"Sec-Fetch-Site": "cross-site"})
        self.assertFalse(h._origin_ok())

    def test_access_log_never_contains_credentials(self):
        import contextlib
        import io
        h = _bare_handler(path="/api/screen?serial=emulator-5554&ticket=LEAKME")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            h.log_message("%s", "200")
        self.assertNotIn("LEAKME", buf.getvalue())
        self.assertIn("/api/screen", buf.getvalue())

    def test_jread_enforces_content_type_and_size(self):
        h = _BodyReq({"Content-Length": "2", "Content-Type": "text/plain"}, b"{}")
        self.assertIsNone(self.ND.jread(h))
        self.assertEqual(h.out[0][0], 415)
        big = _BodyReq({"Content-Length": str(self.ND.MAX_BODY + 1),
                        "Content-Type": "application/json"})
        self.assertIsNone(self.ND.jread(big))
        self.assertEqual(big.out[0][0], 413)
        body = b'{"a": 1}'
        good = _BodyReq({"Content-Length": str(len(body)),
                         "Content-Type": "application/json; charset=utf-8"}, body)
        self.assertEqual(self.ND.jread(good), {"a": 1})
        self.assertEqual(good.out, [])

    def test_shell_and_gui_are_opt_in(self):
        for path, flag, needle in (("/api/shell", "ALLOW_SHELL", "--allow-shell"),
                                   ("/api/shared/reveal", "ALLOW_GUI", "--allow-gui")):
            with mock.patch.object(self.ND, flag, False), \
                 mock.patch.object(self.ND, "jread", return_value={}):
                h = _bare_handler(path=path)
                h._post()
                self.assertEqual(h.out[0][0], 403, path)
                self.assertIn(needle, h.out[0][1]["err"])

    def test_cached_single_flight(self):
        import threading
        key = "test-single-flight"
        self.ND._TTL_CACHE.pop(key, None)
        calls = []
        started, release = threading.Event(), threading.Event()

        def fn():
            calls.append(1)
            started.set()
            release.wait(3)
            return {"ok": True}

        threads = [threading.Thread(target=lambda: self.ND._cached(key, 60, fn))
                   for _ in range(5)]
        for t in threads:
            t.start()
        self.assertTrue(started.wait(3))
        release.set()
        for t in threads:
            t.join(5)
        self.assertEqual(len(calls), 1, "concurrent misses must share one call")
        self.ND._TTL_CACHE.pop(key, None)


class TestV5InputRegressions(unittest.TestCase):
    def test_sh_quote_survives_posix_parsing(self):
        import shlex
        from core import safe
        # Control-free values round-trip byte-for-byte through /system/bin/sh,
        # including shell metacharacters, quotes, expansions and whitespace.
        for s in ("a'; rm -rf /sdcard; '", "$(id) `whoami`", "a b c",
                  "it's a 'test'", "", "semi;pipe|amp&redir>",
                  "back\\slash", "glob*[abc]?", "a\tb"):
            expected = s if not safe.has_control(s) else _CONTROL_RE.sub("", s)
            self.assertEqual(shlex.split(safe.sh_quote(s)), [expected], repr(s))

    def test_sh_quote_strips_controls_defensively(self):
        """Callers reject controls first; quoting strips them as a second layer.

        A tab/newline inside a single-quoted word is legal POSIX but adb joins
        argv with spaces before handing it to `sh -c`, so it is removed rather
        than silently changing which file/URL the device sees.
        """
        import shlex
        from core import safe
        for s in ("a\nrm -rf /sdcard", "a\rb", "nul\x00byte", "esc\x1b[2Jx"):
            quoted = safe.sh_quote(s)
            self.assertFalse(safe.has_control(shlex.split(quoted)[0]), repr(s))
            self.assertEqual(shlex.split(quoted),
                             [_CONTROL_RE.sub("", s)], repr(s))

    def test_control_chars_include_newlines_but_text_allows_them(self):
        from core import safe
        self.assertTrue(safe.has_control("a\nb"))
        self.assertTrue(safe.has_control("a\rb"))
        self.assertFalse(safe.has_text_control("line1\nline2\tend"))
        self.assertTrue(safe.has_text_control("nul\x00byte"))

    def test_device_path_quoting_is_not_naive(self):
        import shlex
        from core import shared as SHARE
        p = "/sdcard/NovaDroid-Shared/a'; reboot; '.mp4"
        self.assertEqual(shlex.split(SHARE._q(p)), [p])

    def test_download_path_is_confined(self):
        from core import apps as APPS, safe
        self.assertIsNone(safe.confine(APPS.REC_DIR, "../secrets.txt"))
        self.assertIsNone(safe.confine(APPS.REC_DIR, "/etc/passwd"))
        self.assertIsNone(safe.confine(APPS.REC_DIR, "..\\secrets.txt"))

    def test_apk_analyze_rejects_arbitrary_host_paths(self):
        from core import compat as COMPAT
        from core.config import ToolPaths
        paths = ToolPaths(sdk=None, android_cli=None, adb=None, emulator=None,
                          emulator_check=None, avdmanager=None)
        r = COMPAT.apk_badging(paths, "C:\\Windows\\System32\\config\\SAM")
        self.assertFalse(r["ok"])
        self.assertNotIn("Windows", r["err"])

    def test_memory_suffixes(self):
        from core import safe
        self.assertEqual(safe.memory_mb("6G"), 6144)
        self.assertEqual(safe.memory_mb("4096M"), 4096)
        self.assertEqual(safe.memory_mb("4194304K"), 4096)
        self.assertEqual(safe.memory_mb("4096"), 4096)
        self.assertEqual(safe.memory_mb("32M"), 32)
        self.assertEqual(safe.memory_mb("garbage"), 0)


if __name__ == "__main__":
    unittest.main()
