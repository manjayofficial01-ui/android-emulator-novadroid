"""v5.1.0 tests — every security fix, bug fix and feature added in this
release, pinned. Every test here would fail against the v5.0.0 code.

Groups: security hardening (input_text quoting, DNS rebinding, raw-flag
denylist, export/upload blocklist, health trim, non-ASCII tokens, log leak),
bug fixes (ports reverse, wipe honesty, engine unknowns, inifile dedupe,
macro save, backup stamp, rename rollback, sms/sensor/display), and the new
features (smart archive, scrcpy wrapper, GPU preference, hybrid CPU, WoA
guard, agent skill file, canary channel).
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import accel as ACCEL  # noqa: E402
from core import agentskill as AGENTSKILL  # noqa: E402
from core import apps as APPS  # noqa: E402
from core import avd as AVD  # noqa: E402
from core import adb as ADB  # noqa: E402
from core import compat as COMPAT  # noqa: E402
from core import display as DISP  # noqa: E402
from core import engine as ENGINE  # noqa: E402
from core import gpu as GPU  # noqa: E402
from core import inifile  # noqa: E402
from core import perf as PERF  # noqa: E402
from core import ports as PORTS  # noqa: E402
from core import shared as SHARE  # noqa: E402
from core import stream as STREAM  # noqa: E402
from core.config import ToolPaths  # noqa: E402

FAKE = ToolPaths(sdk=None, android_cli=None, adb="adb.exe",
                 emulator=None, emulator_check=None, avdmanager=None)


def _fake_run(stdout: str = "", ok: bool = True, err: str = ""):
    return mock.Mock(side_effect=lambda *a, **k: {"ok": ok, "code": 0,
                                                  "out": stdout, "err": err})


def _capturer(box: dict, key: str = "cmd"):
    def fake_run(cmd, timeout=0):
        box[key] = cmd
        return {"ok": True, "code": 0, "out": "", "err": ""}
    return fake_run


class _FakeReq:
    def __init__(self, headers=None, ip="127.0.0.1", path="/api/avds"):
        self.headers = headers or {}
        self.client_address = (ip, 5555)
        self.command = "GET"
        self.path = path


def _bare_handler(headers=None, path="/api/avds"):
    import novadroid as ND
    h = object.__new__(ND.Handler)
    h.headers = headers or {}
    h.path = path
    h.command = "GET"
    h._headers_sent = False
    h.out = []
    h._json = lambda obj, code=200: h.out.append((code, obj))
    h._unauthorized = lambda: h.out.append((401, {"ok": False}))
    return h


# ---------------------------------------------------------------------------
# Security: input_text quoting (P1 — device shell injection)
# ---------------------------------------------------------------------------
class TestInputTextQuoting(unittest.TestCase):
    def test_payload_is_quoted_shell_word(self):
        box = {}
        with mock.patch.object(ADB, "run", _capturer(box)):
            ADB.input_text(FAKE, "emulator-5554", "x; reboot")
        self.assertEqual(box["cmd"][-1], '"x; reboot"')

    def test_quote_and_dollar_escaped_inside_quotes(self):
        box = {}
        with mock.patch.object(ADB, "run", _capturer(box)):
            ADB.input_text(FAKE, "emulator-5554", 'a"b$c`d')
        self.assertEqual(box["cmd"][-1], '"a\\"b\\$c\\`d"')

    def test_percent_survives_once(self):
        box = {}
        with mock.patch.object(ADB, "run", _capturer(box)):
            ADB.input_text(FAKE, "emulator-5554", "50% done")
        self.assertIn("50% done", box["cmd"][-1])
        self.assertNotIn("%%", box["cmd"][-1])


# ---------------------------------------------------------------------------
# Security: DNS rebinding / Host hardening (P1)
# ---------------------------------------------------------------------------
class TestRebindingHardening(unittest.TestCase):
    def setUp(self):
        import novadroid as ND
        self.ND = ND
        self.tok = mock.patch.object(ND, "TOKEN", "")
        self.tok.start()
        self.addCleanup(self.tok.stop)
        self.origins = mock.patch.object(ND, "ALLOWED_ORIGINS", frozenset())
        self.origins.start()
        self.addCleanup(self.origins.stop)

    def test_rebinding_origin_host_fallback_is_refused(self):
        # Origin == Host is NOT enough when the Host is a foreign domain —
        # v5.0.0 accepted exactly this and lost the whole tokenless API.
        h = _bare_handler({"Origin": "http://rebind.evil.com:8080",
                           "Host": "rebind.evil.com:8080"})
        self.assertFalse(h._origin_ok())

    def test_same_origin_localhost_still_works(self):
        h = _bare_handler({"Origin": "http://localhost:8080",
                           "Host": "localhost:8080"})
        self.assertTrue(h._origin_ok())

    def test_allowed_host_fallback_still_works(self):
        import novadroid as ND
        with mock.patch.object(ND, "ALLOWED_HOSTS", frozenset({"tunnel.example"})):
            h = _bare_handler({"Origin": "http://tunnel.example",
                               "Host": "tunnel.example"})
            self.assertTrue(h._origin_ok())

    def test_tokenless_host_check_refuses_foreign_host(self):
        h = _bare_handler({"Host": "rebind.evil.com:8080"})
        self.assertFalse(h._host_ok())

    def test_tokenless_host_check_accepts_loopback(self):
        for host in ("localhost:8080", "127.0.0.1:8080", "[::1]:8080"):
            h = _bare_handler({"Host": host})
            self.assertTrue(h._host_ok(), host)

    def test_tokened_host_check_stays_permissive_without_strict(self):
        import novadroid as ND
        with mock.patch.object(ND, "TOKEN", "s3cr3t"):
            h = _bare_handler({"Host": "tunnel.example"})
            self.assertTrue(h._host_ok(), "Bearer is the gate when a token is set")

    def test_non_ascii_bearer_is_401_not_500(self):
        import novadroid as ND
        with mock.patch.object(ND, "TOKEN", "s3cr3t"):
            h = _FakeReq({"Authorization": "Bearer tóken"})
            self.assertFalse(ND._auth_ok(h, "/api/avds"))  # must not raise


# ---------------------------------------------------------------------------
# Security: raw-flag denylist, export/upload blocklist, health, log
# ---------------------------------------------------------------------------
class TestRawFlagDenylist(unittest.TestCase):
    def test_qemu_passthrough_refused(self):
        with mock.patch.object(AVD, "subprocess") as sp, \
                mock.patch.object(AVD, "paths_ok", True, create=True):
            r = AVD.start_avd_raw(
                ToolPaths(sdk=None, android_cli=None, adb=None,
                          emulator="emulator.exe", emulator_check=None,
                          avdmanager=None),
                "avd_ok", ["-qemu", "-hda", "file=C:/Users/me/secret.img"])
        self.assertFalse(r["ok"])
        self.assertIn("-qemu", r["err"])
        sp.Popen.assert_not_called()

    def test_sdcard_mount_refused_but_gpu_host_allowed(self):
        EMU = ToolPaths(sdk=None, android_cli=None, adb=None,
                        emulator="emulator.exe", emulator_check=None,
                        avdmanager=None)
        with mock.patch.object(AVD.subprocess, "Popen",
                               mock.Mock(return_value=mock.Mock(pid=7))):
            r = AVD.start_avd_raw(EMU, "avd_ok", ["-gpu", "host", "-cores", "4"])
            self.assertTrue(r["ok"])
            r2 = AVD.start_avd_raw(EMU, "avd_ok", ["-sdcard", "C:/x.img"])
        self.assertFalse(r2["ok"])

    def test_affinity_is_accepted_and_reported(self):
        EMU = ToolPaths(sdk=None, android_cli=None, adb=None,
                        emulator="emulator.exe", emulator_check=None,
                        avdmanager=None)
        proc = mock.Mock(pid=9)
        with mock.patch.object(AVD.subprocess, "Popen", mock.Mock(return_value=proc)):
            r = AVD.start_avd_raw(EMU, "avd_ok", ["-gpu", "host"], affinity=0b11)
        self.assertTrue(r["ok"])
        self.assertIn("affinity_pinned", r)


class TestExportUploadBlocklist(unittest.TestCase):
    def test_upload_refuses_lnk_hta(self):
        for name in ("Cleanup Tool.lnk", "update.hta", "run.vbs", "x.wsf"):
            r = SHARE.upload(name, "aGk=")
            self.assertFalse(r["ok"], name)

    def test_upload_still_accepts_normal_files(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(SHARE, "desktop_dir", lambda: Path(td)):
                r = SHARE.upload("photo.png", "aGk=")
        self.assertTrue(r["ok"])

    def test_export_refuses_executables(self):
        with tempfile.TemporaryDirectory() as td:
            stage = Path(td) / "stage"
            stage.mkdir()
            (stage / "tool.exe").write_bytes(b"MZ")
            with mock.patch.object(SHARE, "desktop_dir", lambda: stage), \
                    mock.patch.object(SHARE, "windows_desktop",
                                      lambda: Path(td) / "desk"), \
                    mock.patch.object(SHARE, "device_mirror_dir",
                                      lambda: stage):
                r = SHARE.export_to_desktop(FAKE, "emulator-5554",
                                            ["tool.exe"], source="pc")
        self.assertFalse(r["ok"])
        self.assertIn("executable", r.get("err", ""))
        self.assertEqual(r["copied"], [])

    def test_export_never_clobbers_earlier_novadroid_copy(self):
        with tempfile.TemporaryDirectory() as td:
            stage = Path(td) / "stage"
            desk = Path(td) / "desk"
            stage.mkdir()
            desk.mkdir()
            (stage / "a.txt").write_text("one")
            (desk / "a.txt").write_text("old")
            (desk / "a (NovaDroid).txt").write_text("old2")
            with mock.patch.object(SHARE, "desktop_dir", lambda: stage), \
                    mock.patch.object(SHARE, "windows_desktop", lambda: desk), \
                    mock.patch.object(SHARE, "device_mirror_dir", lambda: stage):
                r = SHARE.export_to_desktop(FAKE, "emulator-5554",
                                            ["a.txt"], source="pc")
            self.assertTrue(r["ok"])
            self.assertEqual(r["copied"], ["a (NovaDroid) 2.txt"])
            self.assertEqual((desk / "a.txt").read_text(), "old")
            self.assertEqual((desk / "a (NovaDroid).txt").read_text(), "old2")


class TestHealthAndLogging(unittest.TestCase):
    def test_health_hides_details_when_unauthenticated(self):
        import novadroid as ND
        with mock.patch.object(ND, "TOKEN", "s3cr3t"), \
                mock.patch.object(ND, "ALLOW_SHELL", True):
            h = _bare_handler(path="/api/health")
            ND.Handler._get(h)
            code, obj = h.out[0]
            self.assertEqual(code, 200)
            self.assertNotIn("features", obj)
            self.assertNotIn("version", obj)
            self.assertEqual(obj["auth"], "token")
            # ...and the full detail is still there for an authed caller
            h2 = _bare_handler({"Authorization": "Bearer s3cr3t"},
                               path="/api/health")
            ND.Handler._get(h2)
            code2, obj2 = h2.out[0]
            self.assertEqual(code2, 200)
            self.assertEqual(obj2["features"]["shell"], True)

    def test_log_request_never_prints_the_ticket(self):
        h = _bare_handler(path="/api/screen?serial=emulator-5554&ticket=LEAKME2")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            h.log_request(200)
        self.assertNotIn("LEAKME2", buf.getvalue())
        self.assertIn("/api/screen", buf.getvalue())

    def test_security_headers_on_html(self):
        import novadroid as ND
        sent = []
        h = object.__new__(ND.Handler)
        h._headers_buffer = [b"Content-Type: text/html; charset=utf-8\r\n"]
        h.send_header = lambda k, v: sent.append((k, v))
        with mock.patch.object(ND.SimpleHTTPRequestHandler, "end_headers"):
            h.end_headers()
        keys = {k for k, _ in sent}
        self.assertIn("Content-Security-Policy", keys)
        self.assertIn("X-Frame-Options", keys)
        self.assertIn("Referrer-Policy", keys)

    def test_no_csp_on_json(self):
        import novadroid as ND
        sent = []
        h = object.__new__(ND.Handler)
        h._headers_buffer = [b"Content-Type: application/json\r\n"]
        h.send_header = lambda k, v: sent.append((k, v))
        with mock.patch.object(ND.SimpleHTTPRequestHandler, "end_headers"):
            h.end_headers()
        self.assertNotIn("Content-Security-Policy", {k for k, _ in sent})
        self.assertIn("X-Frame-Options", {k for k, _ in sent})


# ---------------------------------------------------------------------------
# Bugs: ports
# ---------------------------------------------------------------------------
class TestPortsReverseFix(unittest.TestCase):
    def test_reverse_adds_device_side_first(self):
        # adb reverse REMOTE LOCAL — v5.0.0 built an inverted tunnel.
        calls = []
        with mock.patch.object(PORTS, "run",
                               mock.Mock(side_effect=lambda c, timeout=0:
                                         (calls.append(c),
                                          {"ok": True, "code": 0, "out": "",
                                           "err": ""})[1])):
            r = PORTS.add(FAKE, "emulator-5554", "reverse", 3000, 8080)
        self.assertTrue(r["ok"])
        self.assertEqual(calls[0][-3:], ["reverse", "tcp:8080", "tcp:3000"])

    def test_reverse_list_parses_paren_reverse_rows(self):
        out = "(reverse) tcp:6000 tcp:5000\n"
        with mock.patch.object(PORTS, "run", _fake_run(out)):
            r = PORTS.reverses(FAKE, "emulator-5554")
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["rules"][0]["local"], "tcp:5000")   # PC side
        self.assertEqual(r["rules"][0]["remote"], "tcp:6000")  # device side
        self.assertEqual(r["rules"][0]["serial"], "emulator-5554")

    def test_reverse_remove_targets_device_side_port(self):
        calls = []
        with mock.patch.object(PORTS, "run",
                               mock.Mock(side_effect=lambda c, timeout=0:
                                         (calls.append(c),
                                          {"ok": True, "code": 0, "out": "",
                                           "err": ""})[1])):
            r = PORTS.remove(FAKE, "emulator-5554", "reverse", local=3000,
                             remote=8080)
        self.assertTrue(r["ok"])
        self.assertEqual(calls[0][-2:], ["--remove", "tcp:8080"])

    def test_out_of_range_port_is_rejected_not_clamped(self):
        self.assertEqual(PORTS._port(70000), 0)
        self.assertEqual(PORTS._port(-4), 0)
        r = PORTS.add(FAKE, "emulator-5554", "forward", 70000, 80)
        self.assertFalse(r["ok"])

    def test_invalid_serial_refused(self):
        r = PORTS.add(FAKE, "bad serial; reboot", "forward", 80, 80)
        self.assertFalse(r["ok"])


# ---------------------------------------------------------------------------
# Bugs: AVD lifecycle honesty
# ---------------------------------------------------------------------------
class TestWipeDataHonesty(unittest.TestCase):
    def test_refuses_running_avd(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "x.avd"
            root.mkdir()
            (root / "userdata-qemu.img").write_bytes(b"q")
            with mock.patch.object(AVD, "avd_dir", lambda n: root), \
                    mock.patch.object(AVD, "serial_for_avd",
                                      lambda *a, **k: "emulator-5554"):
                r = AVD.wipe_data(FAKE, "x")
                self.assertFalse(r["ok"])
                self.assertTrue((root / "userdata-qemu.img").exists())

    def test_reports_locked_files_instead_of_ok_true(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "x.avd"
            root.mkdir()
            locked = root / "userdata-qemu.img"
            locked.write_bytes(b"q")
            real_unlink = Path.unlink

            def deny(self, *a, **k):
                if self == locked:
                    raise PermissionError("locked by qemu")
                return real_unlink(self, *a, **k)

            with mock.patch.object(AVD, "avd_dir", lambda n: root), \
                    mock.patch.object(AVD, "serial_for_avd",
                                      lambda *a, **k: ""), \
                    mock.patch.object(Path, "unlink", deny):
                r = AVD.wipe_data(FAKE, "x")
                self.assertFalse(r["ok"])
                self.assertIn("userdata-qemu.img", r.get("locked", []))

    def test_already_clean_is_ok_with_note(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "x.avd"
            root.mkdir()
            with mock.patch.object(AVD, "avd_dir", lambda n: root), \
                    mock.patch.object(AVD, "serial_for_avd",
                                      lambda *a, **k: ""):
                r = AVD.wipe_data(FAKE, "x")
                self.assertTrue(r["ok"])
                self.assertEqual(r["removed"], [])
                self.assertIn("clean", r["note"])


class TestStopAvdWithoutCli(unittest.TestCase):
    def test_cli_less_machine_still_stops_via_emu_kill(self):
        paths = ToolPaths(sdk=None, android_cli=None, adb="adb.exe",
                          emulator=None, emulator_check=None, avdmanager=None)
        serial = mock.Mock(side_effect=["emulator-5554", ""])
        with mock.patch.object(AVD, "run", _fake_run("killed")), \
                mock.patch.object(AVD, "serial_for_avd", serial):
            r = AVD.stop_avd(paths, "x")
        self.assertTrue(r["ok"])
        self.assertIn("emu kill", r["via"])


class TestBackupStampAndRenameRollback(unittest.TestCase):
    def test_same_second_backup_does_not_clobber(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            root = home / "b.avd"
            root.mkdir()
            (root / "config.ini").write_text("hw.avd.name=b\n")
            (home / "b.ini").write_text("path=x\n")
            stamp = "20261106-010203"
            existing = home / "Backups" / f"b-{stamp}.zip"
            existing.parent.mkdir()
            existing.write_bytes(b"old archive")
            with mock.patch.object(AVD, "avd_dir", lambda n: root), \
                    mock.patch.object(AVD, "avd_home", lambda: home), \
                    mock.patch.object(AVD, "BACKUP_DIR", existing.parent), \
                    mock.patch.object(AVD, "serial_for_avd",
                                      lambda *a, **k: ""), \
                    mock.patch.object(AVD.time, "strftime",
                                      lambda f: stamp):
                r = AVD.backup("b")
            self.assertTrue(r["ok"])
            self.assertTrue(r["file"].endswith(f"b-{stamp}-2.zip"))
            self.assertEqual(existing.read_bytes(), b"old archive")

    def test_rename_rolls_back_when_ini_write_fails(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            src = home / "a.avd"
            src.mkdir()
            (src / "config.ini").write_text("hw.avd.name=a\n")
            (home / "a.ini").write_text("path=x\n")
            boom = mock.Mock(side_effect=OSError("disk full"))
            with mock.patch.object(AVD, "avd_dir",
                                   lambda n: home / f"{n}.avd"), \
                    mock.patch.object(AVD, "avd_home", lambda: home), \
                    mock.patch.object(AVD, "serial_for_avd",
                                      lambda *a, **k: ""), \
                    mock.patch.object(Path, "write_text", boom):
                r = AVD.rename_avd(FAKE, "a", "b")
            self.assertFalse(r["ok"])
            self.assertTrue(src.exists(), "source dir must be restored")
            self.assertFalse((home / "b.avd").exists())
            self.assertTrue((home / "a.ini").exists())


# ---------------------------------------------------------------------------
# Bugs: inifile dedupe + engine unknowns
# ---------------------------------------------------------------------------
class TestInifileDedupe(unittest.TestCase):
    def test_preexisting_duplicate_key_is_collapsed(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.ini"
            p.write_text("hw.ramSize=2048\n# keep\nhw.ramSize=4096\nother=1\n")
            inifile.upsert(p, {"hw.ramSize": "6144"})
            text = p.read_text()
            self.assertEqual(text.count("hw.ramSize="), 1)
            self.assertIn("hw.ramSize=6144", text)
            self.assertIn("# keep", text)
            self.assertIn("other=1", text)


class TestEngineUnknownConfig(unittest.TestCase):
    def test_missing_ram_and_cores_are_unknown_not_violations(self):
        rows = ENGINE.avd_rows([{"name": "naked", "config": {
            "image.sysdir_1": ""}}])
        self.assertTrue(rows[0]["ram_ok"])
        self.assertTrue(rows[0]["cores_ok"])
        self.assertIsNone(rows[0]["ram_mb"])

    def test_below_floor_still_flagged(self):
        rows = ENGINE.avd_rows([{"name": "small", "config": {
            "image.sysdir.1": "system-images/android-37/x86_64",
            "hw.ramSize": "2048", "hw.cpu.ncore": "2"}}])
        self.assertFalse(rows[0]["ram_ok"])
        self.assertTrue(rows[0]["cores_ok"])

    def test_canary_detection_and_report_fields(self):
        self.assertTrue(ENGINE.is_canary("37.3.6"))
        self.assertFalse(ENGINE.is_canary("37.2.13"))
        self.assertFalse(ENGINE.is_canary(""))
        # engine.report imports its collaborators lazily — patch at source.
        with mock.patch.object(ENGINE, "emulator_version",
                               lambda p: "37.2.12"), \
                mock.patch("core.accel.accel_status",
                           return_value={"ready": True, "detail": "ok"}), \
                mock.patch("core.avd.list_avds",
                           return_value={"ok": True, "avds": []}), \
                mock.patch("core.perf.hybrid_note", lambda p: None), \
                mock.patch("core.perf.suggest_flags",
                           return_value={"flags": [], "note": ""}), \
                mock.patch("core.perf.detect_hybrid",
                           return_value={"hybrid": False}):
            r = ENGINE.report(FAKE)
        self.assertIn("latest_canary", r["engine"])
        self.assertFalse(r["engine"]["canary"])
        self.assertIn("hybrid", r["engine"])
        self.assertTrue(any("Next-generation engine" in n["title"]
                            for n in r["engine"]["notes"]))


# ---------------------------------------------------------------------------
# Bugs: sms / sensor / display / macro / clamp
# ---------------------------------------------------------------------------
class TestSmallBugFixes(unittest.TestCase):
    def test_clamp_int_survives_infinity(self):
        # infinity clamps to the bound it reaches for; NaN falls back
        self.assertEqual(ADB._clamp_int(float("inf"), 0, 100), 100)
        self.assertEqual(ADB._clamp_int("-inf", 0, 100, 7), 0)
        self.assertEqual(ADB._clamp_int(float("nan"), 0, 100, 7), 7)
        self.assertEqual(ADB._clamp_int("inf", 0, 100, 7), 100)

    def test_send_sms_refuses_filtered_text_instead_of_swapping(self):
        with mock.patch.object(APPS, "run") as m:
            r = APPS.send_sms(FAKE, "emulator-5554", "+15550100",
                              "hi; rm -rf /")
        self.assertFalse(r["ok"])
        m.assert_not_called()

    def test_sensor_values_are_stripped(self):
        box = {}
        with mock.patch.object(APPS, "run",
                               mock.Mock(side_effect=lambda c, timeout=0:
                                         (box.update(cmd=c),
                                          {"ok": True, "code": 0,
                                           "out": "", "err": ""})[1])):
            APPS.sensor_set(FAKE, "emulator-5554", "acceleration",
                            ["1.5", " 2", "9.8 "])
        self.assertEqual(box["cmd"][-1], "1.5:2:9.8")

    def test_refresh_rate_garbage_output_is_not_a_500(self):
        with mock.patch.object(DISP, "run", _fake_run("Bad setting: 90")):
            r = DISP.set_refresh_rate(FAKE, "emulator-5554", 90)
        self.assertFalse(r["ok"])

    def test_macro_save_survives_oserror_and_reserved_names(self):
        r = APPS.__class__ and ADB.macro_save("CON", [{"type": "sleep", "s": 1}])
        self.assertFalse(r["ok"])
        with mock.patch.object(ADB.Path, "write_text",
                               mock.Mock(side_effect=OSError("read-only"))):
            r2 = ADB.macro_save("okname", [{"type": "sleep", "s": 1}])
        self.assertFalse(r2["ok"])
        self.assertIn("could not save", r2["err"])


# ---------------------------------------------------------------------------
# Features: smart app archive
# ---------------------------------------------------------------------------
class TestSmartArchive(unittest.TestCase):
    def test_archive_pulls_then_uninstalls_keeping_data(self):
        calls = []
        seq = [
            {"ok": True, "code": 0, "out": "package:/data/app/x/base.apk",
             "err": ""},                                   # pm path
            {"ok": True, "code": 0, "out": "[100%] x.apk", "err": ""},  # pull
            {"ok": True, "code": 0, "out": "Success", "err": ""},       # uninstall
        ]
        with mock.patch.object(APPS, "run",
                               mock.Mock(side_effect=lambda c, timeout=0:
                                         (calls.append(c), seq.pop(0))[1])):
            r = APPS.archive_app(FAKE, "emulator-5554", "com.example.game")
        self.assertTrue(r["ok"])
        # pull: [adb, -s, serial, pull, <apk>, <dest>] — device APK first
        self.assertEqual(calls[1][-3:-1], ["pull", "/data/app/x/base.apk"])
        # uninstall keeps data: pm uninstall -k --user 0 <pkg>
        self.assertEqual(calls[2][-6:], ["pm", "uninstall", "-k", "--user",
                                         "0", "com.example.game"])

    def test_archive_refuses_bad_package(self):
        r = APPS.archive_app(FAKE, "emulator-5554", "bad package")
        self.assertFalse(r["ok"])

    def test_restore_uses_saved_apk(self):
        with tempfile.TemporaryDirectory() as td:
            adir = Path(td)
            (adir / "com.example.game.apk").write_bytes(b"PK")
            with mock.patch.object(APPS, "ARCHIVE_DIR", adir), \
                    mock.patch("core.adb.install_apk",
                               return_value={"ok": True}) as inst:
                r = APPS.restore_app(FAKE, "emulator-5554", "com.example.game")
        self.assertTrue(r["ok"])
        inst.assert_called_once()

    def test_archived_listing(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "com.a.apk").write_bytes(b"PK")
            with mock.patch.object(APPS, "ARCHIVE_DIR", Path(td)):
                r = APPS.archived()
        self.assertEqual([x["package"] for x in r["archived"]], ["com.a"])


# ---------------------------------------------------------------------------
# Features: scrcpy wrapper
# ---------------------------------------------------------------------------
class TestScrcpyWrapper(unittest.TestCase):
    def test_honest_unavailable(self):
        with mock.patch.object(STREAM, "find_scrcpy", lambda p: ""):
            r = STREAM.status(FAKE)
        self.assertFalse(r["available"])
        self.assertIn("not found", r["note"])

    def test_mirror_command_shape(self):
        box = {}
        proc = mock.Mock(pid=5)
        with mock.patch.object(STREAM, "find_scrcpy", lambda p: "scrcpy.exe"), \
                mock.patch.object(STREAM.subprocess, "Popen",
                                  mock.Mock(return_value=proc)) as popen:
            r = STREAM.mirror(FAKE, "emulator-5554", 90)
        self.assertTrue(r["ok"])
        box["cmd"] = popen.call_args[0][0]
        self.assertEqual(box["cmd"][:4], ["scrcpy.exe", "-s", "emulator-5554",
                                          "--max-fps"])

    def test_app_window_flags(self):
        proc = mock.Mock(pid=6)
        with mock.patch.object(STREAM, "find_scrcpy", lambda p: "scrcpy.exe"), \
                mock.patch.object(STREAM.subprocess, "Popen",
                                  mock.Mock(return_value=proc)) as popen:
            STREAM.app_window(FAKE, "emulator-5554", "com.android.settings",
                              480, 800)
        cmd = popen.call_args[0][0]
        self.assertIn("--start-app", cmd)
        self.assertIn("com.android.settings", cmd)
        self.assertIn("480x800", cmd)

    def test_invalid_package_refused(self):
        with mock.patch.object(STREAM, "find_scrcpy", lambda p: "scrcpy.exe"):
            r = STREAM.app_window(FAKE, "emulator-5554", "bad pkg; rm", 0, 0)
        self.assertFalse(r["ok"])


# ---------------------------------------------------------------------------
# Features: GPU preference
# ---------------------------------------------------------------------------
class _FakeWinreg:
    """Registry double shared by the GPU-preference tests.

    Mirrors the real module shape: SetValueEx/DeleteValue live at module
    level and take the key object as their first argument.
    """
    HKEY_CURRENT_USER = 0
    KEY_SET_VALUE = 2
    REG_SZ = 1
    values: dict = {}

    @classmethod
    def reset(cls):
        cls.values = {}

    @classmethod
    def OpenKey(cls, root, path):
        class K:
            def __enter__(self):
                return cls
            def __exit__(self, *a):
                return False
            @staticmethod
            def QueryValueEx(name):
                try:
                    return _FakeWinreg.values[name], 1
                except KeyError:
                    raise FileNotFoundError(name)
        return K()

    @classmethod
    def CreateKeyEx(cls, root, path, res, access):
        class K:
            def __enter__(self):
                return cls
            def __exit__(self, *a):
                return False
        return K()

    @classmethod
    def QueryValueEx(cls, key, name):
        try:
            return cls.values[name], 1
        except KeyError:
            raise FileNotFoundError(name)

    @classmethod
    def SetValueEx(cls, key, name, reserved, typ, val):
        cls.values[name] = val

    @classmethod
    def DeleteValue(cls, key, name):
        cls.values.pop(name, None)


class TestGpuPreference(unittest.TestCase):
    def setUp(self):
        _FakeWinreg.reset()
        # The qemu worker binary is only pinned when it actually exists next
        # to emulator.exe — stage a fake SDK layout in a tempdir.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        emu = Path(self._tmp.name) / "emulator.exe"
        qemu = Path(self._tmp.name) / "qemu-system-x86_64.exe"
        emu.write_bytes(b"MZ")
        qemu.write_bytes(b"MZ")
        self.emu_path = str(emu)

    def _emu(self):
        return ToolPaths(sdk=None, android_cli=None, adb=None,
                         emulator=self.emu_path,
                         emulator_check=None, avdmanager=None)

    def test_set_and_get_roundtrip(self):
        with mock.patch.dict(sys.modules, {"winreg": _FakeWinreg}):
            r = GPU.set_pref(self._emu(), "performance")
            self.assertTrue(r["ok"])
            g = GPU.get(self._emu())
        self.assertTrue(g["ok"])
        modes = {e["mode"] for e in g["entries"]}
        self.assertIn("high performance (dGPU)", modes)
        # the qemu worker binary must be pinned too — it is the one drawing
        self.assertTrue(any("qemu-system-x86_64" in e["exe"]
                            for e in g["entries"]))

    def test_auto_removes_the_override(self):
        with mock.patch.dict(sys.modules, {"winreg": _FakeWinreg}):
            GPU.set_pref(self._emu(), "power")
            self.assertTrue(_FakeWinreg.values)
            GPU.set_pref(self._emu(), "auto")
            self.assertEqual(_FakeWinreg.values, {})
            g = GPU.get(self._emu())
        self.assertTrue(all(e["mode"] == "auto" for e in g["entries"]))

    def test_bad_mode_refused(self):
        r = GPU.set_pref(self._emu(), "turbo")
        self.assertFalse(r["ok"])


# ---------------------------------------------------------------------------
# Features: hybrid CPU detection + WoA guard
# ---------------------------------------------------------------------------
_CPU_MHZ = [4500] * 8 + [3200] * 4       # 8 P-cores, 4 E-cores


class _FakeWinregCpu:
    """Registry double: 12 logical CPUs, two clock groups, 12th-Gen name.

    Mirrors the real module API: EnumKey/QueryValueEx are module-level
    functions that take the key object as their first argument.
    """
    HKEY_LOCAL_MACHINE = 0

    class _Key:
        def __init__(self, idx=None):
            self.idx = idx
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def QueryValueEx(self, name):
            if self.idx is None:            # the hive root has no values
                raise FileNotFoundError(name)
            if name == "~MHz":
                return _CPU_MHZ[self.idx], 1
            if name == "ProcessorNameString":
                return ("12th Gen Intel(R) Core(TM) i7-1265U", 1)
            raise FileNotFoundError(name)

    @classmethod
    def OpenKey(cls, root, path):
        if str(path).endswith("CentralProcessor"):
            return cls._Key()
        return cls._Key(int(path))

    @classmethod
    def EnumKey(cls, key, i):
        if i >= len(_CPU_MHZ):
            raise OSError("end")
        return str(i)

    @classmethod
    def QueryValueEx(cls, key, name):
        return key.QueryValueEx(name)


def _hybrid_state():
    return {"hybrid": True, "pcore_mask": 255, "pcore_count": 8,
            "logical_cpus": 12, "ecore_count": 4, "detail": "two clock groups",
            "name": "12th Gen Intel", "platform": "win32"}


class TestHybridDetection(unittest.TestCase):
    def test_two_clock_groups_yield_a_pcore_mask(self):
        with mock.patch.dict(sys.modules, {"winreg": _FakeWinregCpu}), \
                mock.patch.object(PERF.sys, "platform", "win32"), \
                mock.patch.object(PERF, "_HYBRID_CACHE", None):
            h = PERF.detect_hybrid()
        self.assertTrue(h["hybrid"])
        self.assertEqual(h["logical_cpus"], 12)
        self.assertEqual(h["pcore_count"], 8)
        self.assertEqual(h["pcore_mask"], sum(1 << j for j in range(8)))

    def test_hybrid_note_quiet_on_new_engine(self):
        import core.engine as ENGINE_MOD
        EMU = ToolPaths(sdk=None, android_cli=None, adb=None,
                        emulator="emulator.exe", emulator_check=None,
                        avdmanager=None)
        with mock.patch.object(PERF, "detect_hybrid", _hybrid_state), \
                mock.patch.object(ENGINE_MOD, "emulator_version",
                                  lambda p: "37.3.6"):
            self.assertIsNone(PERF.hybrid_note(EMU))

    def test_hybrid_note_warns_on_old_engine(self):
        import core.engine as ENGINE_MOD
        EMU = ToolPaths(sdk=None, android_cli=None, adb=None,
                        emulator="emulator.exe", emulator_check=None,
                        avdmanager=None)
        with mock.patch.object(PERF, "detect_hybrid", _hybrid_state), \
                mock.patch.object(ENGINE_MOD, "emulator_version",
                                  lambda p: "37.2.13"):
            note = PERF.hybrid_note(EMU)
        self.assertIsNotNone(note)
        self.assertIn("E-core", note["title"])
        self.assertEqual(note["pcore_mask"], 255)


class TestWoaGuard(unittest.TestCase):
    def test_arm64_host_gets_honest_verdict(self):
        EMU = ToolPaths(sdk=None, android_cli=None, adb=None,
                        emulator="emulator.exe", emulator_check=None,
                        avdmanager=None)
        with mock.patch.object(ACCEL.platform, "system", lambda: "Windows"), \
                mock.patch.object(ACCEL.platform, "machine",
                                  lambda: "ARM64"), \
                mock.patch.object(ACCEL, "run") as run_mock:
            r = ACCEL.accel_status(EMU)
        self.assertEqual(r["accel"], "unsupported")
        self.assertFalse(r["ready"])
        run_mock.assert_not_called()

    def test_x64_host_unaffected(self):
        EMU = ToolPaths(sdk=None, android_cli=None, adb=None,
                        emulator="emulator.exe", emulator_check=None,
                        avdmanager=None)
        with mock.patch.object(ACCEL.platform, "system", lambda: "Windows"), \
                mock.patch.object(ACCEL.platform, "machine", lambda: "AMD64"), \
                mock.patch.object(ACCEL, "run",
                                  _fake_run("accel:\n0\nWHPX is usable", ok=True)):
            r = ACCEL.accel_status(EMU)
        self.assertNotEqual(r["accel"], "unsupported")


# ---------------------------------------------------------------------------
# Features: agent skill file + integrity note
# ---------------------------------------------------------------------------
class TestAgentSkillAndIntegrity(unittest.TestCase):
    def test_skill_file_documents_auth_and_safety(self):
        t = AGENTSKILL.skill_text()
        for needle in ("Authorization: Bearer", "/api/session", "/api/input/text",
                       "Safety rules", "POST /api/apps/archive",
                       "/api/scrcpy/app-window", "ticket"):
            self.assertIn(needle, t)

    def test_skill_starts_with_a_title(self):
        self.assertTrue(AGENTSKILL.skill_text().startswith("# NovaDroid"))

    def test_integrity_note_names_the_tiers(self):
        with tempfile.TemporaryDirectory() as td:
            apk = Path(td) / "x.apk"
            apk.write_bytes(b"PK\x03\x04")
            with mock.patch.object(COMPAT, "apk_badging",
                                   return_value={"ok": True, "minSdk": 24,
                                                 "abis": ["x86_64"],
                                                 "package": "com.x",
                                                 "label": "X"}):
                r = COMPAT.check(FAKE, str(apk), "")
        self.assertIn("MEETS_DEVICE_INTEGRITY", r["integrity_note"])
        self.assertIn("Feb 2026", r["integrity_note"])
