"""Bidirectional shared folder: Windows <-> emulator.

Design (BlueStacks-style shared folder, implemented the robust way):
  PC side:   %USERPROFILE%\\NovaDroid-Shared\\   (plain Windows folder)
  Device:    /sdcard/NovaDroid-Shared/           (visible in Files, Gallery…)
Transport is adb push/pull --sync (works on locked-down Play images, no root,
no guest tools needed) + MEDIA_SCANNER broadcasts so photos/APKs show up
instantly in Android apps. Optional auto-sync thread mirrors both directions
every N seconds while the device is online.
"""
from __future__ import annotations

import base64
import threading
import time
from pathlib import Path

from .config import ToolPaths
from .runner import run

DEVICE_DIR = "/sdcard/NovaDroid-Shared"
MAX_UPLOAD_MB = 250
_SYNC_JOBS: dict[str, dict] = {}


def shared_dir() -> Path:
    d = Path.home() / "NovaDroid-Shared"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Put APKs, photos & files here.txt").write_text(
        "Drop APKs, photos, or any files in this folder.\n"
        "NovaDroid syncs them to /sdcard/NovaDroid-Shared on the emulator\n"
        "and pulls device files back here. Both sides stay in sync.\n",
        encoding="utf-8") if not (d / "Put APKs, photos & files here.txt").exists() else None
    return d


def _check(paths: ToolPaths, serial: str) -> dict | None:
    if not paths.adb:
        return {"ok": False, "err": "adb not found"}
    if not serial:
        return {"ok": False, "err": "no device selected — Quickboot an AVD first"}
    return None


def _online(paths: ToolPaths, serial: str) -> bool:
    r = run([paths.adb, "devices"], timeout=10)
    return any(l.split()[:2] == [serial, "device"] for l in r["out"].splitlines())


def status() -> dict:
    d = shared_dir()
    files = [p for p in d.rglob("*") if p.is_file()]
    return {"ok": True, "dir": str(d), "device_dir": DEVICE_DIR,
            "files": [{"name": str(p.relative_to(d)), "bytes": p.stat().st_size,
                       "mtime": p.stat().st_mtime} for p in sorted(files)][:500],
            "count": len(files),
            "total_mb": round(sum(p.stat().st_size for p in files) / 1e6, 1)}


def _scan(paths: ToolPaths, serial: str, names: list[str]) -> None:
    """Tell Android's media scanner about new files (Gallery/Files pick them up)."""
    for n in names[:50]:  # cap: broadcasts are cheap but let's not spam
        run([paths.adb, "-s", serial, "shell", "am", "broadcast",
             "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
             "-d", f"file://{DEVICE_DIR}/{n}"], timeout=10)


def push(paths: ToolPaths, serial: str, names: list[str] | None = None) -> dict:
    """PC -> device. names omitted = whole folder (--sync: only newer files)."""
    if (e := _check(paths, serial)):
        return e
    if not _online(paths, serial):
        return {"ok": False, "err": f"{serial} is offline"}
    base = shared_dir()
    if names:
        pushed = []
        for n in names:
            src = base / n
            if not src.exists():
                continue
            r = run([paths.adb, "-s", serial, "push", str(src),
                     f"{DEVICE_DIR}/{n}"], timeout=180)
            if r["ok"]:
                pushed.append(n)
        _scan(paths, serial, pushed)
        return {"ok": True, "pushed": pushed}
    r = run([paths.adb, "-s", serial, "push", "--sync", str(base) + "/", DEVICE_DIR + "/"],
            timeout=600)
    if r["ok"]:
        _scan(paths, serial, [p["name"] for p in status()["files"]])
    return {"ok": r["ok"], "log": (r["out"] + r["err"])[-800:] or "in sync"}


def pull(paths: ToolPaths, serial: str, names: list[str] | None = None) -> dict:
    """Device -> PC."""
    if (e := _check(paths, serial)):
        return e
    if not _online(paths, serial):
        return {"ok": False, "err": f"{serial} is offline"}
    base = shared_dir()
    if names:
        pulled = []
        for n in names:
            r = run([paths.adb, "-s", serial, "pull",
                     f"{DEVICE_DIR}/{n}", str(base / Path(n).name)], timeout=180)
            if r["ok"]:
                pulled.append(n)
        return {"ok": True, "pulled": pulled}
    r = run([paths.adb, "-s", serial, "pull", DEVICE_DIR + "/", str(base) + "/"],
            timeout=600)
    return {"ok": r["ok"], "log": (r["out"] + r["err"])[-800:] or "in sync"}


def list_device(paths: ToolPaths, serial: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    r = run([paths.adb, "-s", serial, "shell", "ls", "-p", DEVICE_DIR], timeout=15)
    if not r["ok"]:
        return {"ok": True, "files": [],
                "note": f"{DEVICE_DIR} is empty on device — push something first."}
    return {"ok": True, "dir": DEVICE_DIR,
            "files": [l for l in r["out"].splitlines() if l.strip()]}


def upload(name: str, b64: str, paths: ToolPaths | None = None,
           push_serial: str = "") -> dict:
    """Browser upload -> shared folder (optionally straight onto the device)."""
    safe = Path(name).name  # strip any path the browser sent
    if not safe or safe in (".", ".."):
        return {"ok": False, "err": "bad file name"}
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return {"ok": False, "err": "not valid base64"}
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        return {"ok": False, "err": f"over {MAX_UPLOAD_MB}MB cap"}
    fp = shared_dir() / safe
    fp.write_bytes(raw)
    out: dict = {"ok": True, "file": safe, "bytes": len(raw)}
    if paths and push_serial:
        pr = push(paths, push_serial, [safe])
        out["pushed"] = pr.get("ok", False)
    return out


def reveal() -> dict:
    """Open the shared folder in Explorer (Windows)."""
    import subprocess as _sp
    try:
        _sp.Popen(["explorer", str(shared_dir())])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "err": str(e)}


# --- auto-sync: mirror both ways every N sec while device is online ---
def autosync_set(paths: ToolPaths, serial: str, on: bool, interval: int = 15) -> dict:
    old = _SYNC_JOBS.pop(serial, None)
    if old:
        old["stop"].set()
    if not on:
        return {"ok": True, "autosync": False}
    stop = threading.Event()
    interval = max(5, min(int(interval or 15), 300))

    def _loop():
        while not stop.wait(interval):
            try:
                if _online(paths, serial):
                    push(paths, serial)
                    pull(paths, serial)
                else:
                    pass
            except Exception:
                pass
            if stop.is_set():
                break
        _SYNC_JOBS.pop(serial, None)

    _SYNC_JOBS[serial] = {"stop": stop, "interval": interval,
                          "since": time.time()}
    threading.Thread(target=_loop, daemon=True).start()
    return {"ok": True, "autosync": True, "interval": interval,
            "note": f"Mirroring {shared_dir()} <-> {DEVICE_DIR} every {interval}s while {serial} is online."}


def autosync_status() -> dict:
    return {"ok": True, "jobs": {k: {"interval": v["interval"], "since": v["since"]}
                                 for k, v in _SYNC_JOBS.items()}}
