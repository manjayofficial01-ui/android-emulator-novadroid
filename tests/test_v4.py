"""v4.0.0 tests — every bug fixed and feature added in this release, pinned.

Grouped by capability; all run without a device or SDK (mocked runner or
tempdirs). Every test here would fail against the v3 code.
Run: python -m unittest tests.test_v4 -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import adb as ADB  # noqa: E402
from core import ai as AI  # noqa: E402
from core import apps as APPS  # noqa: E402
from core import avd as AVD  # noqa: E402
from core import display as DISP  # noqa: E402
from core import ports as PORTS  # noqa: E402
from core.config import ToolPaths  # noqa: E402

FAKE = ToolPaths(sdk=None, android_cli=None, adb="adb.exe",
                 emulator=None, emulator_check=None, avdmanager=None)
NO_TOOLS = ToolPaths(sdk=None, android_cli=None, adb=None,
                     emulator=None, emulator_check=None, avdmanager=None)


# ---------------------------------------------------------------------------
# Bug fixes
# ---------------------------------------------------------------------------
class TestInputTextEscaping(unittest.TestCase):
    """v3 silently DELETED apostrophes ("it's" arrived as "its")."""

    def _captured(self):
        box = {}

        def fake_run(cmd, timeout=0):
            box["payload"] = cmd[cmd.index("text") + 1]
            return {"ok": True, "code": 0, "out": "", "err": ""}
        return box, fake_run

    def test_apostrophe_and_space_survive(self):
        box, fr = self._captured()
        with mock.patch.object(ADB, "run", fr):
            r = ADB.input_text(FAKE, "emulator-5554", "it's a test")
        self.assertTrue(r["ok"])
        # inside double quotes the shell keeps ' and spaces verbatim
        self.assertIn("'s", box["payload"])
        self.assertIn(" a ", box["payload"])

    def test_shell_metacharacters_are_escaped(self):
        box, fr = self._captured()
        with mock.patch.object(ADB, "run", fr):
            ADB.input_text(FAKE, "emulator-5554", 'say "hi"; rm $x `whoami`')
        p = box["payload"]
        # v5.0.1: the payload is actually WRAPPED in double quotes now — the
        # v5.0.0 code escaped the right four characters but never added the
        # quotes, so `x; reboot` executed as two shell commands on the device.
        self.assertTrue(p.startswith('"') and p.endswith('"'),
                        f"payload must be a quoted shell word: {p!r}")
        self.assertIn('\\"', p)        # double quote escaped
        self.assertIn('\\$', p)        # dollar escaped
        self.assertIn('\\`', p)        # backtick escaped
        # inside the wrapper quotes every remaining quote must be escaped
        inner = p[1:-1].replace('\\"', '')
        self.assertNotIn('"', inner)

    def test_semicolon_injection_is_neutralized(self):
        # the v5.0.0 attack: text "x; reboot" ran `reboot` as a second command
        box, fr = self._captured()
        with mock.patch.object(ADB, "run", fr):
            ADB.input_text(FAKE, "emulator-5554", "x; reboot")
        self.assertEqual(box["payload"], '"x; reboot"')

    def test_percent_is_not_doubled(self):
        # AOSP Input.sendText does exactly ONE substitution (%s -> space);
        # there is no %% unescape, so v4's doubling typed "50%% done".
        box, fr = self._captured()
        with mock.patch.object(ADB, "run", fr):
            ADB.input_text(FAKE, "emulator-5554", "100% done")
        self.assertIn("100% done", box["payload"])
        self.assertNotIn("%%", box["payload"])


class TestScreenshotBytes(unittest.TestCase):
    """The raw path must serve the same bytes the b64 wrapper encodes."""

    def _png(self):
        return ADB.PNG_MAGIC + b"\x00" * 2000 + ADB.PNG_IEND

    def test_bytes_path_returns_raw_png(self):
        with mock.patch.object(ADB, "run_bytes",
                               return_value={"ok": True, "out": self._png(),
                                             "err": ""}) as rb:
            r = ADB.screenshot_bytes(FAKE, "emulator-5554")
        self.assertTrue(r["ok"])
        self.assertIsInstance(r["png"], bytes)
        self.assertEqual(r["png"], self._png())
        self.assertIn("exec-out", rb.call_args[0][0])

    def test_b64_wrapper_is_consistent(self):
        import base64
        with mock.patch.object(ADB, "run_bytes",
                               return_value={"ok": True, "out": self._png(),
                                             "err": ""}):
            raw = ADB.screenshot_bytes(FAKE, "emulator-5554")
            b64 = ADB.screenshot(FAKE, "emulator-5554")
        self.assertEqual(base64.b64decode(b64["png_b64"]), raw["png"])
        self.assertEqual(b64["via"], raw["via"])


class TestWaitBootReportsRealTime(unittest.TestCase):
    """wait_boot hardcoded waited_s=0 — now it measures."""

    def test_waited_s_is_measured_not_zero(self):
        import itertools
        import types
        # t0 is read at entry (0.0), success happens at tick 1.0 → waited_s 1.0.
        ticks = itertools.count(0, 1)
        fake_time = types.SimpleNamespace(time=lambda: float(next(ticks)),
                                          sleep=lambda s: None)
        with mock.patch.object(APPS, "time", fake_time):
            with mock.patch.object(APPS, "boot_state",
                                   lambda p, s: {"ok": True, "booted": True,
                                                 "ready": True}):
                r = APPS.wait_boot(FAKE, "emulator-5554", timeout=30)
        self.assertTrue(r["booted"])
        # ticks: deadline=0, t0=1, loop-check=2, final=3 → waited = 3-1 = 2.0.
        # The point: it is the real delta, not the old hardcoded 0.
        self.assertEqual(r["waited_s"], 2.0)

    def test_timeout_reports_failure(self):
        import itertools
        import types
        # time must ADVANCE or wait_boot's deadline loop never terminates —
        # a frozen clock turns the test into an infinite spin.
        ticks = itertools.count(0, 1)
        fake_time = types.SimpleNamespace(time=lambda: float(next(ticks)),
                                          sleep=lambda s: None)
        with mock.patch.object(APPS, "time", fake_time):
            with mock.patch.object(APPS, "boot_state",
                                   lambda p, s: {"ok": False}):
                r = APPS.wait_boot(FAKE, "emulator-5554", timeout=30)
        self.assertFalse(r["booted"])
        self.assertIn("never came online", r["err"])


# ---------------------------------------------------------------------------
# New features
# ---------------------------------------------------------------------------
class TestPortsParsing(unittest.TestCase):
    def test_parse_rules(self):
        text = ("emulator-5554 tcp:8080 tcp:8080\r\n"
                "emulator-5554 tcp:5000 localabstract:webview_devtools_remote_42\n"
                "garbage line\n"
                "emulator-5556 tcp:9000 tcp:9000\n")
        rules = PORTS.parse_rules(text)
        self.assertEqual(len(rules), 3)
        self.assertEqual(rules[0]["serial"], "emulator-5554")
        self.assertEqual(rules[1]["remote"], "localabstract:webview_devtools_remote_42")
        self.assertEqual(rules[2]["serial"], "emulator-5556")

    def test_add_validates_direction_and_ports(self):
        r = PORTS.add(FAKE, "emulator-5554", "sideways", 8080, 8080)
        self.assertFalse(r["ok"])
        r = PORTS.add(FAKE, "emulator-5554", "forward", 0, 8080)
        self.assertFalse(r["ok"])
        r = PORTS.add(FAKE, "emulator-5554", "reverse", 8080, 0)
        self.assertFalse(r["ok"])

    def test_add_builds_the_right_adb_command(self):
        calls = []

        def fake_run(cmd, timeout=0):
            calls.append(cmd)
            return {"ok": True, "code": 0, "out": "", "err": ""}
        with mock.patch.object(PORTS, "run", fake_run):
            r = PORTS.add(FAKE, "emulator-5554", "forward", 8080, 8080)
        self.assertTrue(r["ok"])
        self.assertEqual(calls[0],
                         ["adb.exe", "-s", "emulator-5554", "forward",
                          "tcp:8080", "tcp:8080"])

    def test_abstract_socket_forward_only(self):
        r = PORTS.add(FAKE, "emulator-5554", "reverse", 8080, 0,
                      remote_abstract="sock")
        self.assertFalse(r["ok"])

    def test_remove_all(self):
        calls = []

        def fake_run(cmd, timeout=0):
            calls.append(cmd)
            return {"ok": True, "code": 0, "out": "", "err": ""}
        with mock.patch.object(PORTS, "run", fake_run):
            r = PORTS.remove(FAKE, "emulator-5554", "forward", 0, all_rules=True)
        self.assertTrue(r["ok"])
        self.assertIn("--remove-all", calls[0])


class TestOpenUrl(unittest.TestCase):
    def test_rejects_schemeless(self):
        with mock.patch.object(APPS, "run") as m:
            r = APPS.open_url(FAKE, "emulator-5554", "www.google.com")
        self.assertFalse(r["ok"])
        m.assert_not_called()

    def test_accepts_https_deep_link_and_market(self):
        for u in ("https://example.com/x?y=1",
                  "myapp://screen/settings",
                  "market://details?id=com.foo",
                  "mailto:a@b.c", "tel:+1555"):
            with mock.patch.object(APPS, "run",
                                   return_value={"ok": True, "out": "", "err": ""}):
                r = APPS.open_url(FAKE, "emulator-5554", u)
            self.assertTrue(r["ok"], u)

    def test_intent_error_is_reported(self):
        with mock.patch.object(APPS, "run", return_value={
                "ok": True, "out": "Error: Activity class does not exist", "err": ""}):
            r = APPS.open_url(FAKE, "emulator-5554", "myapp://x")
        self.assertFalse(r["ok"])


class TestProxy(unittest.TestCase):
    def test_validates_host_port(self):
        for bad in ("", "localhost", "8080", "a b:80", "host:99999"):
            r = APPS.set_proxy(FAKE, "emulator-5554", bad)
            self.assertFalse(r["ok"], bad)

    def test_verified_write(self):
        def fake_run(cmd, timeout=0):
            if "get global http_proxy" in " ".join(cmd):
                return {"ok": True, "out": "127.0.0.1:8888", "err": ""}
            return {"ok": True, "out": "", "err": ""}
        with mock.patch.object(APPS, "run", fake_run):
            r = APPS.set_proxy(FAKE, "emulator-5554", "127.0.0.1:8888")
        self.assertTrue(r["ok"])
        self.assertEqual(r["verified"], "127.0.0.1:8888")


class TestEcoMode(unittest.TestCase):
    def test_on_disables_animations_and_trims(self):
        cmds = []

        def fake_run(cmd, timeout=0):
            cmds.append(" ".join(cmd))
            return {"ok": True, "out": "done", "err": ""}
        with mock.patch.object(APPS, "run", fake_run):
            r = APPS.eco_mode(FAKE, "emulator-5554", True)
        self.assertTrue(r["ok"])
        joined = "\n".join(cmds)
        self.assertIn("window_animation_scale 0", joined)
        self.assertIn("transition_animation_scale 0", joined)
        self.assertIn("animator_duration_scale 0", joined)
        self.assertIn("pm trim-caches", joined)

    def test_off_restores_scale_1_without_trim(self):
        cmds = []

        def fake_run(cmd, timeout=0):
            cmds.append(" ".join(cmd))
            return {"ok": True, "out": "", "err": ""}
        with mock.patch.object(APPS, "run", fake_run):
            r = APPS.eco_mode(FAKE, "emulator-5554", False)
        self.assertTrue(r["ok"])
        self.assertIn("window_animation_scale 1", "\n".join(cmds))
        self.assertNotIn("trim-caches", "\n".join(cmds))


class TestRefreshRate(unittest.TestCase):
    def test_only_known_rates(self):
        for bad in (30, 0, 144, "fast"):
            r = DISP.set_refresh_rate(FAKE, "emulator-5554", bad)
            self.assertFalse(r["ok"], bad)

    def test_verified_apply(self):
        def fake_run(cmd, timeout=0):
            return {"ok": True, "out": "120.0", "err": ""}
        with mock.patch.object(DISP, "run", fake_run):
            r = DISP.set_refresh_rate(FAKE, "emulator-5554", 120)
        self.assertTrue(r["ok"])
        self.assertEqual(r["verified_hz"], "120.0")

    def test_ignored_setting_is_detected(self):
        def fake_run(cmd, timeout=0):
            return {"ok": True, "out": "null", "err": ""}
        with mock.patch.object(DISP, "run", fake_run):
            r = DISP.set_refresh_rate(FAKE, "emulator-5554", 90)
        self.assertFalse(r["ok"])   # image ignored it — say so, don't lie


class TestAvdDiskToolkit(unittest.TestCase):
    # Sizes are MB-scale so the rounded _mb fields aren't all 0.0.
    def _avd(self, home: Path, name="t") -> Path:
        d = home / f"{name}.avd"
        d.mkdir(parents=True)
        (d / "config.ini").write_text("hw.ramSize=4096\n")
        (d / "userdata-qemu.img").write_bytes(b"u" * 3_000_000)
        (d / "cache.img").write_bytes(b"c" * 2_000_000)
        snap = d / "snapshots" / "default_boot"
        snap.mkdir(parents=True)
        (snap / "snap.bin").write_bytes(b"s" * 1_500_000)
        (home / f"{name}.ini").write_text(f"path={d}\n")
        return d

    def test_disk_usage_breakdown(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self._avd(home)
            with mock.patch.object(AVD, "avd_home", lambda: home):
                r = AVD.disk_usage("t")
        self.assertTrue(r["ok"])
        b = r["breakdown_mb"]
        self.assertEqual(b["userdata"], 3.0)
        self.assertEqual(b["cache"], 2.0)
        self.assertEqual(b["snapshots"], 1.5)
        self.assertEqual(r["reclaimable_mb"], 3.5)

    def test_cleanup_removes_reclaimable_keeps_userdata(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            d = self._avd(home)
            with mock.patch.object(AVD, "avd_home", lambda: home):
                r = AVD.cleanup(NO_TOOLS, "t")
                self.assertTrue(r["ok"])
                self.assertFalse((d / "cache.img").exists())
                self.assertFalse((d / "snapshots").exists())
                self.assertTrue((d / "userdata-qemu.img").exists())
                self.assertGreater(r["freed_mb"], 0)

    def test_cleanup_trims_when_running(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self._avd(home)
            cmds = []

            def fake_run(cmd, timeout=0):
                cmds.append(" ".join(cmd))
                return {"ok": True, "out": "ok", "err": ""}
            with mock.patch.object(AVD, "avd_home", lambda: home), \
                 mock.patch.object(AVD, "run", fake_run):
                r = AVD.cleanup(FAKE, "t", serial="emulator-5554")
        self.assertTrue(r["guest_trimmed"])
        self.assertIn("sm fstrim", "\n".join(cmds))

    def test_backup_and_restore_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self._avd(home, "orig")
            with mock.patch.object(AVD, "avd_home", lambda: home), \
                 mock.patch.object(AVD, "BACKUP_DIR", home / "backups"):
                b = AVD.backup("orig")
                self.assertTrue(b["ok"])
                self.assertTrue(Path(b["file"]).exists())
                with zipfile.ZipFile(b["file"]) as z:
                    names = z.namelist()
                self.assertIn("orig.ini", names)
                self.assertTrue(any("userdata-qemu.img" in n for n in names))
                # volatile files are excluded
                self.assertFalse(any("cache.img" in n for n in names))
                self.assertFalse(any("hardware-qemu.ini" in n for n in names))
                # restore under a new name
                r = AVD.restore(b["file"], "copy")
                self.assertTrue(r["ok"], r.get("err"))
                self.assertTrue((home / "copy.avd" / "userdata-qemu.img").exists())
                self.assertTrue((home / "copy.ini").exists())
                ini = (home / "copy.ini").read_text()
                self.assertIn(f"path={home / 'copy.avd'}", ini)

    def test_backup_refuses_running_avd(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self._avd(home, "busy")
            AVD._SERIAL_MAP.clear()
            AVD._SERIAL_MAP["busy"] = "emulator-5554"
            try:
                with mock.patch.object(AVD, "avd_home", lambda: home), \
                     mock.patch.object(AVD, "BACKUP_DIR", home / "backups"):
                    r = AVD.backup("busy")
            finally:
                AVD._SERIAL_MAP.clear()
        self.assertFalse(r["ok"])

    def test_rename_moves_everything(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self._avd(home, "old")
            with mock.patch.object(AVD, "avd_home", lambda: home):
                r = AVD.rename_avd(NO_TOOLS, "old", "new")
                self.assertTrue(r["ok"], r.get("err"))
                self.assertTrue((home / "new.avd" / "userdata-qemu.img").exists())
                self.assertFalse((home / "old.avd").exists())
                self.assertTrue((home / "new.ini").exists())
                self.assertFalse((home / "old.ini").exists())
                cfg = (home / "new.avd" / "config.ini").read_text()
                self.assertIn("hw.avd.name=new", cfg)


class TestBatchInstall(unittest.TestCase):
    def test_installs_on_all_online_emulators_in_parallel(self):
        cmds = []

        def fake_run(cmd, timeout=0):
            cmds.append(cmd)
            if cmd[1:3] == ["adb.exe", "devices"] or cmd[:2] == ["adb.exe", "devices"]:
                return {"ok": True, "out":
                        "List of devices attached\n"
                        "emulator-5554\tdevice\nemulator-5556\tdevice\n"
                        "emulator-5558\toffline\nusb123\tdevice\n", "err": ""}
            return {"ok": True, "code": 0, "out": "Success", "err": ""}
        p = Path(tempfile.gettempdir()) / "nd-test.apk"
        p.write_bytes(b"PK")
        try:
            with mock.patch.object(ADB, "run", fake_run):
                r = ADB.install_apk_all(FAKE, str(p))
        finally:
            p.unlink()
        self.assertTrue(r["ok"])
        # offline + non-emulator serials must be skipped
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["installed"], ["emulator-5554", "emulator-5556"])
        targets = [c[c.index("-s") + 1] for c in cmds if "install" in c]
        self.assertEqual(sorted(targets), ["emulator-5554", "emulator-5556"])

    def test_no_devices(self):
        with mock.patch.object(ADB, "run", return_value={
                "ok": True, "out": "List of devices attached\n", "err": ""}):
            r = ADB.install_apk_all(FAKE, "whatever.apk")
        self.assertFalse(r["ok"])


class TestMacroLoops(unittest.TestCase):
    def test_run_repeats_and_logs_loop_index(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(ADB, "MACRO_DIR", Path(td)):
                ADB.macro_save("loopy", [{"type": "tap", "x": 1, "y": 2}])
                with mock.patch.object(ADB, "tap",
                                       return_value={"ok": True}) as mt:
                    with mock.patch.object(ADB.time, "sleep", lambda s: None):
                        r = ADB.macro_run(FAKE, "emulator-5554", "loopy",
                                          loops=3, loop_delay_s=0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["loops"], 3)
        self.assertEqual(mt.call_count, 3)
        self.assertTrue(any(l.startswith("2:0:tap") for l in r["log"]))

    def test_loops_clamped(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(ADB, "MACRO_DIR", Path(td)):
                ADB.macro_save("once", [{"type": "key", "code": 3}])
                with mock.patch.object(ADB, "keyevent",
                                       return_value={"ok": True}):
                    r = ADB.macro_run(FAKE, "emulator-5554", "once",
                                      loops=9999, loop_delay_s=0)
        self.assertEqual(r["loops"], 500)


class TestAiProbeBatching(unittest.TestCase):
    """The AI panel must not shell out once per package prefix."""

    def _fake_sh(self):
        calls = []

        def sh(paths, serial, script, timeout=0):
            calls.append(script)
            if script == "pm list packages":
                return {"ok": True, "out":
                        "package:com.google.android.gms\n"
                        "package:com.google.android.as.oss\n"
                        "package:org.tensorflow.lite\n", "err": ""}
            if script.startswith("for p in"):
                return {"ok": True, "out":
                        "com.google.android.gms=versionName=25.05\n", "err": ""}
            return {"ok": True, "out": "", "err": ""}
        return calls, sh

    def test_single_package_listing(self):
        calls, sh = self._fake_sh()
        with mock.patch.object(AI, "_sh", sh), \
             mock.patch.object(AI, "nnapi_status",
                               return_value={"available": True, "detail": "x"}):
            from core import adb as _adb
            with mock.patch.object(_adb, "getprops", return_value={}):
                r = AI.ai_status(FAKE, "emulator-5554")
        self.assertTrue(r["ok"])
        self.assertEqual(r["level"], "partial")   # GMS+PCS present, no AICore
        list_calls = [c for c in calls if c == "pm list packages"]
        self.assertEqual(len(list_calls), 1, "must batch into ONE pm list call")


class TestSyncInputShape(unittest.TestCase):
    """sync_input is the generalised synchronizer behind /api/sync-input."""

    def test_rejects_unknown_action(self):
        import novadroid as ND
        r = ND.sync_input([{"serial": "emulator-5554", "action": "explode"}])
        self.assertFalse(r["ok"])

    def test_fans_out_text_to_all_serials(self):
        import novadroid as ND
        taps_seen = []

        def fake_text(paths, serial, text):
            taps_seen.append((serial, text))
            return {"ok": True}
        # PATHS is a frozen dataclass — replace the whole binding, not a field.
        with mock.patch.object(ND, "PATHS", FAKE):
            with mock.patch.object(ND.ADB, "input_text", fake_text):
                r = ND.sync_input([
                    {"serial": "emulator-5554", "action": "text", "text": "hi"},
                    {"serial": "emulator-5556", "action": "text", "text": "hi"}])
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"], "text")
        self.assertEqual(sorted(taps_seen),
                         [("emulator-5554", "hi"), ("emulator-5556", "hi")])

    def test_sync_tap_backcompat(self):
        import novadroid as ND
        with mock.patch.object(ND, "PATHS", FAKE):
            with mock.patch.object(ND.ADB, "tap", return_value={"ok": True}) as mt:
                r = ND.sync_tap([{"serial": "emulator-5554", "x": 1, "y": 2}])
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"], "tap")
        mt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
