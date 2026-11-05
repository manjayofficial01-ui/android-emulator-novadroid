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
    NOVADROID_TOKEN=<secret> python novadroid.py --host 0.0.0.0 --port 8080 \
        --origin https://your-dashboard.vercel.app --no-browser

Then open http://localhost:8080
Split architecture: the agent (this file) always runs on the machine with the
Android SDK — Vercel hosts only the static dashboard, which talks back here
over a tunnel. The emulator/QEMU itself can never live on serverless hosting.

v5.0.0 security model
---------------------
* If a token is configured, EVERY /api/* request needs it as
  `Authorization: Bearer ...`. There is NO loopback exemption: tunnel traffic
  arrives as 127.0.0.1, so the old `_is_loopback()` shortcut was a complete
  remote auth bypass.
* `?token=` is gone. Tokens in URLs leak into browser history, Referer
  headers, proxy logs and this server's own access log.
* The three binary GETs that cannot set a header (<img>/<video>/download)
  use a short-lived, header-minted `?ticket=` instead — 300 s, scoped to
  those three paths, never accepted anywhere else.
* CORS is an explicit origin allowlist (--origin), not `*`. Cross-site
  requests without an allowed Origin are refused (CSRF / DNS rebinding).
* `/api/shell` and `/api/shared/reveal` are disabled unless the operator
  explicitly starts the agent with --allow-shell / --allow-gui.
* /api/health stays public (no secrets) so the dashboard can show "offline".
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
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
from core import perf as PERF
from core import ai as AI
from core import ports as PORTS
from core import engine as ENGINE
from core import environment as ENVIRON
from core import stream as STREAM
from core import gpu as GPU
from core import agentskill as AGENTSKILL
from core import safe

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
PATHS = ToolPaths.discover()
TOKEN = ""             # set from --token / NOVADROID_TOKEN / --token-file
ALLOW_SHELL = False    # --allow-shell: opt-in arbitrary `adb shell`
ALLOW_GUI = False      # --allow-gui: opt-in launching Explorer on the host
ALLOWED_ORIGINS: frozenset[str] = frozenset()
ALLOWED_HOSTS: frozenset[str] = frozenset()
STRICT_HOST = False
VERSION = "5.1.0"
MAX_BODY = 320 * 1024 * 1024  # browser uploads are base64 -> ~1.37x on the wire
REQUEST_TIMEOUT = 30          # slowloris guard for request line + body

# Binary/image endpoints that may authenticate with a short-lived ticket
# because <img>, <video> and download links cannot set an Authorization header.
TICKET_PATHS = frozenset({"/api/screen", "/api/screen/stream",
                          "/api/record/download"})
TICKET_TTL = 300.0
TICKET_MAX = 128
_TICKETS: dict[str, float] = {}
_TICKETS_LOCK = threading.Lock()

# Endpoints that shell out to slow SDK tools. Cached so a dashboard refresh
# (or an auto-refresh) doesn't spend 30s re-running `android sdk list`.
_TTL_CACHE: dict[str, tuple[float, dict]] = {}
_TTL_LOCK = threading.Lock()
_INFLIGHT: dict[str, threading.Event] = {}

# Host names this server is genuinely served under. DNS rebinding pages
# control Host/Origin by construction, so anything outside this set fails
# the same-origin fallback and (tokenless) the Host check outright.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _host_name(raw: str) -> str:
    """Hostname without the port, handling IPv6 brackets ([::1]:8080)."""
    raw = (raw or "").strip().lower()
    if raw.startswith("["):
        return raw[1:].split("]")[0]
    return raw.split(":")[0]


def _mint_ticket() -> str:
    """Issue a 300 s ticket, pruning expired / excess ones."""
    t = secrets.token_urlsafe(32)
    now = time.time()
    with _TICKETS_LOCK:
        for k, exp in list(_TICKETS.items()):
            if exp <= now:
                _TICKETS.pop(k, None)
        while len(_TICKETS) >= TICKET_MAX:
            _TICKETS.pop(next(iter(_TICKETS)), None)
        _TICKETS[t] = now + TICKET_TTL
    return t


def _ticket_ok(ticket: str) -> bool:
    """Constant-time ticket check + lazy expiry sweep."""
    if not ticket or len(ticket) > 200:
        return False
    now = time.time()
    with _TICKETS_LOCK:
        for k, exp in list(_TICKETS.items()):
            if exp <= now:
                _TICKETS.pop(k, None)
        return any(exp > now and secrets.compare_digest(k, ticket)
                   for k, exp in _TICKETS.items())


def _cached(key: str, ttl: float, fn):
    """Cache `fn()` for `ttl` seconds, with single-flight for concurrent misses.

    v5: the old version released the lock before calling `fn()`, so N
    concurrent dashboard refreshes all ran the same 30-second `android sdk
    list` in parallel. The first caller owns the key; the rest wait on its
    Event and then read the value it published.
    """
    with _TTL_LOCK:
        hit = _TTL_CACHE.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        ev = _INFLIGHT.get(key)
        owner = ev is None
        if owner:
            ev = threading.Event()
            _INFLIGHT[key] = ev
    if not owner:
        ev.wait(timeout=90)
        with _TTL_LOCK:
            hit = _TTL_CACHE.get(key)
        if hit:
            return hit[1]
        return fn()          # owner failed or is still running -> do not hang
    try:
        value = fn()
        with _TTL_LOCK:
            _TTL_CACHE[key] = (time.time(), value)
        return value
    finally:
        with _TTL_LOCK:
            _INFLIGHT.pop(key, None)
        ev.set()


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
def _bearer(handler: SimpleHTTPRequestHandler) -> str:
    hdr = handler.headers.get("Authorization") or ""
    return hdr[7:].strip() if hdr.lower().startswith("bearer ") else ""


def _auth_ok(handler: SimpleHTTPRequestHandler, path: str,
             q: dict | None = None) -> bool:
    """Gate for /api/*.

    There is intentionally NO loopback exemption. Cloudflare Tunnel (and any
    reverse proxy) connects to 127.0.0.1, so trusting loopback means trusting
    the entire internet. `?token=` is deliberately gone as well.

    In local mode (no token configured) the server is bound to loopback and
    open — that is the explicitly chosen, documented default.
    """
    if not TOKEN:
        return True
    presented = _bearer(handler)
    try:
        # compare_digest(str) is ASCII-only; a non-ASCII header or token used
        # to raise TypeError and turn every request into a 500.
        ok = bool(presented) and secrets.compare_digest(
            presented.encode("utf-8"), TOKEN.encode("utf-8"))
    except (UnicodeEncodeError, TypeError):
        ok = False
    if ok:
        return True
    if path in TICKET_PATHS:
        return _ticket_ok((q or {}).get("ticket", ""))
    return False


def jread(handler: SimpleHTTPRequestHandler) -> dict | None:
    """Read + parse the JSON body. Returns {} for an empty body.

    Returns None when the request must NOT continue — the error response has
    already been written. v5 also enforces `Content-Type: application/json`,
    which stops <form>-based CSRF payloads and malformed uploads from ever
    reaching a route handler.
    """
    try:
        n = int(handler.headers.get("Content-Length", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return {}
    if n > MAX_BODY:
        handler._json({"ok": False, "err": "request body too large"}, 413)
        return None
    ctype = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        handler._json({"ok": False, "err": "expected Content-Type: application/json"},
                      415)
        return None
    try:
        raw = handler.rfile.read(n)
    except (OSError, ConnectionError, socket.timeout):
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class Handler(SimpleHTTPRequestHandler):
    # "Server:" deliberately carries no version (fingerprinting aid).
    server_version = "NovaDroid"
    sys_version = ""

    def __init__(self, *a, **kw):
        self._headers_sent = False
        super().__init__(*a, directory=str(STATIC), **kw)

    def setup(self):
        super().setup()
        # A slow client can otherwise hold a ThreadingHTTPServer worker open
        # forever by sending one byte every few minutes.
        self.connection.settimeout(REQUEST_TIMEOUT)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (socket.timeout, TimeoutError):
            self.close_connection = True
        except OSError:
            self.close_connection = True

    def send_response(self, code, message=None):
        self._headers_sent = True
        super().send_response(code, message)

    def end_headers(self):
        # Security headers on every response. CSP only for HTML (the static
        # dashboard); MJPEG/PNG/JSON would simply ignore it, but scoping it
        # keeps tunnel configs from needing 'unsafe-inline' elsewhere. HTML
        # also revalidates every load so an agent upgrade is picked up
        # immediately (assets carry ?v= cache-busting query strings).
        try:
            buf = getattr(self, "_headers_buffer", None)
            ctype = ""
            if buf:
                for line in buf:
                    if line.lower().startswith(b"content-type:"):
                        ctype = line.decode("latin-1").lower()
                        break
            if "text/html" in ctype:
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; img-src 'self' data: blob:; "
                                 "style-src 'self' 'unsafe-inline'; "
                                 "connect-src 'self'; frame-ancestors 'self'")
                self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "no-referrer")
        except Exception:
            pass  # headers are hardening, never worth failing a response over
        super().end_headers()

    def log_request(self, code="-", size="-"):
        # The base implementation logs the full request line — including the
        # ?ticket= on binary views. Log path + status only.
        print("[" + self.command + "]", urllib.parse.urlsplit(self.path).path,
              "-", code)

    def log_message(self, fmt, *args):  # quieter logs; NEVER log the query
        print("[" + self.command + "]",
              urllib.parse.urlsplit(self.path).path, "-", fmt % args)

    # ---- helpers ----
    def _cors(self):
        origin = self.headers.get("Origin") or ""
        if origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _json(self, obj: dict, code: int = 200):
        try:
            body = json.dumps(obj, default=str).encode("utf-8")
        except (TypeError, ValueError):
            body = b'{"ok": false, "err": "response was not serialisable"}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def _q(self) -> dict:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))

    def _unauthorized(self):
        body = json.dumps({
            "ok": False,
            "err": "unauthorized — paste the backend token in the 🔌 "
                   "Backend bar (binary views use a session ticket)"}).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin") or ""
        if origin:
            if origin in ALLOWED_ORIGINS:
                return True
            # Browsers DO send Origin on same-origin POSTs. Comparing the
            # origin's authority to the Host header keeps the local dashboard
            # working with an empty --origin allowlist — but a DNS-rebinding
            # page controls BOTH of those values, so the fallback is only
            # trusted when the Host itself is one we actually serve
            # (loopback, or an explicitly allowed host). v5.0.0 accepted any
            # Host here, which let rebind.evil.com → 127.0.0.1 read the whole
            # tokenless API from a browser.
            u = urllib.parse.urlsplit(origin)
            host = _host_name(self.headers.get("Host") or "")
            return bool(u.netloc) and u.netloc.lower() == (
                self.headers.get("Host") or "").lower() and host in (
                _LOOPBACK_HOSTS | ALLOWED_HOSTS)
        # Modern browsers send Sec-Fetch-Site: cross-site on cross-origin
        # requests that omit Origin. Refuse those (CSRF / DNS rebinding).
        return (self.headers.get("Sec-Fetch-Site") or "").lower() != "cross-site"

    def _host_ok(self) -> bool:
        # Tokenless mode is the DNS-rebinding sweet spot (no auth to defeat),
        # so the Host header is always validated there. With a token set the
        # Bearer check is the real gate; --strict-host still turns this on.
        if STRICT_HOST or not TOKEN:
            raw = self.headers.get("Host") or ""
            host = _host_name(raw)
            return host in (_LOOPBACK_HOSTS | ALLOWED_HOSTS)
        return True

    def _guard(self, q: dict | None, path: str) -> bool:
        if not self._origin_ok():
            self._json({"ok": False, "err": "origin not allowed — add it with --origin"},
                       403)
            return False
        if not self._host_ok():
            self._json({"ok": False, "err": "host not allowed"}, 403)
            return False
        if not _auth_ok(self, path, q):
            self._unauthorized()
            return False
        return True

    def _server_error(self, ex: Exception):
        """Log the detail locally; never leak paths/tracebacks to a client."""
        print(f"[{self.command}] internal error ({type(ex).__name__}): {ex}")
        if not self._headers_sent:
            try:
                self._json({"ok": False, "err": "internal agent error — see console"},
                           500)
            except Exception:
                pass

    def do_OPTIONS(self):
        if not self._origin_ok():
            return self._json({"ok": False, "err": "origin not allowed — add it with --origin"},
                              403)
        origin = self.headers.get("Origin") or ""
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------------------------------------------------------------- GET --
    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionError, ConnectionResetError):
            pass
        except Exception as ex:
            self._server_error(ex)

    def _get(self):
        split = urllib.parse.urlsplit(self.path)
        path, q = split.path, self._q()
        if path == "/" or path == "/index.html":
            return super().do_GET()
        # Health is deliberately public so the dashboard can distinguish
        # "agent offline" from "bad token" — but the authenticated detail
        # (version, feature flags) is only for callers that passed the gate;
        # over a tunnel it made target fingerprinting free.
        if path == "/api/health":
            if _auth_ok(self, path, q):
                return self._json({"ok": True, "app": "NovaDroid",
                                   "version": VERSION,
                                   "auth": "token" if TOKEN else "open",
                                   "remote": bool(TOKEN), "secure": bool(TOKEN),
                                   "features": {"shell": ALLOW_SHELL,
                                                "gui": ALLOW_GUI,
                                                "tickets": bool(TOKEN)}})
            return self._json({"ok": True, "app": "NovaDroid",
                               "auth": "token" if TOKEN else "open"})
        if path.startswith("/api/") and not self._guard(q, path):
            return
        if path == "/api/info":
            return self._json(_cached("info", 120.0, self._info))
        if path == "/api/engine":
            return self._json(_cached("engine", 60.0,
                                      lambda: ENGINE.report(PATHS)))
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
        if path == "/api/screen/stream":
            return self._stream(q)
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
                                          _int(q.get("lines", 200), 200),
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
        if path == "/api/avds/environment":
            return self._json(ENVIRON.get(q.get("name", "")))
        # --- AVD toolkit (disk usage) + port forwarding ---
        if path == "/api/avds/disk":
            return self._json(AVD.disk_usage(q.get("name", "")))
        if path == "/api/ports":
            return self._json(PORTS.forwards(PATHS, q.get("serial", "")))
        if path == "/api/ports/reverse":
            return self._json(PORTS.reverses(PATHS, q.get("serial", "")))
        if path == "/api/refresh-rates":
            return self._json({"ok": True, "rates": list(DISP.REFRESH_RATES)})
        if path == "/api/shared":
            return self._json(SHARE.status())
        if path == "/api/shared/device":
            return self._json(SHARE.list_device(PATHS, q.get("serial", "")))
        if path == "/api/shared/autosync":
            return self._json(SHARE.autosync_status())
        # --- smoothness engine ---
        if path == "/api/smoothness":
            return self._json(PERF.measure(PATHS, q.get("serial", ""),
                                           q.get("package", "")))
        if path == "/api/smoothness/diagnose":
            return self._json(PERF.diagnose(PATHS, q.get("serial", ""),
                                            q.get("package", "")))
        if path == "/api/smoothness/history":
            return self._json(PERF.history(_int(q.get("limit", 60), 60)))
        if path == "/api/smoothness/renderer":
            return self._json(PERF.renderer(PATHS, q.get("serial", "")))
        if path == "/api/smoothness/flags":
            return self._json(PERF.suggest_flags(PATHS, q.get("serial", "")))
        if path == "/api/tune":
            return self._json(PERF.autotune(PATHS, q.get("name", ""), apply=False))
        # --- AI era ---
        if path == "/api/ai":
            return self._json(AI.ai_status(PATHS, q.get("serial", "")))
        if path == "/api/ai/triage":
            return self._json(AI.triage(PATHS, q.get("serial", ""),
                                        _int(q.get("lines", 1000), 1000),
                                        q.get("spec", "")))
        if path == "/api/ai/nnapi":
            return self._json(AI.nnapi_status(PATHS, q.get("serial", "")))
        # --- v5.1: agent skill file, scrcpy, GPU preference, hybrid CPU ---
        if path == "/api/agent-skill":
            body = AGENTSKILL.skill_text().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            return
        if path == "/api/scrcpy":
            return self._json(STREAM.status(PATHS))
        if path == "/api/gpu":
            return self._json(GPU.get(PATHS))
        if path == "/api/cpu":
            return self._json(PERF.detect_hybrid())
        if path == "/api/apps/archived":
            return self._json(APPS.archived())
        return super().do_GET() if not path.startswith("/api/") else \
            self._json({"ok": False, "err": f"unknown GET {path}"}, 404)

    def _info(self) -> dict:
        """Public host facts, with absolute local paths deliberately omitted."""
        acc = ACCEL.accel_status(PATHS)
        return {
            "ok": True, "version": VERSION,
            "sdk": "configured" if PATHS.sdk else None,
            "sdk_name": PATHS.sdk.name if PATHS.sdk else None,
            "android_cli": bool(PATHS.android_cli), "adb": bool(PATHS.adb),
            "emulator": bool(PATHS.emulator), "defaults": DEFAULTS,
            "accel": {"accel": acc.get("accel", "unknown"),
                      "ready": bool(acc.get("ready")),
                      "detail": str(acc.get("detail", ""))[:300],
                      "whpx_usable": bool(acc.get("whpx_usable")),
                      "sunset_note": acc.get("sunset_note", ""),
                      "features_enabled": acc.get("features_enabled") or []},
            "host": {"cpu_count": HOST.cpu_count(),
                     "mem_total_mb": HOST.memory().get("total_mb")},
        }

    def _screen(self, q: dict):
        serial = q.get("serial", "")
        if not safe.valid_serial(serial) or not PATHS.adb:
            return self._json({"ok": False, "err": "valid serial + adb required"}, 400)
        s = ADB.screenshot_bytes(PATHS, serial)
        if not s.get("ok"):
            return self._json(s, 502)
        raw = s["png"]
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-NovaDroid-Via", s.get("via", "?"))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def _stream(self, q: dict):
        """MJPEG-style live view: one HTTP connection, many frames.

        Keeps one connection open and pushes frames into it, skipping frames
        that are byte-identical to the previous one: an idle device costs
        ~0 bandwidth instead of a full PNG per tick.
        """
        serial = q.get("serial", "")
        if not safe.valid_serial(serial) or not PATHS.adb:
            return self._json({"ok": False, "err": "valid serial + adb required"}, 400)
        try:
            fps = max(1, min(int(float(q.get("fps", 8) or 8)), 30))
        except (TypeError, ValueError):
            fps = 8
        budget = 1.0 / fps
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=--novaframe")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors()
        self.end_headers()
        # A stream is intentionally long-lived; the request-line timeout must
        # not kill an idle-but-open live view.
        try:
            self.connection.settimeout(None)
        except OSError:
            pass
        last = b""
        try:
            while True:
                t0 = time.time()
                s = ADB.screenshot_bytes(PATHS, serial)
                raw = s["png"] if s.get("ok") else b""
                if raw and raw != last:
                    last = raw
                    self.wfile.write(
                        b"--novaframe\r\n"
                        b"Content-Type: image/png\r\n"
                        b"Content-Length: " + str(len(raw)).encode() + b"\r\n\r\n"
                        + raw + b"\r\n")
                    self.wfile.flush()
                time.sleep(max(0.0, budget - (time.time() - t0)))
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass          # client closed the tab — normal, not an error
        except Exception as e:
            print("[stream]", serial, "-", type(e).__name__, e)

    def _download(self, name: str):
        """Stream a recorded mp4 back to the browser (path-confined, chunked)."""
        target = safe.confine(APPS.REC_DIR, safe.safe_filename(name))
        try:
            if target is None or not target.is_file():
                return self._json({"ok": False, "err": "file not found"}, 404)
            size = target.stat().st_size
        except OSError:
            return self._json({"ok": False, "err": "file not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{target.name}"')
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors()
        self.end_headers()
        try:
            self.connection.settimeout(None)   # large downloads may be slow
            with target.open("rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)   # 1 MiB
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    # --------------------------------------------------------------- POST --
    def do_POST(self):
        try:
            self._post()
        except (BrokenPipeError, ConnectionError, ConnectionResetError):
            pass
        except Exception as ex:
            self._server_error(ex)

    def _post(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/") and not self._guard(None, path):
            return
        b = jread(self)
        if b is None:      # error already sent (413/415) — no second response
            return
        if path == "/api/session":
            # Header-authenticated ticket mint. Local (tokenless) mode needs
            # no ticket at all; the frontend skips this call in that case.
            if not TOKEN:
                return self._json({"ok": True, "ticket": "", "local": True})
            return self._json({"ok": True, "ticket": _mint_ticket(),
                               "expires_s": int(TICKET_TTL),
                               "scope": sorted(TICKET_PATHS)})
        if path == "/api/shell" and not ALLOW_SHELL:
            return self._json({"ok": False,
                               "err": "ADB shell is disabled by default. "
                                      "Restart the agent with --allow-shell to enable it."},
                              403)
        if path == "/api/shared/reveal" and not ALLOW_GUI:
            return self._json({"ok": False,
                               "err": "Opening Explorer is disabled by default. "
                                      "Restart the agent with --allow-gui to enable it."},
                              403)
        if path == "/api/avds":
            return self._json(AVD.create_avd(PATHS, b.get("profile", "medium_phone")))
        if path == "/api/avds/start":
            return self._json(AVD.start_avd(PATHS, b.get("name", ""), bool(b.get("cold"))))
        if path == "/api/avds/start-raw":
            extra = b.get("extra", [])
            if isinstance(extra, str):
                extra = extra.split()
            affinity = b.get("affinity", 0) or 0
            try:
                affinity = int(affinity)
            except (TypeError, ValueError):
                affinity = 0
            return self._json(AVD.start_avd_raw(PATHS, b.get("name", ""),
                                                extra, affinity=affinity))
        if path == "/api/avds/stop":
            return self._json(AVD.stop_avd(PATHS, b.get("name", "")))
        if path == "/api/avds/wipe":
            return self._json(AVD.wipe_data(PATHS, b.get("name", "")))
        if path == "/api/avds/clone":
            return self._json(AVD.clone_avd(PATHS, b.get("src", ""),
                                            b.get("name", "") or b.get("dst", "")))
        if path == "/api/fleet":
            return self._json(AVD.fleet(PATHS, b.get("names") or [],
                                        b.get("action", "start"),
                                        bool(b.get("cold")),
                                        _int(b.get("stagger", 8), 8)))
        if path == "/api/snapshots":
            return self._json(AVD.snapshot(PATHS, b.get("serial", ""),
                                           b.get("action", ""), b.get("name", "")))
        if path == "/api/apk/install":
            return self._json(ADB.install_apk(PATHS, b.get("serial", ""),
                                              b.get("apk_path", "")))
        if path == "/api/geo":
            return self._json(ADB.geo_fix(PATHS, b.get("serial", ""),
                                          _float(b.get("lat", 27.7172), 27.7172),
                                          _float(b.get("lng", 85.3240), 85.3240)))
        if path == "/api/battery":
            return self._json(ADB.battery(PATHS, b.get("serial", ""),
                                          _int(b.get("level", 85), 85),
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
                                        _int(b.get("ms", 300), 300)))
        if path == "/api/input/text":
            return self._json(ADB.input_text(PATHS, b.get("serial", ""),
                                             b.get("text", "")))
        if path == "/api/input/key":
            return self._json(ADB.keyevent(PATHS, b.get("serial", ""),
                                           _int(b.get("code", 0), 0)))
        # --- macros ---
        if path == "/api/macros":
            return self._json(ADB.macro_save(b.get("name", "macro"),
                                             b.get("steps", [])))
        if path == "/api/macros/run":
            return self._json(ADB.macro_run(PATHS, b.get("serial", ""),
                                            b.get("name", ""),
                                            _int(b.get("loops", 1), 1),
                                            _float(b.get("loop_delay_s", 1.0), 1.0)))
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
                                                _int(b.get("seconds", 30), 30),
                                                _int(b.get("bitrate", 8), 8)))
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
                                              _int(b.get("level", 4), 4)))
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
        if path == "/api/avds/environment":
            return self._json(ENVIRON.set_mode(b.get("name", ""),
                                               b.get("mode", "")))
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
                                                 _int(b.get("interval", 15), 15)))
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
        # --- fleet batch install + generalised multi-instance sync ---
        if path == "/api/apk/install-all":
            return self._json(ADB.install_apk_all(PATHS, b.get("apk_path", "")))
        if path == "/api/sync-input":
            return self._json(sync_input(b.get("inputs", [])))
        # --- AVD toolkit ---
        if path == "/api/avds/cleanup":
            return self._json(AVD.cleanup(PATHS, b.get("name", ""),
                                          b.get("serial", "")))
        if path == "/api/avds/backup":
            return self._json(AVD.backup(b.get("name", "")))
        if path == "/api/avds/restore":
            return self._json(AVD.restore(b.get("zip", ""),
                                          b.get("name", "")))
        if path == "/api/avds/rename":
            return self._json(AVD.rename_avd(PATHS, b.get("src", ""),
                                             b.get("dst", "") or b.get("name2", "")))
        if path == "/api/avds/root":
            return self._json(AVD.root_toggle(PATHS, b.get("serial", "")))
        # --- device power-user features ---
        if path == "/api/open-url":
            return self._json(APPS.open_url(PATHS, b.get("serial", ""),
                                            b.get("url", "")))
        if path == "/api/proxy":
            return self._json(APPS.set_proxy(PATHS, b.get("serial", ""),
                                             b.get("proxy", "")))
        if path == "/api/proxy/clear":
            return self._json(APPS.clear_proxy(PATHS, b.get("serial", "")))
        if path == "/api/eco":
            return self._json(APPS.eco_mode(PATHS, b.get("serial", ""),
                                            bool(b.get("on", True))))
        if path == "/api/adb/tcpip":
            return self._json(APPS.tcpip(PATHS, b.get("serial", ""),
                                         _int(b.get("port", 5555), 5555)))
        if path == "/api/display/refresh":
            return self._json(DISP.set_refresh_rate(PATHS, b.get("serial", ""),
                                                    b.get("hz", 60)))
        if path == "/api/display/refresh/save":
            return self._json(DISP.save_refresh_to_avd(b.get("name", ""),
                                                       b.get("hz", 60)))
        # --- port forwarding ---
        if path == "/api/ports/add":
            return self._json(PORTS.add(PATHS, b.get("serial", ""),
                                        b.get("direction", "forward"),
                                        b.get("local", 0), b.get("remote", 0),
                                        b.get("abstract", "")))
        if path == "/api/ports/remove":
            return self._json(PORTS.remove(PATHS, b.get("serial", ""),
                                           b.get("direction", "forward"),
                                           b.get("local", 0),
                                           bool(b.get("all")),
                                           b.get("remote", 0)))
        # --- smoothness + AI assist ---
        if path == "/api/input/long-press":
            return self._json(ADB.long_press(PATHS, b.get("serial", ""),
                                             b.get("x", 0), b.get("y", 0),
                                             _int(b.get("ms", 1000), 1000)))
        if path == "/api/tune/apply":
            return self._json(PERF.autotune(PATHS, b.get("name", ""), apply=True))
        if path == "/api/ai/command":
            return self._json(AI.assist(PATHS, b.get("serial", ""),
                                        b.get("text", "")))
        if path == "/api/ai/interpret":
            return self._json(AI.interpret(b.get("text", "")))
        if path == "/api/ai/triage":
            return self._json(AI.triage_logcat(b.get("lines", []) or []))
        # --- v5.1: smart app archive + scrcpy + GPU preference ---
        if path == "/api/apps/archive":
            return self._json(APPS.archive_app(PATHS, b.get("serial", ""),
                                               b.get("package", "")))
        if path == "/api/apps/restore-archive":
            return self._json(APPS.restore_app(PATHS, b.get("serial", ""),
                                               b.get("package", "")))
        if path == "/api/scrcpy/mirror":
            return self._json(STREAM.mirror(PATHS, b.get("serial", ""),
                                            b.get("max_fps", 60)))
        if path == "/api/scrcpy/app-window":
            return self._json(STREAM.app_window(PATHS, b.get("serial", ""),
                                                b.get("package", ""),
                                                b.get("w", 0), b.get("h", 0)))
        if path == "/api/gpu":
            return self._json(GPU.set_pref(PATHS, b.get("mode", "")))
        return self._json({"ok": False, "err": f"unknown POST {path}"}, 404)

    # ------------------------------------------------------------- DELETE --
    def do_DELETE(self):
        try:
            self._delete()
        except (BrokenPipeError, ConnectionError, ConnectionResetError):
            pass
        except Exception as ex:
            self._server_error(ex)

    def _delete(self):
        path = urllib.parse.urlsplit(self.path).path
        q = self._q()
        if path.startswith("/api/") and not self._guard(q, path):
            return
        if path == "/api/avds":
            return self._json(AVD.remove_avd(PATHS, q.get("name", "")))
        if path == "/api/macros":
            return self._json(ADB.macro_delete(q.get("name", "")))
        return self._json({"ok": False, "err": "unknown DELETE"}, 404)


def _int(value, default: int) -> int:
    """Query/JSON numbers must never 500 a route with a ValueError."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Operation synchronizer (LDPlayer-class): one input mirrored to N instances
# --------------------------------------------------------------------------
_INPUT_ACTIONS = ("tap", "swipe", "text", "key")


def sync_input(inputs) -> dict:
    """Mirror ONE action to EVERY instance — in PARALLEL.

    Uses one adb process per device, concurrently, and validates input
    instead of KeyError-ing. Mirrored actions are tap/swipe/text/key.
    """
    from concurrent.futures import ThreadPoolExecutor

    if not PATHS.adb:
        return {"ok": False, "err": "adb not found"}
    if not isinstance(inputs, list) or not inputs:
        return {"ok": False, "err": "no valid targets (pick online emulator devices)"}
    head = inputs[0]
    if not isinstance(head, dict):
        return {"ok": False, "err": "no valid targets (pick online emulator devices)"}
    action = str(head.get("action", "tap")).lower()
    if action not in _INPUT_ACTIONS:
        return {"ok": False, "err": f"action must be one of {_INPUT_ACTIONS}"}
    items = []
    for t in inputs:
        if not isinstance(t, dict):
            continue
        serial = str(t.get("serial", "")).strip()
        if not re_serial(serial):
            continue
        items.append((serial, {k: v for k, v in t.items() if k != "serial"}))
    if not items:
        return {"ok": False, "err": "no valid targets (pick online emulator devices)"}

    def _one(item):
        serial, args = item
        if action == "tap":
            r = ADB.tap(PATHS, serial, args.get("x", 0), args.get("y", 0))
        elif action == "swipe":
            r = ADB.swipe(PATHS, serial, args.get("x1", 0), args.get("y1", 0),
                          args.get("x2", 0), args.get("y2", 0),
                          args.get("ms", 300))
        elif action == "text":
            r = ADB.input_text(PATHS, serial, args.get("text", ""))
        else:
            r = ADB.keyevent(PATHS, serial, args.get("code", 0))
        return {"serial": serial, "ok": bool(r.get("ok"))}

    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        results = list(pool.map(_one, items))
    return {"ok": all(r["ok"] for r in results), "action": action,
            "results": results, "count": len(results), "parallel": True}


def sync_tap(taps) -> dict:
    """Back-compat wrapper: the v1 payload shape mapped onto sync_input."""
    return sync_input([{**(t if isinstance(t, dict) else {}), "action": "tap"}
                       for t in (taps or [])])


def re_serial(value: str) -> bool:
    """Accept real hardware serials too (USB devices, not just emulators)."""
    return safe.valid_serial(value)


# --------------------------------------------------------------------------
def _origins_from(args) -> frozenset[str]:
    raw = list(args.origin or [])
    raw += [s for s in os.environ.get("NOVADROID_ORIGINS", "").split(",") if s.strip()]
    out = set()
    for s in raw:
        for part in str(s).split(","):
            part = part.strip().rstrip("/")
            if not part:
                continue
            u = urllib.parse.urlsplit(part)
            if u.scheme in ("http", "https") and u.netloc and u.path in ("", "/"):
                out.add(f"{u.scheme}://{u.netloc}")
    return frozenset(out)


def main():
    global TOKEN, ALLOW_SHELL, ALLOW_GUI, ALLOWED_ORIGINS, ALLOWED_HOSTS
    global STRICT_HOST, MAX_BODY
    ap = argparse.ArgumentParser(description="NovaDroid emulator manager")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; use 0.0.0.0 for tunnel/remote access (requires a token)")
    ap.add_argument("--token", default="",
                    help="Bearer token (prefer NOVADROID_TOKEN or --token-file: "
                         "command-line args are visible to other local users)")
    ap.add_argument("--token-file", default="",
                    help="read the Bearer token from a file instead of argv")
    ap.add_argument("--gen-token", action="store_true",
                    help="print a fresh token and exit")
    ap.add_argument("--origin", action="append", default=[],
                    help="allowed CORS origin, e.g. https://my-app.vercel.app "
                         "(repeatable; also NOVADROID_ORIGINS)")
    ap.add_argument("--allow-shell", action="store_true",
                    help="enable /api/shell (arbitrary adb shell) — OFF by default")
    ap.add_argument("--allow-gui", action="store_true",
                    help="enable /api/shared/reveal (launches Explorer) — OFF by default")
    ap.add_argument("--strict-host", action="store_true",
                    help="also validate the Host header (DNS-rebinding protection)")
    ap.add_argument("--allowed-host", action="append", default=[],
                    help="extra Host header to allow with --strict-host (repeatable)")
    ap.add_argument("--max-body-mb", type=int, default=320,
                    help="maximum JSON body size in MiB (default 320, for base64 APKs)")
    ap.add_argument("--strict-token", action="store_true",
                    help="deprecated no-op: v5 always requires the token when set")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    if args.gen_token:
        print(secrets.token_urlsafe(24))
        return

    token = args.token or os.environ.get("NOVADROID_TOKEN", "")
    if not token and args.token_file:
        try:
            token = Path(args.token_file).read_text(encoding="utf-8").strip()
        except OSError:
            print(f"REFUSED: cannot read token file: {args.token_file}")
            raise SystemExit(2)
    TOKEN = token.strip()
    ALLOW_SHELL = args.allow_shell
    ALLOW_GUI = args.allow_gui
    ALLOWED_ORIGINS = _origins_from(args)
    STRICT_HOST = args.strict_host
    ALLOWED_HOSTS = frozenset(
        {(urllib.parse.urlsplit(o).hostname or "").lower()
         for o in ALLOWED_ORIGINS}
        | {h.strip().lower() for h in args.allowed_host if h.strip()}) - {""}
    MAX_BODY = max(1, min(args.max_body_mb, 1024)) * 1024 * 1024

    remote = args.host not in ("127.0.0.1", "localhost", "::1")
    if remote and not TOKEN:
        print("REFUSED: --host exposes adb-shell power to the network; "
              "set NOVADROID_TOKEN or pass --token-file.")
        print("  Generate one:  python novadroid.py --gen-token")
        raise SystemExit(2)
    if remote and not ALLOWED_ORIGINS:
        print("WARNING: no --origin configured. A browser-hosted dashboard on "
              "another origin will be blocked by CORS. Add --origin <url>.")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    url = f"http://{'localhost' if args.host == '0.0.0.0' else args.host}:{args.port}"
    print(f"NovaDroid {VERSION} running at {url}  "
          f"(auth={'token' if TOKEN else 'localhost-open'})")
    print(f"  CORS origins: {', '.join(sorted(ALLOWED_ORIGINS)) or 'same-origin only'}")
    print(f"  opt-in:       shell={'ON' if ALLOW_SHELL else 'off'}, "
          f"gui={'ON' if ALLOW_GUI else 'off'}")
    print(f"  SDK:          {PATHS.sdk or 'not found'}")
    if args.token:
        print("  NOTE: --token is visible in the process list; prefer "
              "NOVADROID_TOKEN or --token-file next time.")
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
