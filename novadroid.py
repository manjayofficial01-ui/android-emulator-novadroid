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
import urllib.parse
import webbrowser
from functools import partial
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
from core.runner import run

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
PATHS = ToolPaths.discover()
TOKEN = ""  # set from --token; required for any non-loopback client


def _client_ip(handler: SimpleHTTPRequestHandler) -> str:
    return (handler.client_address[0] if handler.client_address else "")


def _is_loopback(ip: str) -> bool:
    return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _auth_ok(handler: SimpleHTTPRequestHandler) -> bool:
    """Gate for /api/*. Localhost stays open (old behavior); remote needs the token.

    The API can power off VMs, run adb shell, and read your disk — exposing it
    without a token would hand that to the whole internet via the tunnel.
    """
    if _is_loopback(_client_ip(handler)) and not TOKEN:
        return True
    if TOKEN and handler.headers.get("Authorization") == f"Bearer {TOKEN}":
        return True
    return False


def jread(handler: SimpleHTTPRequestHandler) -> dict:
    try:
        n = int(handler.headers.get("Content-Length", 0) or 0)
    except ValueError:
        n = 0
    if not n:
        return {}
    try:
        return json.loads(handler.rfile.read(n).decode("utf-8") or "{}")
    except Exception:
        return {}


class Handler(SimpleHTTPRequestHandler):
    server_version = "NovaDroid/1.0"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def log_message(self, fmt, *args):  # quieter logs
        print("[" + self.command + "]", self.path, "-", fmt % args)

    # ---- helpers ----
    def _json(self, obj: dict, code: int = 200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _q(self) -> dict:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        q = self._q()
        if path == "/" or path == "/index.html":
            return super().do_GET()
        if path.startswith("/static/"):
            self.path = path[len("/static"):] or "/"
            return super().do_GET()
        if path.startswith("/api/") and not _auth_ok(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            self.wfile.write('{"ok": false, "err": "unauthorized - set the backend token in dashboard settings"}'.encode())
            return
        if path == "/api/health":
            return self._json({"ok": True, "app": "NovaDroid", "version": "1.1.0",
                               "remote": bool(TOKEN)})
        if path == "/api/info":
            acc = ACCEL.accel_status(PATHS)
            sdk_pkgs: dict = {}
            if PATHS.android_cli:
                r = run([PATHS.android_cli, "sdk", "list"], timeout=60)
                sdk_pkgs = {"raw": (r["out"] or r["err"])[:3000]}
            return self._json({
                "ok": True,
                "sdk": str(PATHS.sdk) if PATHS.sdk else None,
                "android_cli": PATHS.android_cli, "adb": PATHS.adb,
                "emulator": PATHS.emulator, "defaults": DEFAULTS,
                "accel": acc, "sdk_packages": sdk_pkgs})
        if path == "/api/avds":
            return self._json(AVD.list_avds(PATHS))
        if path == "/api/profiles":
            return self._json(AVD.list_profiles(PATHS))
        if path == "/api/devices":
            return self._json(ADB.devices(PATHS))
        if path == "/api/jobs":
            return self._json({"ok": True, "job": AVD.job_state(q.get("name", ""))})
        if path == "/api/screen":
            serial = q.get("serial", "")
            if not serial or not PATHS.adb:
                return self._json({"ok": False, "err": "serial + adb required"}, 400)
            s = ADB.screenshot(PATHS, serial)
            if not s.get("ok"):
                return self._json(s, 502)
            raw = base64.b64decode(s["png_b64"])
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/layout":
            return self._json(ADB.layout(PATHS, q.get("serial", "")))
        if path == "/api/perf":
            return self._json(ADB.perf(PATHS, q.get("serial", "")))
        if path == "/api/macros":
            return self._json(ADB.macro_list())
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

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/") and not _auth_ok(self):
            return self._json({"ok": False, "err": "unauthorized"}, 401)
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
        if path == "/api/apk/install":
            return self._json(ADB.install_apk(PATHS, b.get("serial", ""), b.get("apk_path", "")))
        if path == "/api/geo":
            return self._json(ADB.geo_fix(PATHS, b.get("serial", ""), float(b.get("lat", 27.7172)), float(b.get("lng", 85.3240))))
        if path == "/api/battery":
            return self._json(ADB.battery(PATHS, b.get("serial", ""), int(b.get("level", 85))))
        if path == "/api/shell":
            return self._json(ADB.shell(PATHS, b.get("serial", ""), b.get("cmd", "echo hi")))
        if path == "/api/macros":
            return self._json(ADB.macro_save(b.get("name", "macro"), b.get("steps", [])))
        if path == "/api/macros/run":
            return self._json(ADB.macro_run(PATHS, b.get("serial", ""), b.get("name", "")))
        if path == "/api/display/apply":
            return self._json(DISP.apply_display(PATHS, b.get("serial", ""),
                                                 b.get("w", 0), b.get("h", 0), b.get("dpi", 0)))
        if path == "/api/display/reset":
            return self._json(DISP.reset_display(PATHS, b.get("serial", "")))
        if path == "/api/display/orientation":
            return self._json(DISP.set_orientation(PATHS, b.get("serial", ""), b.get("mode", "")))
        if path == "/api/display/save":
            return self._json(DISP.save_to_avd(b.get("name", ""),
                                               b.get("w", 0), b.get("h", 0), b.get("dpi", 0)))
        if path == "/api/avds/skin":
            return self._json(DISP.set_skin(PATHS, b.get("name", ""), b.get("skin", "")))
        if path == "/api/avds/window/fix":
            return self._json(WIN.fix_window(b.get("name", "")))
        if path == "/api/shared/upload":
            return self._json(SHARE.upload(b.get("name", ""), b.get("b64", ""),
                                           PATHS, b.get("push_serial", "")))
        if path == "/api/shared/push":
            return self._json(SHARE.push(PATHS, b.get("serial", ""), b.get("files")))
        if path == "/api/shared/pull":
            return self._json(SHARE.pull(PATHS, b.get("serial", ""), b.get("files")))
        if path == "/api/shared/autosync":
            return self._json(SHARE.autosync_set(PATHS, b.get("serial", ""),
                                                 bool(b.get("on")), int(b.get("interval", 15))))
        if path == "/api/shared/reveal":
            return self._json(SHARE.reveal())
        if path == "/api/apk/analyze":
            return self._json(COMPAT.check(PATHS, b.get("apk_path", ""), b.get("serial", "")))
        if path == "/api/sync-tap":
            # Operation synchronizer: one tap mirrored to N instances (LDPlayer-style).
            taps = b.get("taps", [])
            res = []
            for t in taps:
                r = run([PATHS.adb, "-s", t["serial"], "shell", "input", "tap",
                         str(t["x"]), str(t["y"])], timeout=10)
                res.append({"serial": t["serial"], "ok": r["ok"]})
            # run in parallel for real sync feel
            return self._json({"ok": True, "results": res})
        return self._json({"ok": False, "err": f"unknown POST {path}"}, 404)

    def do_DELETE(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/") and not _auth_ok(self):
            return self._json({"ok": False, "err": "unauthorized"}, 401)
        if path == "/api/avds":
            name = self._q().get("name", "")
            return self._json(AVD.remove_avd(PATHS, name))
        return self._json({"ok": False, "err": "unknown DELETE"}, 404)


def main():
    global TOKEN
    ap = argparse.ArgumentParser(description="NovaDroid emulator manager")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; use 0.0.0.0 for tunnel/remote access (requires --token)")
    ap.add_argument("--token", default="",
                    help="Bearer token required by remote clients (Vercel dashboard + tunnel)")
    ap.add_argument("--gen-token", action="store_true",
                    help="print a fresh token and exit")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    if args.gen_token:
        print(secrets.token_urlsafe(24))
        return
    TOKEN = args.token
    remote = args.host not in ("127.0.0.1", "localhost", "::1")
    if remote and not TOKEN:
        print("REFUSED: --host exposes adb-shell power to the network; restart with --token <secret>.")
        print("  Generate one:  python novadroid.py --gen-token")
        raise SystemExit(2)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"NovaDroid running at {url}  (auth={'token' if TOKEN else 'localhost-open'})")
    print(f"  SDK: {PATHS.sdk}")
    print(f"  adb: {PATHS.adb}")
    print(f"  emulator: {PATHS.emulator}")
    if not args.no_browser and not remote:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
