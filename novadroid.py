#!/usr/bin/env python3
"""NovaDroid — best-in-class Android emulator manager for Windows.

Zero-dependency (stdlib only). Wraps the industry-best engine
(Google Android Emulator 37.x: forked QEMU + ranchu + gfxstream + WHPX)
with the competitive feature set of BlueStacks/LDPlayer/MuMu/Nox/MEmu
(multi-instance, sync-tap, macros, perf dashboard, sensor injection)
minus their ads, bloat, and stale Android bases.

Usage:
    python novadroid.py [--port 8080] [--no-browser]
    # remote access (dashboard hosted on Vercel + tunnel):
    python novadroid.py --host 0.0.0.0 --port 8080 --token <secret> --no-browser

Then open http://localhost:8080
Split architecture: the agent (this file) always runs on the machine with the
Android SDK — Vercel hosts only the static dashboard, which talks back here
over a tunnel. The emulator/QEMU itself can never live on serverless hosting.
"""
from __future__ import annotations

import argparse
import base64
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core.config import ToolPaths, DEFAULTS
from core import avd as AVD
from core import adb as ADB
from core import accel as ACCEL
from core import display as DISP
from core import window as WIN
from core import shared as SHARE
from core import compat as COMPAT
from core import apps as APPS
from core import host as HOST
from core.runner import run

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
PATHS = ToolPaths.discover()
TOKEN = ""            # set from --token; required for any non-loopback client
STRICT_TOKEN = False  # --strict-token: require the token even from localhost
VERSION = "2.0.0"     # shown in dashboard footer — if it looks old, restart the agent
MAX_BODY = 320 * 1024 * 1024  # browser uploads are base64 → ~1.37x on the wire

# Endpoints that shell out to slow SDK tools. Cached so a dashboard refresh
# (or an auto-refresh) doesn't spend 30s re-running `android sdk list`.
_TTL_CACHE: dict[str, tuple[float, dict]] = {}
_TTL_LOCK = threading.Lock()


def _cached(key: str, ttl: float, fn):
    now = time.time()
    with _TTL_LOCK:
        hit = _TTL_CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _TTL_LOCK:
        _TTL_CACHE[key] = (now, value)
    return value


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
def _client_ip(handler: SimpleHTTPRequestHandler) -> str:
    return (handler.client_address[0] if handler.client_address else "")


def _is_loopback(ip: str) -> bool:
    return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _auth_ok(handler: SimpleHTTPRequestHandler, q: dict | None = None) -> bool:
    """Gate for /api/*.

    This API can power off VMs, run adb shell and read your disk, so a remote
    client must present the token. Two deliberate allowances:

    * loopback is trusted (it is the operator's own machine) unless
      --strict-token is passed;
    * the token may arrive as ?token= — `<img src="/api/screen">` cannot set
      an Authorization header, so without this the remote dashboard could
      never show a screenshot.
    """
    if not TOKEN:
        return True
    if _is_loopback(_client_ip(handler)) and not STRICT_TOKEN:
        return True
    presented = ""
    hdr = handler.headers.get("Authorization") or ""
    if hdr.lower().startswith("bearer "):
        presented = hdr[7:].strip()
    if not presented:
        presented = (q or {}).get("token", "")
    return bool(presented) and secrets.compare_digest(presented, TOKEN)


def jread(handler: SimpleHTTPRequestHandler) -> dict:
    try:
        n = int(handler.headers.get("Content-Length", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return {}
    if n > MAX_BODY:
        handler.send_error(413, "request body too large")
        return {}
    try:
        raw = handler.rfile.read(n)
    except (OSError, ConnectionError):
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class Handler(SimpleHTTPRequestHandler):
    server_version = f"NovaDroid/{VERSION}"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def log_message(self, fmt, *args):  # quieter logs
        print("[" + self.command + "]", self.path, "-", fmt % args)

    # ---- helpers ----
    def _json(self, obj: dict, code: int = 200):
        try:
            body = json.dumps(obj, default=str).encode("utf-8")
        except (TypeError, ValueError):
            body = b'{"ok": false, "err": "response was not serialisable"}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionError):
            pass

    def _q(self) -> dict:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))

    def _unauthorized(self, path: str):
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({
            "ok": False, "err": "unauthorized — set the backend token in the "
                                "🔌 Backend bar (or pass ?token= for <img> loads)"
        }).encode())

    def _guard(self, q: dict | None = None) -> bool:
        if _auth_ok(self, q):
            return True
        self._unauthorized(urllib.parse.urlsplit(self.path).path)
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # ---------------------------------------------------------------- GET --
    def do_GET(self):
        split = urllib.parse.urlsplit(self.path)
        path, q = split.path, self._q()
        if path == "/" or path == "/index.html":
            return super().do_GET()
        if path.startswith("/api/") and not self._guard(q):
            return
        if path == "/api/health":
            return self._json({"ok": True, "app": "NovaDroid", "version": VERSION,
                               "remote": bool(TOKEN)})
        if path == "/api/info":
            return self._json(_cached("info", 120.0, self._info))
        if path == "/api/avds":
            return self._json(AVD.list_avds(PATHS))
        if path == "/api/profiles":
            return self._json(AVD.list_profiles(PATHS))
        if path == "/api/devices":
            return self._json(ADB.devices(PATHS))
        if path == "/api/jobs":
            return self._json({"ok": True, "job": AVD.job_state(q.get("name", ""))})
        if path == "/api/screen":
            return self._screen(q)
        if path == "/api/layout":
            return self._json(ADB.layout(PATHS, q.get("serial", "")))
        if path == "/api/perf":
            return self._json(ADB.perf(PATHS, q.get("serial", "")))
        if path == "/api/host":
            return self._json(_cached("host", 1.5, HOST.snapshot))
        if path == "/api/boot":
            return self._json(APPS.boot_state(PATHS, q.get("serial", "")))
        if path == "/api/macros":
            if q.get("name"):
                return self._json(ADB.macro_read(q["name"]))
            return self._json(ADB.macro_list())
        # --- app manager ---
        if path == "/api/apps":
            return self._json(APPS.list_packages(
                PATHS, q.get("serial", ""), q.get("third_party", "1") != "0"))
        if path == "/api/apps/info":
            return self._json(APPS.package_info(PATHS, q.get("serial", ""),
                                                q.get("package", "")))
        if path == "/api/logcat":
            return self._json(APPS.logcat(PATHS, q.get("serial", ""),
                                          int(q.get("lines", 200) or 200),
                                          q.get("spec", "")))
        if path == "/api/clipboard":
            return self._json(APPS.clipboard_get(PATHS, q.get("serial", "")))
        # --- recording ---
        if path == "/api/record":
            return self._json(APPS.record_state(q.get("serial", "")))
        if path == "/api/record/download":
            return self._download(q.get("file", ""))
        # --- sensors / network ---
        if path == "/api/sensors":
            return self._json(APPS.sensor_status(PATHS, q.get("serial", "")))
        if path == "/api/network":
            return self._json(APPS.network_status(PATHS, q.get("serial", "")))
        # --- snapshots (quickboot) ---
        if path == "/api/snapshots":
            return self._json(AVD.snapshots(PATHS, q.get("serial", "")))
        # --- display / window / shared / compat ---
        if path == "/api/display/presets":
            return self._json(DISP.presets())
        if path == "/api/display":
            return self._json(DISP.get_display(PATHS, q.get("serial", "")))
        if path == "/api/skins":
            return self._json(DISP.list_skins(PATHS))
        if path == "/api/avds/window":
            return self._json(WIN.window_state(q.get("name", "")))
        if path == "/api/shared":
            return self._json(SHARE.status())
        if path == "/api/shared/device":
            return self._json(SHARE.list_device(PATHS, q.get("serial", "")))
        if path == "/api/shared/autosync":
            return self._json(SHARE.autosync_status())
        return super().do_GET() if not path.startswith("/api/") else \
            self._json({"ok": False, "err": f"unknown GET {path}"}, 404)

    def _info(self) -> dict:
        acc = ACCEL.accel_status(PATHS)
        sdk_pkgs: dict = {}
        if PATHS.android_cli:
            r = run([PATHS.android_cli, "sdk", "list"], timeout=90)
            sdk_pkgs = {"raw": (r["out"] or r["err"])[:3000]}
        return {
            "ok": True, "version": VERSION,
            "sdk": str(PATHS.sdk) if PATHS.sdk else None,
            "android_cli": PATHS.android_cli, "adb": PATHS.adb,
            "emulator": PATHS.emulator, "defaults": DEFAULTS,
            "accel": acc, "sdk_packages": sdk_pkgs,
            "host": {"cpu_count": HOST.cpu_count(),
                     "mem_total_mb": HOST.memory().get("total_mb")},
        }

    def _screen(self, q: dict):
        serial = q.get("serial", "")
        if not serial or not PATHS.adb:
            return self._json({"ok": False, "err": "serial + adb required"}, 400)
        s = ADB.screenshot(PATHS, serial)
        if not s.get("ok"):
            return self._json(s, 502)
        raw = base64.b64decode(s["png_b64"])
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-NovaDroid-Via", s.get("via", "?"))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionError):
            pass

    def _download(self, name: str):
        """Stream a recorded mp4 back to the browser (path-confined)."""
        base = APPS.REC_DIR
        target = (base / Path(name).name)
        try:
            if base.resolve() not in target.resolve().parents or not target.is_file():
                return self._json({"ok": False, "err": "file not found"}, 404)
        except OSError:
            return self._json({"ok": False, "err": "file not found"}, 404)
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{target.name}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionError):
            pass

    # --------------------------------------------------------------- POST --
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/") and not self._guard():
            return
        b = jread(self)
        if path == "/api/avds":
            return self._json(AVD.create_avd(PATHS, b.get("profile", "medium_phone")))
        if path == "/api/avds/start":
            return self._json(AVD.start_avd(PATHS, b.get("name", ""), bool(b.get("cold"))))
        if path == "/api/avds/start-raw":
            extra = b.get("extra", [])
            if isinstance(extra, str):
                extra = extra.split()
            return self._json(AVD.start_avd_raw(PATHS, b.get("name", ""), extra))
        if path == "/api/avds/stop":
            return self._json(AVD.stop_avd(PATHS, b.get("name", "")))
        if path == "/api/avds/wipe":
            return self._json(AVD.wipe_data(b.get("name", "")))
        if path == "/api/avds/clone":
            return self._json(AVD.clone_avd(PATHS, b.get("src", ""),
                                            b.get("name", "") or b.get("dst", "")))
        if path == "/api/fleet":
            return self._json(AVD.fleet(PATHS, b.get("names") or [],
                                        b.get("action", "start"),
                                        bool(b.get("cold")),
                                        int(b.get("stagger", 8) or 8)))
        if path == "/api/snapshots":
            return self._json(AVD.snapshot(PATHS, b.get("serial", ""),
                                           b.get("action", ""), b.get("name", "")))
        if path == "/api/apk/install":
            return self._json(ADB.install_apk(PATHS, b.get("serial", ""),
                                              b.get("apk_path", "")))
        if path == "/api/geo":
            return self._json(ADB.geo_fix(PATHS, b.get("serial", ""),
                                          float(b.get("lat", 27.7172)),
                                          float(b.get("lng", 85.3240))))
        if path == "/api/battery":
            return self._json(ADB.battery(PATHS, b.get("serial", ""),
                                          int(b.get("level", 85)),
                                          b.get("status", "not-charging")))
        if path == "/api/battery/reset":
            return self._json(ADB.battery_reset(PATHS, b.get("serial", "")))
        if path == "/api/shell":
            return self._json(ADB.shell(PATHS, b.get("serial", ""),
                                        b.get("cmd", "echo hi")))
        # --- interactive input (live-view tap / swipe / type) ---
        if path == "/api/input/tap":
            return self._json(ADB.tap(PATHS, b.get("serial", ""),
                                      b.get("x", 0), b.get("y", 0)))
        if path == "/api/input/swipe":
            return self._json(ADB.swipe(PATHS, b.get("serial", ""),
                                        b.get("x1", 0), b.get("y1", 0),
                                        b.get("x2", 0), b.get("y2", 0),
                                        b.get("ms", 300)))
        if path == "/api/input/text":
            return self._json(ADB.input_text(PATHS, b.get("serial", ""),
                                             b.get("text", "")))
        if path == "/api/input/key":
            return self._json(ADB.keyevent(PATHS, b.get("serial", ""),
                                           b.get("code", 0)))
        # --- macros ---
        if path == "/api/macros":
            return self._json(ADB.macro_save(b.get("name", "macro"),
                                             b.get("steps", [])))
        if path == "/api/macros/run":
            return self._json(ADB.macro_run(PATHS, b.get("serial", ""),
                                            b.get("name", "")))
        # --- app manager ---
        if path == "/api/apps/launch":
            return self._json(APPS.launch(PATHS, b.get("serial", ""),
                                          b.get("package", "")))
        if path == "/api/apps/stop":
            return self._json(APPS.stop_app(PATHS, b.get("serial", ""),
                                            b.get("package", "")))
        if path == "/api/apps/clear":
            return self._json(APPS.clear_app(PATHS, b.get("serial", ""),
                                             b.get("package", "")))
        if path == "/api/apps/uninstall":
            return self._json(APPS.uninstall(PATHS, b.get("serial", ""),
                                             b.get("package", "")))
        if path == "/api/apps/grant":
            return self._json(APPS.grant_all(PATHS, b.get("serial", ""),
                                             b.get("package", "")))
        if path == "/api/logcat/clear":
            return self._json(APPS.logcat_clear(PATHS, b.get("serial", "")))
        if path == "/api/clipboard":
            return self._json(APPS.clipboard_set(PATHS, b.get("serial", ""),
                                                 b.get("text", "")))
        # --- recording ---
        if path == "/api/record/start":
            return self._json(APPS.record_start(PATHS, b.get("serial", ""),
                                                int(b.get("seconds", 30) or 30),
                                                int(b.get("bitrate", 8) or 8)))
        if path == "/api/record/stop":
            return self._json(APPS.record_stop(PATHS, b.get("serial", "")))
        # --- sensors / radio / biometrics ---
        if path == "/api/sensor":
            vals = b.get("values")
            if isinstance(vals, str):
                vals = [v for v in vals.replace(",", ":").split(":") if v.strip()]
            return self._json(APPS.sensor_set(PATHS, b.get("serial", ""),
                                              b.get("kind", ""), vals or []))
        if path == "/api/network":
            return self._json(APPS.network(PATHS, b.get("serial", ""),
                                           b.get("speed", ""), b.get("delay", "")))
        if path == "/api/biometric":
            return self._json(APPS.fingerprint(PATHS, b.get("serial", ""),
                                               bool(b.get("press", True))))
        if path == "/api/sms":
            return self._json(APPS.send_sms(PATHS, b.get("serial", ""),
                                            b.get("number", ""), b.get("text", "")))
        if path == "/api/call":
            return self._json(APPS.gsm_call(PATHS, b.get("serial", ""),
                                            b.get("number", "")))
        if path == "/api/signal":
            return self._json(APPS.gsm_signal(PATHS, b.get("serial", ""),
                                              int(b.get("level", 4))))
        if path == "/api/power":
            return self._json(APPS.power_toggle(PATHS, b.get("serial", "")))
        if path == "/api/rotate":
            return self._json(APPS.emu_rotate(PATHS, b.get("serial", "")))
        # --- display ---
        if path == "/api/display/apply":
            return self._json(DISP.apply_display(PATHS, b.get("serial", ""),
                                                 b.get("w", 0), b.get("h", 0),
                                                 b.get("dpi", 0)))
        if path == "/api/display/reset":
            return self._json(DISP.reset_display(PATHS, b.get("serial", "")))
        if path == "/api/display/orientation":
            return self._json(DISP.set_orientation(PATHS, b.get("serial", ""),
                                                   b.get("mode", "")))
        if path == "/api/display/save":
            return self._json(DISP.save_to_avd(b.get("name", ""),
                                               b.get("w", 0), b.get("h", 0),
                                               b.get("dpi", 0)))
        if path == "/api/avds/skin":
            return self._json(DISP.set_skin(PATHS, b.get("name", ""),
                                            b.get("skin", "")))
        if path == "/api/avds/window/fix":
            return self._json(WIN.fix_window(b.get("name", "")))
        # --- shared folder ---
        if path == "/api/shared/upload":
            return self._json(SHARE.upload(b.get("name", ""), b.get("b64", ""),
                                           PATHS, b.get("push_serial", "")))
        if path == "/api/shared/push":
            return self._json(SHARE.push(PATHS, b.get("serial", ""), b.get("files")))
        if path == "/api/shared/pull":
            return self._json(SHARE.pull(PATHS, b.get("serial", ""), b.get("files")))
        if path == "/api/shared/autosync":
            return self._json(SHARE.autosync_set(PATHS, b.get("serial", ""),
                                                 bool(b.get("on")),
                                                 int(b.get("interval", 15) or 15)))
        if path == "/api/shared/sync":
            return self._json(SHARE.sync_now(PATHS, b.get("serial", "")))
        if path == "/api/shared/mirror":
            return self._json(SHARE.mirror(PATHS, b.get("serial", ""),
                                           b.get("direction", "")))
        if path == "/api/shared/delete-pc":
            return self._json(SHARE.delete_pc(b.get("files", [])))
        if path == "/api/shared/delete-device":
            return self._json(SHARE.delete_device(PATHS, b.get("serial", ""),
                                                  b.get("files", [])))
        if path == "/api/shared/delete":
            return self._json(SHARE.delete_everywhere(PATHS, b.get("serial", ""),
                                                      b.get("files", [])))
        if path == "/api/shared/reveal":
            return self._json(SHARE.reveal())
        if path == "/api/shared/export":
            return self._json(SHARE.export_to_desktop(PATHS, b.get("serial", ""),
                                                      b.get("files", []),
                                                      b.get("source", "device")))
        if path == "/api/apk/analyze":
            return self._json(COMPAT.check(PATHS, b.get("apk_path", ""),
                                           b.get("serial", "")))
        if path == "/api/sync-tap":
            return self._json(sync_tap(b.get("taps", [])))
        return self._json({"ok": False, "err": f"unknown POST {path}"}, 404)

    # ------------------------------------------------------------- DELETE --
    def do_DELETE(self):
        path = urllib.parse.urlsplit(self.path).path
        q = self._q()
        if path.startswith("/api/") and not self._guard(q):
            return
        if path == "/api/avds":
            return self._json(AVD.remove_avd(PATHS, q.get("name", "")))
        if path == "/api/macros":
            return self._json(ADB.macro_delete(q.get("name", "")))
        return self._json({"ok": False, "err": "unknown DELETE"}, 404)


# --------------------------------------------------------------------------
# Operation synchronizer (LDPlayer-class): one input mirrored to N instances
# --------------------------------------------------------------------------
def sync_tap(taps) -> dict:
    """Mirror one tap to every instance — but do it in PARALLEL.

    The previous version looped serially while claiming to be a
    "synchronizer": with 4 instances the last tap landed ~600ms after the
    first, which is very visible when you are farming. Uses one adb process
    per device, concurrently, and validates input instead of KeyError-ing.
    """
    from concurrent.futures import ThreadPoolExecutor

    if not PATHS.adb:
        return {"ok": False, "err": "adb not found"}
    items = []
    for t in (taps or []):
        if not isinstance(t, dict):
            continue
        serial = str(t.get("serial", "")).strip()
        if not re_serial(serial):
            continue
        items.append((serial, t.get("x", 0), t.get("y", 0)))
    if not items:
        return {"ok": False, "err": "no valid targets (pick online emulator devices)"}

    def _one(item):
        serial, x, y = item
        r = ADB.tap(PATHS, serial, x, y)
        return {"serial": serial, "ok": bool(r.get("ok"))}

    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        results = list(pool.map(_one, items))
    return {"ok": all(r["ok"] for r in results), "results": results,
            "count": len(results), "parallel": True}


def re_serial(value: str) -> bool:
    """Accept real hardware serials too (USB devices, not just emulators)."""
    return bool(value) and all(c.isalnum() or c in "._:-" for c in value)


# --------------------------------------------------------------------------
def main():
    global TOKEN, STRICT_TOKEN
    ap = argparse.ArgumentParser(description="NovaDroid emulator manager")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; use 0.0.0.0 for tunnel/remote access (requires --token)")
    ap.add_argument("--token", default="",
                    help="Bearer token required by remote clients (Vercel dashboard + tunnel)")
    ap.add_argument("--strict-token", action="store_true",
                    help="also require the token from localhost (default: localhost is trusted)")
    ap.add_argument("--gen-token", action="store_true",
                    help="print a fresh token and exit")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    if args.gen_token:
        print(secrets.token_urlsafe(24))
        return
    TOKEN = args.token
    STRICT_TOKEN = args.strict_token
    remote = args.host not in ("127.0.0.1", "localhost", "::1")
    if remote and not TOKEN:
        print("REFUSED: --host exposes adb-shell power to the network; "
              "restart with --token <secret>.")
        print("  Generate one:  python novadroid.py --gen-token")
        raise SystemExit(2)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    url = f"http://{'localhost' if args.host == '0.0.0.0' else args.host}:{args.port}"
    print(f"NovaDroid {VERSION} running at {url}  "
          f"(auth={'token' if TOKEN else 'localhost-open'})")
    print(f"  SDK:      {PATHS.sdk}")
    print(f"  adb:      {PATHS.adb}")
    print(f"  emulator: {PATHS.emulator}")
    if not PATHS.adb:
        print("  !! adb not found — set ANDROID_SDK_ROOT or install platform-tools")
    if not args.no_browser and not remote:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        try:
            srv.server_close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
