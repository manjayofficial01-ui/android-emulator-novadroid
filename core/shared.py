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
GUIDE_NAME = "Put APKs, photos & files here.txt"
_SYNC_JOBS: dict[str, dict] = {}


def shared_dir() -> Path:
    d = Path.home() / "NovaDroid-Shared"
    fresh = not d.exists()
    d.mkdir(parents=True, exist_ok=True)
    if fresh:  # write once — never resurrect after the user deletes it
        (d / GUIDE_NAME).write_text(
            "Drop APKs, photos, or any files in this folder.\n"
            "NovaDroid syncs them to /sdcard/NovaDroid-Shared on the emulator\n"
            "and pulls device files back here. Both sides stay in sync.\n",
            encoding="utf-8")
    return d


def _pc_path(name: str) -> Path | None:
    """Confine a user-supplied name inside the shared dir (no escapes)."""
    clean = Path(name).name
    if not clean or clean in (".", ".."):
        return None
    p = shared_dir() / clean
    try:
        if p.resolve().parent != shared_dir().resolve():
            return None
    except Exception:
        return None
    return p


def _dev_name(name: str) -> str | None:
    """Confine a device-side name to a direct child of DEVICE_DIR (no rm -rf /).

    Spaces and & are fine (single-quoted for the device shell); only the
    quote char itself, separators and dot-names are refused.
    """
    if not name or "/" in name or "\\" in name:
        return None
    if "'" in name or "\n" in name:
        return None
    clean = Path(name).name
    if not clean or clean in (".", ".."):
        return None
    return clean


def _q(remote: str) -> str:
    """Single-quote a remote path — adb joins args with spaces, so an
    unquoted 'My Video.mp4' would split into three paths on-device."""
    return f"'{remote}'"


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
             "-d", _q(f"file://{DEVICE_DIR}/{n}")], timeout=10)


def _push_one(paths: ToolPaths, serial: str, src: Path, dest: str) -> bool:
    """Push a single file or dir. --sync skips unchanged files (fast re-syncs)."""
    r = run([paths.adb, "-s", serial, "push", "--sync", str(src), dest], timeout=300)
    return r["ok"]


def push(paths: ToolPaths, serial: str, names: list[str] | None = None) -> dict:
    """PC -> device, entry by entry (flat — never nests the folder into itself)."""
    if (e := _check(paths, serial)):
        return e
    if not _online(paths, serial):
        return {"ok": False, "err": f"{serial} is offline"}
    base = shared_dir()
    if names:
        targets = [base / n for n in names]
    else:  # whole folder, minus the local readme (stays a PC-side thing)
        targets = sorted(p for p in base.iterdir() if p.name != GUIDE_NAME)
    pushed, failed, skipped = [], [], 0
    for src in targets:
        if not src.exists() or src == base:
            skipped += 1
            continue
        dest = f"{DEVICE_DIR}/{src.name}"
        if _push_one(paths, serial, src, dest):
            pushed.append(src.name)
        else:
            failed.append(src.name)
    if pushed:
        _scan(paths, serial, pushed)
    out: dict = {"ok": not failed, "pushed": pushed, "skipped": skipped}
    if failed:
        out["err"] = f"failed: {', '.join(failed)}"
    return out


def pull(paths: ToolPaths, serial: str, names: list[str] | None = None) -> dict:
    """Device -> PC, entry by entry (flat — mirrors push)."""
    if (e := _check(paths, serial)):
        return e
    if not _online(paths, serial):
        return {"ok": False, "err": f"{serial} is offline"}
    base = shared_dir()
    if names is None:
        dev = list_device(paths, serial)
        if not dev.get("ok"):
            return {"ok": False, "err": "could not list device folder"}
        names = [f for f in dev["files"] if not f.endswith("/")]
    pulled, failed = [], []
    for n in names:
        clean = Path(n).name  # never let device paths escape the shared dir
        r = run([paths.adb, "-s", serial, "pull",
                 f"{DEVICE_DIR}/{clean}", str(base / clean)], timeout=300)
        if r["ok"]:
            pulled.append(clean)
        else:
            failed.append(clean)
    out: dict = {"ok": not failed, "pulled": pulled}
    if failed:
        out["err"] = f"failed: {', '.join(failed)}"
    return out


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
def _job_tick(paths: ToolPaths, serial: str, job: dict) -> None:
    """One mirror pass. Records telemetry so the UI can show what happened."""
    job["last_run"] = time.time()
    try:
        if not _online(paths, serial):
            job["waiting"] = True
            job["last_result"] = f"waiting for {serial} (offline)"
            return
        job["waiting"] = False
        pr = push(paths, serial)
        pl = pull(paths, serial)
        if pr.get("ok") and pl.get("ok"):
            job["last_result"] = "synced both ways ✓"
        else:
            job["last_result"] = ("push: " + (pr.get("err") or "ok")
                                  + " · pull: " + (pl.get("err") or "ok"))
    except Exception as e:
        job["last_result"] = f"error: {e}"


def autosync_set(paths: ToolPaths, serial: str, on: bool, interval: int = 15) -> dict:
    old = _SYNC_JOBS.pop(serial, None) if serial else None
    if old:
        old["stop"].set()
    if not on:
        return {"ok": True, "autosync": False}
    if not serial:
        return {"ok": False,
                "err": "Pick a device first (Quickboot an AVD) — nothing to sync with."}
    stop = threading.Event()
    interval = max(5, min(int(interval or 15), 300))
    job = {"stop": stop, "interval": interval, "since": time.time(),
           "last_run": 0, "last_result": "starting…", "waiting": True}

    def _loop():
        while not stop.wait(interval):
            _job_tick(paths, serial, job)
            if stop.is_set():
                break
        _SYNC_JOBS.pop(serial, None)

    _SYNC_JOBS[serial] = job
    threading.Thread(target=_loop, daemon=True).start()
    state = "online — syncing" if _online(paths, serial) else f"armed, waiting for {serial} to come online"
    return {"ok": True, "autosync": True, "interval": interval,
            "note": f"{state}. PC {shared_dir()} <-> {DEVICE_DIR} every {interval}s."}


def autosync_status() -> dict:
    return {"ok": True, "jobs": {
        k: {"interval": v["interval"], "since": v["since"],
            "last_run": v.get("last_run", 0),
            "last_result": v.get("last_result", ""),
            "waiting": v.get("waiting", True)}
        for k, v in _SYNC_JOBS.items()}}


def sync_now(paths: ToolPaths, serial: str) -> dict:
    """Manual one-shot MERGE (never deletes — safe default)."""
    if (e := _check(paths, serial)):
        return e
    if not _online(paths, serial):
        return {"ok": False,
                "err": f"{serial} is offline — Quickboot the AVD and wait for boot, then Sync."}
    job: dict = {}
    _job_tick(paths, serial, job)
    ok = job.get("last_result") == "synced both ways ✓"
    return {"ok": ok, "result": job.get("last_result", ""),
            "pc": str(shared_dir()), "device_dir": DEVICE_DIR}


def delete_pc(names: list[str]) -> dict:
    """Permanently delete entries from the desktop folder. Stays deleted:
    merge-sync only copies existing files, and the guide is write-once."""
    gone, bad = [], []
    for n in names or []:
        p = _pc_path(n)
        if p is None or not p.exists():
            bad.append(n)
            continue
        try:
            if p.is_dir() and not p.is_symlink():
                import shutil as _sh
                _sh.rmtree(p)
            else:
                p.unlink()
            gone.append(Path(n).name)
        except Exception:
            bad.append(n)
    out: dict = {"ok": not bad, "deleted": gone}
    if bad:
        out["err"] = f"not found/refused: {', '.join(bad)}"
    return out


def delete_everywhere(paths: ToolPaths, serial: str, names: list[str]) -> dict:
    """Delete from BOTH desktop and device in one click (the 🗑 button).

    No serial / offline device → still deletes from the desktop and says so.
    Merge-sync can never resurrect these: it only copies files that exist.
    """
    pc = delete_pc(names or [])
    dev: dict = {"ok": True, "deleted": [], "note": "no device picked"}
    if serial:
        if not _online(paths, serial):
            dev = {"ok": False, "deleted": [],
                   "note": f"{serial} offline — deleted from desktop only"}
        else:
            dr = delete_device(paths, serial, names or [])
            dev = {"ok": dr["ok"], "deleted": dr.get("deleted", []),
                   "note": dr.get("err", "")}
    return {"ok": bool(pc.get("ok")), "deleted_pc": pc.get("deleted", []),
            "deleted_device": dev.get("deleted", []),
            "device_note": dev.get("note", ""),
            "err": pc.get("err", "")}
    gone, bad = [], []
    for n in names or []:
        p = _pc_path(n)
        if p is None or not p.exists():
            bad.append(n)
            continue
        try:
            if p.is_dir() and not p.is_symlink():
                import shutil as _sh
                _sh.rmtree(p)
            else:
                p.unlink()
            gone.append(Path(n).name)
        except Exception:
            bad.append(n)
    out: dict = {"ok": not bad, "deleted": gone}
    if bad:
        out["err"] = f"not found/refused: {', '.join(bad)}"
    return out


def delete_device(paths: ToolPaths, serial: str, names: list[str]) -> dict:
    """Permanently delete entries from the device folder (rm inside it only)."""
    if (e := _check(paths, serial)):
        return e
    if not _online(paths, serial):
        return {"ok": False, "err": f"{serial} is offline"}
    gone, bad = [], []
    for n in names or []:
        clean = _dev_name(n)
        if clean is None:
            bad.append(n)
            continue
        r = run([paths.adb, "-s", serial, "shell", "rm", "-rf",
                 _q(f"{DEVICE_DIR}/{clean}")], timeout=30)
        (gone if r["ok"] else bad).append(clean)
    out: dict = {"ok": not bad, "deleted": gone}
    if bad:
        out["err"] = f"failed/refused: {', '.join(bad)}"
    return out


def _pc_names() -> set[str]:
    return {p.name for p in shared_dir().iterdir() if p.name != GUIDE_NAME}


def mirror(paths: ToolPaths, serial: str, direction: str) -> dict:
    """EXACT one-way mirror — target ends up identical to source, extras deleted.

    direction='push': PC is boss. Device extras deleted, then PC pushed.
    direction='pull': device is boss. PC extras deleted (guide readme spared),
                      then device pulled. "One file on desktop + sync" =>
                      exactly one file on both sides.
    """
    if (e := _check(paths, serial)):
        return e
    if direction not in ("push", "pull"):
        return {"ok": False, "err": "direction must be 'push' or 'pull'"}
    if not _online(paths, serial):
        return {"ok": False,
                "err": f"{serial} is offline — Quickboot the AVD and wait for boot."}
    if direction == "push":
        dev = list_device(paths, serial)
        extras = [f for f in dev.get("files", []) if not f.endswith("/")
                  and f not in _pc_names()]
        del_res = delete_device(paths, serial, extras) if extras else {"ok": True}
        pr = push(paths, serial)
        return {"ok": bool(del_res.get("ok") and pr.get("ok")),
                "direction": "push",
                "deleted_from_device": extras,
                "copied_to_device": pr.get("pushed", []),
                "err": del_res.get("err") or pr.get("err") or ""}
    dev = list_device(paths, serial)
    if not dev.get("ok"):
        return {"ok": False, "err": "could not list device folder"}
    want = {f for f in dev["files"] if not f.endswith("/")}
    extras = sorted(_pc_names() - want)
    del_res = delete_pc(extras) if extras else {"ok": True}
    pl = pull(paths, serial)
    return {"ok": bool(del_res.get("ok") and pl.get("ok")),
            "direction": "pull",
            "deleted_from_pc": extras,
            "copied_to_pc": pl.get("pulled", []),
            "err": del_res.get("err") or pl.get("err") or ""}
