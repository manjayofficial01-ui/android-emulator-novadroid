"""v5.0.0 coverage: engine report, camera environment, and the second round
of backend hardening (serial validation, device-shell quoting, screenshot
races, error redaction, dead code).

Every test is hermetic: temp directories for files, mocks for subprocesses,
no Android SDK and no running emulator required.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import adb as ADB  # noqa: E402
from core import ai as AI  # noqa: E402
from core import engine as ENGINE  # noqa: E402
from core import environment as ENV  # noqa: E402
from core import inifile  # noqa: E402
from core import perf as PERF  # noqa: E402
from core import safe  # noqa: E402
from core import window as WIN  # noqa: E402
from core.config import ToolPaths  # noqa: E402
from core.runner import run  # noqa: E402

NO_TOOLS = ToolPaths(sdk=None, android_cli=None, adb=None, emulator=None,
                     emulator_check=None, avdmanager=None)
WITH_ADB = ToolPaths(sdk=None, android_cli=None, adb="C:/sdk/adb.exe",
                     emulator=None, emulator_check=None, avdmanager=None)


# --------------------------------------------------------------------------
# /api/engine — researched release facts turned into actionable checks
# --------------------------------------------------------------------------
class TestEngine(unittest.TestCase):
    def test_version_parsing_and_channel(self):
        self.assertEqual(ENGINE.version_tuple("Android emulator version 37.2.12.0"),
                         (37, 2, 12, 0))
        self.assertEqual(ENGINE.version_tuple("garbage"), ())
        self.assertEqual(ENGINE.channel_for("37.2.12"), "current")
        self.assertEqual(ENGINE.channel_for("37.2.13"), "current")
        self.assertEqual(ENGINE.channel_for("36.4.9"), "outdated")
        # An SDK we cannot query is NEVER reported as up to date.
        self.assertEqual(ENGINE.channel_for(""), "unknown")
        self.assertEqual(ENGINE.channel_for("canary"), "unknown")

    def test_avd_rows_enforce_api37_ram_floor_and_unit_suffixes(self):
        rows = ENGINE.avd_rows([
            {"name": "good", "config": {
                "image.sysdir.1": "system-images/android-37/google_apis/x86_64/",
                "hw.ramSize": "6G", "hw.cpu.ncore": "4",
                "hw.gpu.mode": "host"}},
            {"name": "starved", "config": {
                "image.sysdir.1": "system-images/android-37/google_apis/x86_64/",
                "hw.ramSize": "2G", "hw.cpu.ncore": "8",
                "hw.gpu.mode": "auto"}},
            {"name": "legacy", "config": {
                "image.sysdir.1": "system-images/android-33/google_apis/x86_64/",
                "hw.ramSize": "2048", "hw.cpu.ncore": "2",
                "hw.gpu.mode": "swiftshader"}},
        ])
        by = {r["name"]: r for r in rows}
        self.assertEqual(by["good"]["api"], 37)
        self.assertEqual(by["good"]["ram_mb"], 6144)   # 37.1.11 "6G" suffix
        self.assertTrue(by["good"]["ram_ok"])
        self.assertTrue(by["good"]["cores_ok"])
        self.assertEqual(by["starved"]["ram_mb"], 2048)
        self.assertFalse(by["starved"]["ram_ok"])
        self.assertFalse(by["starved"]["cores_ok"])    # 8 > 4 vCPU ceiling
        self.assertTrue(by["legacy"]["ram_ok"])        # 1536 floor for < API 37
        self.assertEqual(ENGINE.avd_rows([]), [])

    def test_vulkan_config_is_flagged_for_no_snapshot(self):
        rows = ENGINE.avd_rows([
            {"name": "vk", "config": {"image.sysdir.1": "android-35/default/x86_64",
                                      "hw.ramSize": "4096", "hw.cpu.ncore": "4",
                                      "hw.gpu.mode": "auto",
                                      "hw.gpu.enabled": "VulkanNativeSwapchain"}},
        ])
        self.assertTrue(rows[0]["vulkan_snapshot_risk"])

    def test_report_without_sdk_never_raises(self):
        with mock.patch("core.accel.accel_status",
                        return_value={"accel": "unknown", "ready": False,
                                      "detail": "no SDK", "features_enabled": []}), \
             mock.patch("core.avd.list_avds", return_value={"avds": []}), \
             mock.patch("core.perf.suggest_flags",
                        return_value={"flags": ["-cores 4"], "note": "n"}):
            r = ENGINE.report(NO_TOOLS)
        self.assertTrue(r["ok"])
        self.assertEqual(r["engine"]["channel"], "unknown")
        self.assertEqual(r["engine"]["emulator_version"], "unknown")
        self.assertEqual(r["engine"]["latest_stable"], ENGINE.LATEST_STABLE)
        self.assertTrue(r["engine"]["aehd"]["deprecated"])
        self.assertIn("checks", r["engine"])
        self.assertTrue(any("4 GB" in c["label"] for c in r["engine"]["checks"]))
        self.assertTrue(r["flags"])
        self.assertEqual(r["avds"], [])

    def test_report_survives_a_broken_accel_probe(self):
        with mock.patch("core.accel.accel_status", side_effect=RuntimeError("boom")), \
             mock.patch("core.avd.list_avds", return_value={"avds": []}), \
             mock.patch("core.perf.suggest_flags", side_effect=RuntimeError("boom")):
            r = ENGINE.report(NO_TOOLS)
        self.assertTrue(r["ok"])
        self.assertFalse(r["engine"]["accel"]["ready"])
        self.assertEqual(r["flags"], [])

    def _report_for(self, version: str) -> dict:
        """A report with a pinned `emulator -version` and no SDK probes."""
        with mock.patch.object(ENGINE, "emulator_version", return_value=version), \
             mock.patch("core.accel.accel_status",
                        return_value={"accel": "WHPX", "ready": True,
                                      "detail": "ok", "features_enabled": []}), \
             mock.patch("core.avd.list_avds", return_value={"avds": []}), \
             mock.patch("core.perf.suggest_flags",
                        return_value={"flags": ["-cores 4"], "note": "n"}):
            return ENGINE.report(NO_TOOLS)

    def test_outdated_engine_exposes_an_actionable_update_command(self):
        # A warning that says "outdated" without telling the operator what to
        # run is a dead end. The card must carry the exact verified command.
        e = self._report_for("37.1.11")["engine"]
        self.assertEqual(e["channel"], "outdated")
        upd = e["update"]
        self.assertTrue(upd["available"])
        self.assertEqual(upd["installed"], "37.1.11")
        self.assertEqual(upd["target"], ENGINE.LATEST_PATCH)
        self.assertEqual(upd["command"], ENGINE.UPDATE_COMMAND)
        self.assertEqual(upd["alt_command"], "android sdk update emulator")
        self.assertIn("restart", upd["note"].lower())
        note = next(n for n in e["notes"] if n.get("command"))
        self.assertEqual(note["level"], "warn")
        self.assertIn(ENGINE.UPDATE_COMMAND, note["detail"])
        self.assertIn("37.1.11", note["detail"])

    def test_current_engine_has_no_pending_update(self):
        e = self._report_for("37.2.13.0")["engine"]
        self.assertEqual(e["channel"], "current")
        self.assertFalse(e["update"]["available"])
        self.assertEqual(e["update"]["installed"], "37.2.13.0")
        self.assertFalse(any(n.get("command") for n in e["notes"]))

    def test_unknown_version_is_never_reported_as_an_update(self):
        e = self._report_for("")["engine"]
        self.assertEqual(e["channel"], "unknown")
        self.assertFalse(e["update"]["available"])
        self.assertEqual(e["update"]["installed"], "unknown")

    def test_notes_embed_the_installed_version_when_outdated(self):
        notes = ENGINE._notes({"ready": True}, "outdated", "36.4.9")
        self.assertEqual(notes[0]["command"], ENGINE.UPDATE_COMMAND)
        self.assertIn("36.4.9", notes[0]["detail"])


# --------------------------------------------------------------------------
# /api/avds/environment — 36.6.11 camera "environment" mode
# --------------------------------------------------------------------------
class TestEnvironment(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.avd = self.root / "pixel.avd"
        self.avd.mkdir()
        self.cfg = self.avd / "config.ini"
        self.cfg.write_text("# comment\nhw.ramSize=4096\n", encoding="utf-8")
        patcher = mock.patch("core.avd.avd_dir", return_value=self.avd)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_invalid_names_never_touch_disk(self):
        for bad in ("../escape", "a/b", "name;reboot", ""):
            self.assertFalse(ENV.get(bad)["ok"], bad)
            self.assertFalse(ENV.set_mode(bad, "environment")["ok"], bad)
        self.assertEqual(self.cfg.read_text(encoding="utf-8"),
                         "# comment\nhw.ramSize=4096\n")

    def test_missing_avd_is_reported(self):
        self.cfg.unlink()
        self.assertFalse(ENV.get("pixel")["ok"])
        self.assertFalse(ENV.set_mode("pixel", "environment")["ok"])

    def test_set_environment_creates_placeholder_and_updates_config(self):
        r = ENV.set_mode("pixel", "environment")
        self.assertTrue(r["ok"])
        self.assertEqual(r["camera"], "environment")
        env_ini = self.avd / "environment.ini"
        self.assertTrue(env_ini.is_file())
        self.assertIn("36.6.11", env_ini.read_text(encoding="utf-8"))
        self.assertEqual(inifile.parse(self.cfg)["hw.camera.back"], "environment")
        self.assertIn("hw.ramSize=4096", self.cfg.read_text(encoding="utf-8"))
        got = ENV.get("pixel")
        self.assertEqual(got["mode"], "environment")
        self.assertTrue(got["exists"])
        self.assertEqual(got["keys"], {})  # comment-only placeholder

    def test_existing_scene_definitions_are_never_overwritten(self):
        env_ini = self.avd / "environment.ini"
        original = "# my scene\nscene.name=Office\n"
        env_ini.write_text(original, encoding="utf-8")
        self.assertTrue(ENV.set_mode("pixel", "environment")["ok"])
        self.assertEqual(env_ini.read_text(encoding="utf-8"), original)
        self.assertEqual(ENV.get("pixel")["keys"], {"scene.name": "Office"})

    def test_mode_mapping_and_validation(self):
        self.assertEqual(ENV.set_mode("pixel", "virtual")["camera"], "virtualscene")
        self.assertEqual(ENV.get("pixel")["mode"], "virtual")
        self.assertEqual(ENV.set_mode("pixel", "none")["camera"], "none")
        self.assertFalse(ENV.set_mode("pixel", "webcam0")["ok"])
        self.assertFalse(ENV.set_mode("pixel", "'; reboot; '")["ok"])


# --------------------------------------------------------------------------
# Second-round hardening regressions
# --------------------------------------------------------------------------
class TestV5Hardening(unittest.TestCase):
    def test_adb_check_rejects_malicious_serial(self):
        self.assertIsNone(ADB._check(WITH_ADB, "emulator-5554"))
        for bad in ("emulator-5554; reboot", "-s other", "a b", "x\nreboot"):
            r = ADB._check(WITH_ADB, bad)
            self.assertIsNotNone(r, bad)
            self.assertFalse(r["ok"])

    def test_getprops_quotes_and_allowlists_property_names(self):
        scripts = []

        def fake_run(cmd, timeout=60):
            scripts.append(cmd[-1])
            return {"ok": True, "code": 0, "out": "", "err": ""}

        with mock.patch.object(ADB, "run", side_effect=fake_run):
            ADB.getprops(WITH_ADB, "emulator-5554",
                         ["ro.build.version.sdk", "qemu.gles"])
            # An injection-shaped "property" is refused, never interpolated.
            ADB.getprops(WITH_ADB, "emulator-5554", ["ro.x; reboot"])
            ADB.getprops(WITH_ADB, "emulator-5554", ["$(id)"])
        self.assertEqual(len(scripts), 1, "illegal props must not reach adb")
        self.assertIn("'ro.build.version.sdk'", scripts[0])
        self.assertIn("'qemu.gles'", scripts[0])

    def test_screenshot_fallback_uses_unique_paths_and_cleans_up(self):
        import itertools
        import types
        # A minimal buffer that passes _is_png(): magic + padding + IEND.
        fake_png = ADB.PNG_MAGIC + b"\x00" * 1024 + ADB.PNG_IEND
        remotes, cleaned = [], []
        calls = {"pull": 0}

        def fake_run_bytes(cmd, timeout=60):
            return {"ok": True, "code": 0, "out": b"not a png", "err": ""}

        def fake_run(cmd, timeout=60):
            if "screencap" in cmd:
                remotes.append(cmd[-1])
                return {"ok": True, "code": 0, "out": "", "err": ""}
            if "pull" in cmd:
                calls["pull"] += 1
                # The local destination is the last argv element verbatim;
                # never shlex a Windows path (it eats the backslashes).
                Path(cmd[-1]).write_bytes(fake_png)
                return {"ok": True, "code": 0, "out": "", "err": ""}
            if "rm" in cmd:
                cleaned.append(cmd[-1])
            return {"ok": True, "code": 0, "out": "", "err": ""}

        counter = itertools.count(1)
        fake_time = types.SimpleNamespace(time_ns=lambda: next(counter))
        with mock.patch.object(ADB, "run_bytes", side_effect=fake_run_bytes), \
             mock.patch.object(ADB, "run", side_effect=fake_run), \
             mock.patch.object(ADB, "time", fake_time):
            first = ADB.screenshot_bytes(WITH_ADB, "emulator-5554")
            second = ADB.screenshot_bytes(WITH_ADB, "emulator-5554")
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(remotes), 2)
        self.assertNotEqual(remotes[0], remotes[1])
        self.assertNotIn("/sdcard/s.png", remotes)
        self.assertEqual(cleaned, remotes, "every temp file must be removed")
        self.assertEqual(calls["pull"], 2)

    def test_runner_errors_never_echo_host_paths_or_exceptions(self):
        secret = "C:/Users/operator/Secret-APKs/private.apk"
        with mock.patch("core.runner.subprocess.run",
                        side_effect=subprocess.TimeoutExpired("adb", 1)):
            r = run([secret, "-s", "emulator-5554", "install", secret], timeout=1)
        self.assertFalse(r["ok"])
        self.assertNotIn("Secret-APKs", r["err"])
        self.assertNotIn("emulator-5554", r["err"])
        self.assertIn("timeout after 1s", r["err"])
        with mock.patch("core.runner.subprocess.run",
                        side_effect=OSError(f"cannot open {secret}")):
            r = run([secret], timeout=1)
        self.assertNotIn("Secret-APKs", r["err"])
        self.assertIn("OSError", r["err"])

    def test_fix_window_rejects_bad_name_without_moving_any_window(self):
        with mock.patch.object(WIN, "move_running") as move, \
             mock.patch.object(WIN, "reset_saved_position") as reset:
            for bad in ("../escape", "a/b", "x;reboot", ""):
                r = WIN.fix_window(bad)
                self.assertFalse(r["ok"], bad)
            move.assert_not_called()
            reset.assert_not_called()

    def test_ai_sh_rejects_bad_serial_and_assist_redacts_errors(self):
        with mock.patch.object(AI, "run") as runner:
            out = AI._sh(WITH_ADB, "emulator-5554; reboot", "echo hi")
            self.assertFalse(out["ok"])
            runner.assert_not_called()
        plan = {"ok": True, "intent": "input.tap", "args": {"x": 1, "y": 2},
                "needs_device": True}
        with mock.patch.object(AI, "interpret", return_value=plan), \
             mock.patch("core.adb.tap", side_effect=RuntimeError("leak C:/secret")):
            r = AI.assist(WITH_ADB, "emulator-5554", "tap 1 2")
        self.assertFalse(r["ok"])
        self.assertNotIn("C:/secret", r["err"])
        self.assertIn("RuntimeError", r["err"])

    def test_inifile_delete_is_gone(self):
        # Dead, untested code removed in the v5 audit.
        self.assertFalse(hasattr(inifile, "delete"))

    def test_suggest_flags_is_usable_before_any_measurement(self):
        with mock.patch.object(PERF, "history", return_value={"samples": []}), \
             mock.patch.object(PERF.HOST, "snapshot",
                               return_value={"cpu_count": 8,
                                             "mem": {"total_mb": 16384}}):
            r = PERF.suggest_flags(NO_TOOLS)
        self.assertTrue(r["ok"])
        self.assertEqual(r["basis"], "host defaults")
        self.assertIn("-cores 4", r["flags"])
        self.assertIn("-memory 4096", r["flags"])  # never below the 4 GB floor
        self.assertIn("Never use Vulkan", r["note"])

    def test_confine_and_apk_path_reject_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for bad in ("../x", "..\\x", "/etc/passwd", "C:/Windows/x",
                        "a/../../x", "a\x00b"):
                self.assertIsNone(safe.confine(base, bad), bad)
            good = safe.confine(base, "nested/file.apk")
            self.assertIsNotNone(good)
            self.assertTrue(str(good).startswith(str(base.resolve())))


if __name__ == "__main__":
    unittest.main()
