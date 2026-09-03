"""Two-folder sharing: Windows <-> emulator.

  NovaDroid-Shared/Desktop/  — stage files here to share them TO the device.
  NovaDroid-Shared/Device/   — exact readout of the device folder; pulls land here.

Push uploads Desktop/ to /sdcard/NovaDroid-Shared. Pull makes Device/ identical
to the device (new/changed downloaded, extras removed). The trash button deletes
a name from Desktop/ + Device/ + device at once, so nothing ever resurrects.
Transport is adb (no root, no guest tools), plus media-scanner broadcasts.
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
    (d / "Desktop").mkdir(exist_ok=True)
    (d / "Device").mkdir(exist_ok=True)
    if fresh:
        (d / GUIDE_NAME).write_text(
            "Desktop/ : drop files here to share them TO the emulator.\n"
            "Device/  : exact copy of the emulator's shared folder.\n"
            "Trash in the dashboard deletes from Desktop + Device + device.\n",
            encoding="utf-8")
        return d
    # one-time migration from the old flat layout (marker = idempotent)
    if not (d / ".folders-v2").exists():
        for p in list(d.iterdir()):
            if p.is_file() and p.name not in (GUIDE_NAME, ".folders-v2"):
                try:
                    p.rename(d / "Desktop" / p.name)
                except Exception:
                    pass
        try:
            (d / ".folders-v2").write_text("migrated\n", encoding="utf-8")
        except Exception:
            pass
    return d


def desktop_dir() -> Path:
    return shared_dir() / "Desktop"


def device_mirror_dir() -> Path:
    return shared_dir() / "Device"


def _ls(d: Path) -> list[dict]:
    try:
        files = [p for p in d.iterdir() if p.is_file()]
    except Exception:
        files = []
    return [{"name": p.name, "bytes": p.stat().st_size, "mtime": p.stat().st_mtime}
            for p in sorted(files)]


def _confine(base: Path, name: str) -> Path | None:
    clean = Path(name).name
    if not clean or clean in (".", ".."):
        return None
    p = base / clean
    try:
        if p.resolve().parent != base.resolve():
            return None
    except Exception:
        return None
    return p


def _dev_name(name: str) -> str | None:
    """Direct child of DEVICE_DIR only (no rm -rf /). Spaces/& fine —
    single-quoted for the device shell; only the quote char is refused."""
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
    desk, mirr = _ls(desktop_dir()), _ls(device_mirror_dir())
    total = sum(f["bytes"] for f in desk + mirr)
    return {"ok": True, "dir": str(d), "device_dir": DEVICE_DIR,
            "desktop": desk, "deviceFolder": mirr,
            "files": desk + mirr,  # compat for older dashboard builds
            "count": len(desk) + len(mirr), "total_mb": round(total / 1e6, 1)}


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
    """Desktop/ -> device. Safe copy: never deletes."""
    if (e := _check(paths, serial)):
        return e
    if not _online(paths, serial):
        return {"ok": False, "err": f"{serial} is offline"}
    base = desktop_dir()
    if names:
        targets = [base / Path(n).name for n in names]
    else:  # whole folder, files only — subdirs are organization, not payload
        targets = sorted(p for p in base.iterdir() if p.is_file())
    pushed, failed, skipped = [], [], 0
    for src in targets:
        if not src.exists() or src == base:
            skipped += 1
            continue
        if _push_one(paths, serial, src, f"{DEVICE_DIR}/{src.name}"):
            pushed.append(src.name)
        else:
            failed.append(src.name)
    if pushed:
        _scan(paths, serial, pushed)
    out: dict = {"ok": not failed, "pushed": pushed, "skipped": skipped}
    if failed:
        out["err"] = f"failed: {', '.join(failed)}"
    return out


def _dev_sizes(paths: ToolPaths, serial: str) -> dict:
    """name -> bytes for device files (toybox stat; best effort)."""
    r = run([paths.adb, "-s", serial, "shell", "stat", "-c", "%s %n",
             f"{DEVICE_DIR}/*"], timeout=30)
    sizes: dict = {}
    if r["ok"]:
        for line in r["out"].splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                sizes[Path(parts[1]).name] = int(parts[0])
    return sizes


def pull(paths: ToolPaths, serial: str, names: list[str] | None = None) -> dict:
    """Device -> Device/ folder. Whole-folder pull is EXACT: Device/ ends up
    identical to the device (extras removed), so the folder truly shows it."""
    if (e := _check(paths, serial)):
        return e
    if not _online(paths, serial):
        return {"ok": False, "err": f"{serial} is offline"}
    mirror_dir = device_mirror_dir()
    if names is not None:
        pulled, failed = [], []
        for n in names:
            clean = _dev_name(n)
            if clean is None:
                failed.append(n)
                continue
            r = run([paths.adb, "-s", serial, "pull",
                     f"{DEVICE_DIR}/{clean}", str(mirror_dir / clean)], timeout=300)
            (pulled if r["ok"] else failed).append(clean)
        out: dict = {"ok": not failed, "pulled": pulled}
        if failed:
            out["err"] = f"failed/refused: {', '.join(failed)}"
        return out
    dev = list_device(paths, serial)
    if not dev.get("ok"):
        return {"ok": False, "err": "could not list device folder"}
    want = {f for f in dev["files"] if not f.endswith("/")}
    sizes = _dev_sizes(paths, serial)
    pulled, skipped, failed = [], [], []
    for name in sorted(want):
        local = mirror_dir / name
        if local.exists() and name in sizes and local.stat().st_size == sizes[name]:
            skipped.append(name)  # unchanged — skip the re-download
            continue
        r = run([paths.adb, "-s", serial, "pull",
                 f"{DEVICE_DIR}/{name}", str(local)], timeout=300)
        (pulled if r["ok"] else failed).append(name)
    for extra in sorted({p.name for p in mirror_dir.iterdir() if p.is_file()} - want):
        try:
            (mirror_dir / extra).unlink()
            skipped.append(f"-{extra}")
        except Exception:
            failed.append(extra)
    out = {"ok": not failed, "pulled": pulled, "skipped": skipped}
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
    """Browser upload -> Desktop/ staging (optionally straight onto the device)."""
    import base64 as _b64
    safe = Path(name).name  # strip any path the browser sent
    if not safe or safe in (".", ".."):
        return {"ok": False, "err": "bad file name"}
    try:
        raw = _b64.b64decode(b64, validate=True)
    except Exception:
        return {"ok": False, "err": "not valid base64"}
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        return {"ok": False, "err": f"over {MAX_UPLOAD_MB}MB cap"}
    fp = desktop_dir() / safe
    fp.write_bytes(raw)
    out: dict = {"ok": True, "file": safe, "bytes": len(raw), "folder": "Desktop"}
    if paths and push_serial:
        pr = push(paths, push_serial, [safe])
        out["pushed"] = pr.get("ok", False)
    return out


def reveal(which: str = "") -> dict:
    """Open the shared folder (or a subfolder) in Explorer (Windows)."""
    import subprocess as _sp
    target = shared_dir() / Path(which).name if which in ("Desktop", "Device") else shared_dir()
    try:
        _sp.Popen(["explorer", str(target)])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def _delete_where(base: Path, names: list[str]) -> tuple[list, list]:
    gone, bad = [], []
    for n in names or []:
        p = _confine(base, n)
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
    return gone, bad


def delete_pc(names: list[str]) -> dict:
    """Delete from Desktop/ staging (compat alias)."""
    gone, bad = _delete_where(desktop_dir(), names)
    out: dict = {"ok": not bad, "deleted": gone}
    if bad:
        out["err"] = f"not found/refused: {', '.join(bad)}"
    return out


def delete_device(paths: ToolPaths, serial: str, names: list[str]) -> dict:
    """Delete from the device folder (rm inside it only)."""
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


def delete_everywhere(paths: ToolPaths, serial: str, names: list[str]) -> dict:
    """Trash: delete a name from Desktop/ + Device/ + device in one click.
    Stays deleted — sync only copies files that exist."""
    d1, b1 = _delete_where(desktop_dir(), names or [])
    d2, b2 = _delete_where(device_mirror_dir(), names or [])
    gone = sorted(set(d1) | set(d2))
    bad = [n for n in (b1 + b2) if n not in gone]
    dev: dict = {"ok": True, "deleted": [], "note": "no device picked"}
    if serial:
        if not _online(paths, serial):
            dev = {"ok": False, "deleted": [],
                   "note": f"{serial} offline — removed from desktop folders only"}
        else:
            dr = delete_device(paths, serial, names or [])
            dev = {"ok": dr["ok"], "deleted": dr.get("deleted", []),
                   "note": dr.get("err", "")}
            gone = sorted(set(gone) | set(dev["deleted"]))
    return {"ok": not bad, "deleted_pc": gone,
            "deleted_device": dev.get("deleted", []),
            "device_note": dev.get("note", ""),
            "err": (f"not found/refused: {', '.join(bad)}" if bad else "")}


def _pc_names() -> set[str]:
    return {p.name for p in desktop_dir().iterdir()}


def mirror(paths: ToolPaths, serial: str, direction: str) -> dict:
    """push: Desktop/ is boss — device extras deleted, then Desktop/ pushed.
    pull: full pull reconcile (Device/ := device)."""
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
    pl = pull(paths, serial)
    return {"ok": bool(pl.get("ok")), "direction": "pull",
            "deleted_from_pc": [s[1:] for s in pl.get("skipped", []) if s.startswith("-")],
            "copied_to_pc": pl.get("pulled", []),
            "err": pl.get("err") or ""}


# --- auto-sync: push Desktop/ + reconcile Device/ while online ---
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
            n = len(pr.get("pushed", [])) + len(pl.get("pulled", []))
            job["last_result"] = f"synced both ways ✓ ({n} new)" if n else "in sync ✓"
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
            "note": f"{state}. Desktop/ pushes up, Device/ mirrors down, every {interval}s."}


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
    ok = job.get("last_result", "").startswith("synced both ways") or \
        job.get("last_result", "") == "in sync ✓"
    return {"ok": ok, "result": job.get("last_result", ""),
            "pc": str(shared_dir()), "device_dir": DEVICE_DIR}
