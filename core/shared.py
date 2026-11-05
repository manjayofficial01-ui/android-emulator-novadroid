"""Two-folder sharing: Windows <-> emulator.

  NovaDroid-Shared/Desktop/  — stage files here to share them TO the device.
  NovaDroid-Shared/Device/   — exact readout of the device folder; pulls land here.

Push uploads Desktop/ to /sdcard/NovaDroid-Shared. Pull makes Device/ identical
to the device (new/changed downloaded, extras removed). The trash button deletes
a name from Desktop/ + Device/ + device at once, so nothing ever resurrects.
Transport is adb (no root, no guest tools), plus media-scanner broadcasts.
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from . import safe
from .config import ToolPaths
from .runner import run

DEVICE_DIR = "/sdcard/NovaDroid-Shared"
MAX_UPLOAD_MB = 250
GUIDE_NAME = "Put APKs, photos & files here.txt"
_SYNC_JOBS: dict[str, dict] = {}
_SYNC_JOBS_LOCK = threading.RLock()


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
    # Self-healing tidy, EVERY call (not marker-gated): the shared ROOT must
    # hold only folders + guide. Anything else is a stray — e.g. written by a
    # pre-1.2 agent whose pull target was the root — and gets filed into
    # Desktop/. Smaller-or-equal dupes are just deleted (often interrupted
    # transfers); bigger ones are kept with a suffix, never overwritten.
    for p in list(d.iterdir()):
        if not p.is_file() or p.name in (GUIDE_NAME,) or p.name.startswith("."):
            continue
        dest = d / "Desktop" / p.name
        try:
            if dest.exists():
                if p.stat().st_size <= dest.stat().st_size:
                    p.unlink()
                    continue
                dest = d / "Desktop" / (p.stem + " (root copy)" + p.suffix)
            p.rename(dest)
        except Exception:
            pass
    return d


def desktop_dir() -> Path:
    return shared_dir() / "Desktop"


def device_mirror_dir() -> Path:
    return shared_dir() / "Device"


def windows_desktop() -> Path:
    """The user's real Windows Desktop, best-effort resolved.

    ~\\Desktop can be redirected (OneDrive / non-English locale), so we ask
    the shell for CSIDL_DESKTOPDIRECTORY (0x10) and fall back to ~/Desktop.
    Always exists afterwards (created if missing).
    """
    d = Path.home() / "Desktop"
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        ctypes.windll.shell32.SHGetFolderPathW.argtypes = [
            wintypes.HWND, ctypes.c_int, wintypes.HANDLE, ctypes.c_uint,
            ctypes.c_wchar_p]
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)
        cand = Path(buf.value)
        if cand and cand.exists():
            d = cand
    except Exception:
        pass
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _ls(d: Path) -> list[dict]:
    try:
        files = [p for p in d.iterdir() if p.is_file()]
    except Exception:
        files = []
    out = []
    for p in sorted(files):
        try:
            # A file removed between iterdir() and stat() (concurrent trash
            # click, autosync pull) must not 500 the whole listing.
            st = p.stat()
        except OSError:
            continue
        out.append({"name": p.name, "bytes": st.st_size, "mtime": st.st_mtime})
    return out


def _confine(base: Path, name: str) -> Path | None:
    if not isinstance(name, str) or safe.has_control(name):
        return None
    clean = safe.safe_filename(name)
    if not clean:
        return None
    p = base / clean
    try:
        if p.resolve().parent != base.resolve():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return p


def _dev_name(name: str) -> str | None:
    """Direct child of DEVICE_DIR only (no `rm -rf /`).

    v5 keeps the strict v4 allowlist even though `safe.sh_quote()` now makes
    apostrophes harmless: refusing separators, quotes and control characters
    costs almost nothing and gives a second, independent line of defence.
    """
    if (not isinstance(name, str) or not name or len(name) > 255
            or safe.has_control(name)
            or "/" in name or "\\" in name or "'" in name):
        return None
    clean = safe.safe_filename(name)
    return clean or None


def _q(remote: str) -> str:
    """POSIX-quote a remote path — adb joins args with spaces and hands them
    to `/system/bin/sh -c`, so v4's naive `f"'{remote}'"` let a filename
    containing an apostrophe inject arbitrary device-shell commands."""
    return safe.sh_quote(remote)


def _check(paths: ToolPaths, serial: str) -> dict | None:
    if not paths.adb:
        return {"ok": False, "err": "adb not found"}
    if not serial:
        return {"ok": False, "err": "no device selected — Quickboot an AVD first"}
    if not safe.valid_serial(serial):
        return {"ok": False, "err": "invalid device serial"}
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
        clean = _dev_name(n)
        if not clean:
            continue
        # Whole URI is one POSIX-quoted word. A name like `a'; reboot; '`
        # can no longer terminate the quote and append a second command.
        uri = safe.sh_quote(f"file://{DEVICE_DIR}/{clean}")
        run([paths.adb, "-s", serial, "shell", "am", "broadcast",
             "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
             "-d", uri], timeout=10)


def _push_one(paths: ToolPaths, serial: str, src: Path, dest: str) -> bool:
    """Push a single file or dir. --sync skips unchanged files (fast re-syncs);
    older platform-tools reject the flag, so retry once without it."""
    r = run([paths.adb, "-s", serial, "push", "--sync", str(src), dest], timeout=300)
    if not r["ok"] and "unknown option" in (r["err"] + r["out"]).lower():
        r = run([paths.adb, "-s", serial, "push", str(src), dest], timeout=300)
    return r["ok"]


def push(paths: ToolPaths, serial: str, names: list[str] | None = None) -> dict:
    """Desktop/ -> device. Safe copy: never deletes."""
    if (e := _check(paths, serial)):
        return e
    if not _online(paths, serial):
        return {"ok": False, "err": f"{serial} is offline"}
    base = desktop_dir()
    if names:
        targets = [p for p in (_confine(base, n) for n in names) if p is not None]
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
    # Device-controlled names must not become local path components: on
    # Windows a backslash in a filename would let `mirror_dir / name` escape.
    want = {c for c in (_dev_name(f) for f in dev["files"]
                        if not f.endswith("/")) if c}
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


def export_to_desktop(paths: ToolPaths, serial: str, names: list[str],
                       source: str = "device") -> dict:
    """Copy selected shared files to the user's real Windows Desktop.

    source='device' pulls the named files from the emulator first (if online),
    then copies them out of the Device/ mirror. source='pc' copies straight
    from the Desktop/ staging folder. Never deletes; on a name clash on the
    Desktop the copy is kept with a '(NovaDroid)' suffix so nothing is clobbered.

    Executable-shaped names are refused (a malicious app can stage
    `Cleanup Tool.lnk` in /sdcard/NovaDroid-Shared and hope the operator
    double-clicks it — the Desktop is the one place that lure goes live),
    and symlinks/reparse points are never copied out.
    """
    if not names:
        return {"ok": False, "err": "no files selected"}
    dst = windows_desktop()
    if source == "device":
        if (e := _check(paths, serial)):
            return e
        if not _online(paths, serial):
            return {"ok": False, "err": f"{serial} is offline"}
        pull(paths, serial, names)  # best-effort: only the named files
        src_dir = device_mirror_dir()
    else:
        src_dir = desktop_dir()
    copied, failed, refused = [], [], []
    for n in names:
        clean = _dev_name(n)
        if not clean:
            continue
        if clean.lower().endswith(EXECUTABLE_EXTS):
            refused.append(clean)
            continue
        src = src_dir / clean
        if not src.exists() or not src.is_file() or src.is_symlink():
            failed.append(clean)
            continue
        target = dst / clean
        if target.exists():
            # Re-exports must not overwrite an earlier (NovaDroid) copy.
            i = 2
            cand = dst / f"{src.stem} (NovaDroid){src.suffix}"
            while cand.exists():
                cand = dst / f"{src.stem} (NovaDroid) {i}{src.suffix}"
                i += 1
            target = cand
        try:
            shutil.copy2(src, target)
            copied.append(target.name)
        except Exception:
            failed.append(clean)
    out: dict = {"ok": not (failed or refused), "desktop": str(dst),
                 "source": source, "copied": copied}
    if failed or refused:
        bits = []
        if failed:
            bits.append(f"not found/refused: {', '.join(failed)}")
        if refused:
            bits.append(f"executable files not exported: {', '.join(refused)}")
        out["err"] = "; ".join(bits)
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


#: Windows integrates these extensions with the shell — a double-click runs
#: code. Staged device/browser files with these names are refused on upload
#: AND on export-to-Desktop, where the lure actually gets clicked.
EXECUTABLE_EXTS = (".exe", ".bat", ".cmd", ".ps1", ".scr", ".pif", ".com",
                   ".hta", ".lnk", ".vbs", ".vbe", ".js", ".jse", ".wsf",
                   ".wsh", ".msi", ".msp", ".mst", ".cpl", ".jar", ".gadget",
                   ".diagcab", ".diagcfg", ".diagpack")


def upload(name: str, b64: str, paths: ToolPaths | None = None,
           push_serial: str = "") -> dict:
    """Browser upload -> Desktop/ staging (optionally straight onto the device).

    `host_path` is intentionally returned: the dashboard hands it straight
    back to /api/apk/install and /api/apk/analyze, both of which re-validate
    it with `safe.apk_path()` before touching the filesystem.
    """
    import base64 as _b64
    if not isinstance(name, str) or safe.has_control(name) or len(name) > 255:
        return {"ok": False, "err": "bad file name"}
    clean = safe.safe_filename(name)
    if not clean or clean in (".", "..") or clean.lower().endswith(EXECUTABLE_EXTS):
        return {"ok": False, "err": "bad file name"}
    try:
        raw = _b64.b64decode(b64 or "", validate=True)
    except Exception:
        return {"ok": False, "err": "not valid base64"}
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        return {"ok": False, "err": f"over {MAX_UPLOAD_MB}MB cap"}
    fp = _confine(desktop_dir(), clean)
    if fp is None:
        return {"ok": False, "err": "bad file name"}
    try:
        fp.write_bytes(raw)
    except OSError:
        return {"ok": False, "err": "could not write the uploaded file"}
    out: dict = {"ok": True, "file": clean, "bytes": len(raw), "folder": "Desktop",
                "host_path": str(fp)}
    if paths and push_serial:
        pr = push(paths, push_serial, [clean])
        out["pushed"] = pr.get("ok", False)
    return out


def reveal(which: str = "") -> dict:
    """Open the shared folder (or a subfolder) in Explorer (Windows).

    OPT-IN ONLY: the router refuses this endpoint unless the agent was
    started with --allow-gui. A REST API should not be able to spawn a GUI
    window on the operator's desktop by default.
    """
    import subprocess as _sp
    target = shared_dir() / Path(which).name if which in ("Desktop", "Device") else shared_dir()
    try:
        _sp.Popen(["explorer", str(target)])
        return {"ok": True}
    except OSError:
        return {"ok": False, "err": "could not open Explorer on this host"}


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
def _job_set(job: dict, **kv) -> None:
    """Job dicts are read by the HTTP thread and written by a worker thread."""
    with _SYNC_JOBS_LOCK:
        job.update(kv)


def _job_tick(paths: ToolPaths, serial: str, job: dict) -> None:
    """One mirror pass. Records telemetry so the UI can show what happened."""
    _job_set(job, last_run=time.time())
    try:
        if not _online(paths, serial):
            _job_set(job, waiting=True,
                     last_result=f"waiting for {serial} (offline)")
            return
        _job_set(job, waiting=False)
        pr = push(paths, serial)
        pl = pull(paths, serial)
        if pr.get("ok") and pl.get("ok"):
            n = len(pr.get("pushed", [])) + len(pl.get("pulled", []))
            _job_set(job, last_result=f"synced both ways ✓ ({n} new)" if n
                     else "in sync ✓")
        else:
            _job_set(job, last_result="push: " + (pr.get("err") or "ok")
                     + " · pull: " + (pl.get("err") or "ok"))
    except Exception as ex:
        # Generic to the client, detailed only in the agent's console.
        print(f"[autosync] tick failed ({type(ex).__name__}): {ex}")
        _job_set(job, last_result="sync failed — see agent console")


def autosync_set(paths: ToolPaths, serial: str, on: bool, interval: int = 15) -> dict:
    with _SYNC_JOBS_LOCK:
        old = _SYNC_JOBS.pop(serial, None) if serial else None
    if old:
        old["stop"].set()          # the OLD loop exits without deleting the NEW job
    if not on:
        return {"ok": True, "autosync": False}
    if not serial:
        return {"ok": False,
                "err": "Pick a device first (Quickboot an AVD) — nothing to sync with."}
    if not safe.valid_serial(serial):
        return {"ok": False, "err": "invalid device serial"}
    try:
        interval = max(5, min(int(interval or 15), 300))
    except (TypeError, ValueError):
        return {"ok": False, "err": "sync interval must be a number"}
    stop = threading.Event()
    job = {"stop": stop, "interval": interval, "since": time.time(),
           "last_run": 0, "last_result": "starting…", "waiting": True}

    def _loop():
        try:
            while not stop.wait(interval):
                _job_tick(paths, serial, job)
                if stop.is_set():
                    break
        finally:
            # v4 bug: an old loop that was still winding down popped the
            # freshly registered job for the same serial. Only remove self.
            with _SYNC_JOBS_LOCK:
                if _SYNC_JOBS.get(serial) is job:
                    _SYNC_JOBS.pop(serial, None)

    with _SYNC_JOBS_LOCK:
        _SYNC_JOBS[serial] = job
    threading.Thread(target=_loop, daemon=True,
                     name=f"nova-autosync-{serial}").start()
    state = ("online — syncing" if _online(paths, serial)
             else f"armed, waiting for {serial} to come online")
    return {"ok": True, "autosync": True, "interval": interval,
            "note": f"{state}. Desktop/ pushes up, Device/ mirrors down, every {interval}s."}


def autosync_status() -> dict:
    with _SYNC_JOBS_LOCK:
        jobs = {
            k: {"interval": v["interval"], "since": v["since"],
                "last_run": v.get("last_run", 0),
                "last_result": v.get("last_result", ""),
                "waiting": v.get("waiting", True)}
            for k, v in _SYNC_JOBS.items()}
    return {"ok": True, "jobs": jobs}


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
