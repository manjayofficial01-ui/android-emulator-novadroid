"""Optional scrcpy wrapper — hardware-decoded mirroring + per-app windows.

NovaDroid's built-in MJPEG live view decodes PNGs in Python (fine for
monitoring, heavy at high FPS). scrcpy 5.x mirrors with hardware decoding
(D3D11VA on Windows — roughly 10x lower decode CPU) and since 4.x can open
an app in its own resizable virtual display (`--new-display=WxH
--start-app=<pkg>`): the manager-layer equivalent of BlueStacks' "True
Desktop Windowing" and MuMu's Desktop Window Mode, without forking the
engine.

Consistent with the project's wrap-don't-replace philosophy:
  * scrcpy is never bundled. If ``scrcpy.exe`` is on PATH, next to adb, or
    pointed at by NOVADROID_SCRCPY, the performance view appears; otherwise
    the API says so honestly and the MJPEG stream remains the default.
  * Spawned windows are detached processes the user closes like any app.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

from . import safe
from .config import ToolPaths

_SPAM: list = []          # spawned Popen handles (bounded, drained lazily)
_SPAM_LOCK = threading.Lock()


def find_scrcpy(paths: ToolPaths) -> str:
    """Locate scrcpy.exe — env override, PATH, adb's folder, SDK root."""
    env = os.environ.get("NOVADROID_SCRCPY", "").strip()
    if env:
        p = Path(env)
        if p.is_file():
            return str(p)
        if p.is_dir() and (p / "scrcpy.exe").is_file():
            return str(p / "scrcpy.exe")
    found = shutil.which("scrcpy")
    if found:
        return found
    for base in (Path(paths.adb).parent if paths.adb else None,
                 Path(paths.sdk) if paths.sdk else None):
        if base:
            cand = base / "scrcpy.exe"
            if cand.is_file():
                return str(cand)
    return ""


def version(exe: str) -> str:
    if not exe:
        return ""
    from .runner import run
    r = run([exe, "--version"], timeout=10)
    first = (r.get("out") or "").strip().splitlines()
    return (first[0].strip() if first else "")[:80]


def status(paths: ToolPaths) -> dict:
    """Is the scrcpy path available? Always 200-ok; `available` is the truth."""
    exe = find_scrcpy(paths)
    out: dict = {"ok": True, "available": bool(exe), "path": exe or None,
                 "version": version(exe) if exe else ""}
    if not exe:
        out["note"] = ("scrcpy not found — install it (scoop install scrcpy / "
                       "scrcpy.org) or set NOVADROID_SCRCPY. The built-in "
                       "MJPEG stream stays available either way.")
    return out


def _spawn(cmd: list[str]) -> dict:
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    except OSError:
        return {"ok": False, "err": "could not launch scrcpy"}
    with _SPAM_LOCK:
        _SPAM[:] = [p for p in _SPAM if p.poll() is None][-31:]
        _SPAM.append(proc)
    return {"ok": True, "pid": proc.pid}


def mirror(paths: ToolPaths, serial: str, max_fps: int = 60,
           extra: list[str] | None = None) -> dict:
    """Open a hardware-decoded scrcpy window for `serial`."""
    exe = find_scrcpy(paths)
    if not exe:
        return {"ok": False, "err": "scrcpy not found — see GET /api/scrcpy"}
    if (e := _check_serial(serial)):
        return e
    fps = _clamp(max_fps, 15, 240, 60)
    extra = [str(a) for a in (extra or []) if str(a).strip()]
    # Detached argv (no shell), but a space inside one token would still
    # mis-split a "flag value" pair — scrcpy takes them as two tokens anyway.
    if len(extra) > 16 or any(safe.has_control(a) or " " in a for a in extra):
        return {"ok": False, "err": "illegal extra scrcpy flag"}
    return _spawn([exe, "-s", serial, "--max-fps", str(fps)] + extra)


def app_window(paths: ToolPaths, serial: str, package: str,
               w: int = 0, h: int = 0) -> dict:
    """Open ONE app in its own resizable virtual display (scrcpy 4.x+).

    This is what competitors call desktop windowing — except it works per
    app, on the stock Google emulator, with no vendor fork.
    """
    exe = find_scrcpy(paths)
    if not exe:
        return {"ok": False, "err": "scrcpy not found — see GET /api/scrcpy"}
    if (e := _check_serial(serial)):
        return e
    if not safe.valid_package(package):
        return {"ok": False, "err": "invalid package name"}
    w, h = _clamp(w, 240, 4096, 0), _clamp(h, 240, 4096, 0)
    cmd = [exe, "-s", serial, "--start-app", package]
    if w and h:
        cmd += ["--new-display", f"{w}x{h}"]
    return _spawn(cmd)


def _check_serial(serial: str) -> dict | None:
    if not safe.valid_serial(serial):
        return {"ok": False, "err": "valid serial required"}
    return None


def _clamp(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(int(float(v)), hi))
    except (TypeError, ValueError, OverflowError):
        return default
