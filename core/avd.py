"""AVD lifecycle via `android emulator` CLI + direct emulator binary fallback.

Cutting-edge defaults applied on top of stock AVDs:
 - Quickboot snapshots (fast boot 6-13s like BlueStacks, vs 50s cold)
 - gfxstream GPU auto (host -> ANGLE/SwiftShader/Lavapipe fallback)
 - Zero-config multi-instance networking (emulator 36.10+: Wi-Fi Direct/NSD
   work between AVDs with no manual port forwarding)
 - 4 vCPU / 4GB RAM baseline (API 37 floor + QEMU scaling research)

Multi-instance correctness: with three AVDs running, "stop B" must kill B.
The old `_serial_for_avd` returned the first online emulator regardless of
the name it was asked about, so with a fleet up you could stop the wrong VM.
Serials are now resolved per-device through the emulator console
(`adb emu avd name`) and cached for a few seconds.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import inifile
from .config import ToolPaths
from .runner import run, run_in_background

# Tracks background `emulator start` jobs so UI can poll state.
_JOBS: dict[str, dict] = {}
# name -> serial, rebuilt lazily (see _serial_map).
_SERIAL_MAP: dict[str, str] = {}
_MAP_TS = 0.0
_MAP_TTL = 5.0

# Files a clone must NOT inherit — they are per-instance state, not config.
_CLONE_EXCLUDE = ("userdata-qemu.img", "userdata-qemu.img.qcow2", "cache.img",
                  "cache.img.qcow2", "snapshots.img", "hardware-qemu.ini",
                  "emulator-user.ini", "sdcard.img", "userdata.img",
                  "userdata.img.qcow2")
_CLONE_EXCLUDE_DIRS = ("snapshots", "cache")


# --------------------------------------------------------------------------
# Listing / config
# --------------------------------------------------------------------------
def avd_home() -> Path:
    return Path.home() / ".android" / "avd"


def avd_dir(name: str) -> Path:
    return avd_home() / f"{name}.avd"


def _tune_keys() -> dict[str, str]:
    return {"hw.cpu.ncore": "4", "hw.ramSize": "4096",
            "vm.heapSize": "576", "hw.gpu.enabled": "yes",
            "hw.gpu.mode": "auto", "hw.keyboard": "yes",
            "hw.mainKeys": "no", "disk.dataPartition.size": "8G",
            "fastboot.forceColdBoot": "no"}


def _scan_avd_names() -> list[str]:
    """Every AVD present on disk, regardless of whether a tool lists it.

    `android emulator list` only reports AVDs that have a sibling `<name>.ini`.
    An AVD whose .ini was removed (or that was created by another tool) still
    boots fine but vanished from the dashboard — including when it was the
    instance actually running. Scanning the AVD home is the ground truth.
    """
    root = avd_home()
    names: set[str] = set()
    try:
        if root.exists():
            for p in root.iterdir():
                if p.is_dir() and p.name.endswith(".avd"):
                    n = p.name[:-4]
                elif p.is_file() and p.name.endswith(".ini"):
                    n = p.name[:-4]
                else:
                    continue
                if re.fullmatch(r"[\w.\-]+", n):
                    names.add(n)
    except OSError:
        pass
    return sorted(names)


def list_avds(paths: ToolPaths) -> dict:
    names: set[str] = set(_scan_avd_names())
    raw = ""
    if paths.android_cli:
        r = run([paths.android_cli, "emulator", "list"], timeout=60)
        raw = r["out"]
        for line in (r["out"] + "\n" + r["err"]).splitlines():
            name = line.strip().split()[0] if line.strip() else ""
            if not name or name.startswith(("-", "Available", "Usage", "No ")):
                continue
            if re.fullmatch(r"[\w.\-]+", name):
                names.add(name)
    elif paths.emulator:
        # No `android` CLI: the emulator binary can list AVDs on its own.
        r = run([paths.emulator, "-list-avds"], timeout=45)
        raw = r["out"]
        for line in (r["out"] + "\n" + r["err"]).splitlines():
            name = line.strip()
            if name and re.fullmatch(r"[\w.\-]+", name):
                names.add(name)
    if not names:
        return {"ok": False, "avds": [], "raw": raw[:500],
                "err": "no AVDs found — create one above (or install the `android` CLI)"}
    avds = [{"name": n} for n in sorted(names)]
    serials = _serial_map(paths)
    for a in avds:
        a.update(_avd_config(a["name"]))
        a["serial"] = serials.get(a["name"], "")
        a["running"] = bool(a["serial"])
        job = _JOBS.get(a["name"])
        if job:
            a["job"] = {k: v for k, v in job.items()
                        if k in ("state", "mode", "elapsed_s", "booted")}
    return {"ok": True, "avds": avds, "raw": r["out"][:2000]}


def _avd_config(name: str) -> dict:
    base = avd_dir(name) / "config.ini"
    info: dict = {}
    try:
        if base.exists():
            for line in base.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k in ("hw.cpu.ncore", "hw.ramSize", "hw.gpu.mode",
                             "image.sysdir.1", "hw.device.name", "PlayStore.enabled",
                             "hw.lcd.width", "hw.lcd.height", "hw.lcd.density",
                             "skin.name", "avd.ini.displayname", "hw.avd.name"):
                        info[k] = v.strip()
    except Exception:
        pass
    return {"config": info}


def list_profiles(paths: ToolPaths) -> dict:
    if not paths.android_cli:
        return {"ok": False, "profiles": []}
    r = run([paths.android_cli, "emulator", "create", "--list-profiles"], timeout=30)
    profiles = [l.strip() for l in r["out"].splitlines() if l.strip()]
    return {"ok": True, "profiles": profiles or
            ["small_phone", "medium_phone", "medium_tablet",
             "small_desktop", "medium_desktop", "large_desktop"]}


# --------------------------------------------------------------------------
# Serial <-> AVD mapping (multi-instance correctness)
# --------------------------------------------------------------------------
def _serial_map(paths: ToolPaths, force: bool = False) -> dict[str, str]:
    """{avd_name: serial}, cached for _MAP_TTL seconds.

    `adb emu avd name` asks the running instance who it is, which is the only
    reliable mapping — port ordering is not stable across reboots.
    """
    global _SERIAL_MAP, _MAP_TS
    now = time.time()
    if not force and _SERIAL_MAP and now - _MAP_TS < _MAP_TTL:
        return _SERIAL_MAP
    if not paths.adb:
        return _SERIAL_MAP
    r = run([paths.adb, "devices"], timeout=15)
    serials = [parts[0] for parts in (l.split() for l in r["out"].splitlines())
               if len(parts) >= 2 and parts[1] == "device"
               and parts[0].startswith("emulator-")]
    if not serials:
        _SERIAL_MAP, _MAP_TS = {}, now
        return _SERIAL_MAP

    def _ask(serial: str) -> tuple[str, str]:
        res = run([paths.adb, "-s", serial, "emu", "avd", "name"], timeout=12)
        name = ""
        for line in res["out"].splitlines():
            line = line.strip()
            if line and line.upper() not in ("OK", "KO"):
                name = line
                break
        return serial, name

    mapping: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(serials))) as pool:
        for serial, name in pool.map(_ask, serials):
            if name:
                mapping[name] = serial
    _SERIAL_MAP, _MAP_TS = mapping, now
    return mapping


def serial_for_avd(paths: ToolPaths, name: str) -> str:
    """The serial of the running AVD called `name`, else ""."""
    return _serial_map(paths).get(name, "") or _serial_map(paths, force=True).get(name, "")


# --------------------------------------------------------------------------
# Create / clone / remove
# --------------------------------------------------------------------------
def create_avd(paths: ToolPaths, profile: str = "medium_phone") -> dict:
    if not paths.android_cli:
        return {"ok": False, "err": "`android` CLI not on PATH"}
    before = {a["name"] for a in list_avds(paths).get("avds", [])}
    # A first create downloads the system image; 300s was too tight on a
    # cold cache and silently killed the download mid-flight.
    r = run([paths.android_cli, "emulator", "create", profile], timeout=1800)
    after = list_avds(paths).get("avds", [])
    new = [a for a in after if a["name"] not in before]
    name = new[0]["name"] if new else None
    ok = r["ok"] or name is not None
    if ok and name:
        _tune_avd_config(name)
    return {"ok": ok, "name": name, "avds": after,
            "out": (r["out"] + "\n" + r["err"]).strip()[-2000:]}


def _tune_avd_config(name: str) -> None:
    """Apply the NovaDroid performance baseline to a fresh AVD config.ini."""
    cfg = avd_dir(name) / "config.ini"
    if not cfg.exists():
        return
    try:
        inifile.upsert(cfg, _tune_keys())
        inifile.upsert(cfg, {"hw.avd.name": name})
    except OSError:
        pass


def clone_avd(paths: ToolPaths, src: str, dst: str) -> dict:
    """Duplicate an AVD's *configuration* into a fresh instance.

    Multi-instance farming wants N identical devices, not N downloads. User
    data, snapshots and caches are deliberately excluded so a clone boots as
    a clean device with the same hardware profile, skin and resolution — and
    the copy takes seconds instead of copying 8 GB of userdata.
    """
    src = (src or "").strip()
    dst = (dst or "").strip()
    if not re.fullmatch(r"[\w.\-]+", src) or not re.fullmatch(r"[\w.\-]+", dst):
        return {"ok": False, "err": "AVD names may only use letters, digits, . _ -"}
    if src == dst:
        return {"ok": False, "err": "source and target must differ"}
    src_dir, dst_dir = avd_dir(src), avd_dir(dst)
    src_ini, dst_ini = avd_home() / f"{src}.ini", avd_home() / f"{dst}.ini"
    if not src_dir.exists() or not src_ini.exists():
        return {"ok": False, "err": f"AVD not found: {src}"}
    if dst_dir.exists() or dst_ini.exists():
        return {"ok": False, "err": f"target already exists: {dst}"}

    def _ignore(directory: str, names: list[str]) -> set[str]:
        drop = {n for n in names if n in _CLONE_EXCLUDE_DIRS
                or any(n == e or n.startswith(e + ".") for e in _CLONE_EXCLUDE)}
        return drop

    try:
        shutil.copytree(src_dir, dst_dir, ignore=_ignore, symlinks=False)
        ini = inifile.parse(src_ini)
        ini["avd.ini.displayname"] = dst
        ini["path"] = str(dst_dir)
        # path.rel is what the emulator actually resolves first.
        ini["path.rel"] = f"avd/{dst}.avd"
        dst_ini.write_text(
            "\n".join(f"{k}={v}" for k, v in ini.items()) + "\n", encoding="utf-8")
        inifile.upsert(dst_dir / "config.ini", {"hw.avd.name": dst})
    except (OSError, shutil.Error) as e:
        shutil.rmtree(dst_dir, ignore_errors=True)
        try:
            dst_ini.unlink()
        except OSError:
            pass
        return {"ok": False, "err": f"clone failed: {e}"}
    return {"ok": True, "src": src, "name": dst, "dir": str(dst_dir),
            "note": ("Cloned configuration only (no user data) — the clone boots "
                     "as a fresh device. Great for multi-instance fleets.")}


def remove_avd(paths: ToolPaths, name: str) -> dict:
    if not re.fullmatch(r"[\w.\-]+", name or ""):
        return {"ok": False, "err": "invalid AVD name"}
    out = ""
    if paths.android_cli:
        r = run([paths.android_cli, "emulator", "remove", name], timeout=180)
        out = (r["out"] + r["err"]).strip()[-1000:]
        removed = r["ok"]
    else:
        removed = False
    if not removed:
        # The CLI cannot remove an AVD it cannot see (missing .ini). Since
        # "delete" is explicit and confirmed in the UI, finish the job on disk.
        target_dir, target_ini = avd_dir(name), avd_home() / f"{name}.ini"
        if not target_dir.exists() and not target_ini.exists():
            return {"ok": False, "err": f"AVD not found: {name}", "out": out}
        shutil.rmtree(target_dir, ignore_errors=True)
        try:
            target_ini.unlink()
        except OSError:
            pass
        removed = not target_dir.exists() and not target_ini.exists()
        out = (out + " | removed from disk").strip(" |")
    _JOBS.pop(name, None)
    _SERIAL_MAP.pop(name, None)
    return {"ok": removed, "out": out}


# --------------------------------------------------------------------------
# Start / stop
# --------------------------------------------------------------------------
def start_avd(paths: ToolPaths, name: str, cold: bool = False) -> dict:
    if not name:
        return {"ok": False, "err": "no AVD name given"}
    if not paths.android_cli:
        return {"ok": False, "err": "`android` CLI not on PATH"}
    if serial_for_avd(paths, name):
        return {"ok": True, "already": True, "job": _JOBS.get(name, {}),
                "note": f"{name} is already running."}
    cmd = [paths.android_cli, "emulator", "start", name] + (["--cold"] if cold else [])
    job = {"state": "starting", "mode": "cold" if cold else "quickboot",
           "t0": time.time(), "elapsed_s": 0.0, "booted": False, "log": ""}
    _JOBS[name] = job

    def _done(res: dict):
        started = _JOBS.get(name)
        t0 = started["t0"] if started else time.time()
        job.update({"t0": t0,
                    "log": (res["out"] + res["err"])[-2000:],
                    "elapsed_s": round(time.time() - t0, 1)})
        if res["ok"]:
            # The CLI exits as soon as it has spawned QEMU — that is NOT a
            # booted device. Follow through to a real boot_completed so the
            # dashboard never shows "ready" on a black screen.
            job["state"] = "booting"
            _JOBS[name] = job
            serial = _await_serial(paths, name, timeout=300)
            if not serial:
                job["state"] = "error"
                job["log"] = (job.get("log", "") + " | no serial appeared").strip()
                return
            job["serial"] = serial
            from . import apps as APPS
            res2 = APPS.wait_boot(paths, serial, timeout=600)
            job["booted"] = bool(res2.get("booted"))
            job["state"] = "ready" if job["booted"] else "error"
            job["elapsed_s"] = round(time.time() - t0, 1)
            if not job["booted"]:
                job["log"] = (job.get("log", "") + " | " + str(res2.get("err", ""))).strip()
        else:
            job["state"] = "error"
        _JOBS[name] = job

    run_in_background(cmd, _done, timeout=1800)
    mode = "cold boot (no snapshot)" if cold else "quickboot snapshot"
    return {"ok": True, "job": job, "mode": mode,
            "note": ("First boot takes minutes (Play image provisioning); later "
                     "quickboots take seconds. Job state advances to 'ready' "
                     "when sys.boot_completed is set.")}


def _await_serial(paths: ToolPaths, name: str, timeout: int = 300) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = serial_for_avd(paths, name)
        if s:
            return s
        time.sleep(2)
    return ""


def start_avd_raw(paths: ToolPaths, name: str, extra: list[str] | None = None) -> dict:
    """Power-user path: launch emulator.exe directly for flags the CLI hides.

    Exposes cutting-edge knobs: -gpu host/swiftshader_indirect/lavapipe,
    -feature VulkanNativeSwapchain, -no-snapshot, -memory, -cores.
    """
    if not name:
        return {"ok": False, "err": "no AVD name given"}
    if not paths.emulator:
        return {"ok": False, "err": "emulator.exe not found in SDK"}
    extra = [str(a) for a in (extra or []) if str(a).strip()]
    # Never let a stray flag turn this into an arbitrary process launch.
    if any(a.startswith((";", "&", "|")) or "\n" in a for a in extra):
        return {"ok": False, "err": "illegal character in emulator flags"}
    cmd = [paths.emulator, f"@{name}"] + extra
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        _JOBS[name] = {"state": "booting", "mode": "raw", "t0": time.time(),
                       "elapsed_s": 0.0, "booted": False, "log": " ".join(extra)}
        return {"ok": True, "cmd": " ".join(cmd)}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def stop_avd(paths: ToolPaths, name: str) -> dict:
    global _MAP_TS
    if not name:
        return {"ok": False, "err": "no AVD name given"}
    if not paths.android_cli:
        return {"ok": False, "err": "`android` CLI not on PATH"}
    r = run([paths.android_cli, "emulator", "stop", name], timeout=180)
    # Fallback: kill by the serial that actually belongs to THIS AVD.
    if not r["ok"] and paths.adb:
        serial = serial_for_avd(paths, name)
        if serial:
            run([paths.adb, "-s", serial, "emu", "kill"], timeout=45)
    _JOBS.pop(name, None)
    _SERIAL_MAP.pop(name, None)
    _MAP_TS = 0.0
    return {"ok": True, "out": (r["out"] + r["err"]).strip()[-1000:]}


def fleet(paths: ToolPaths, names: list[str], action: str = "start",
          cold: bool = False, stagger: int = 8) -> dict:
    """Start or stop several AVDs, spaced out so they don't thrash the disk.

    Booting four AVDs at once makes all four slow — the image reads are the
    bottleneck, not the CPU. A stagger lets each one get through its
    snapshot restore before the next starts.
    """
    names = [str(n) for n in (names or []) if str(n).strip()]
    if not names:
        return {"ok": False, "err": "no AVD names given"}
    if action not in ("start", "stop"):
        return {"ok": False, "err": "action must be 'start' or 'stop'"}
    results: list[dict] = []
    for i, name in enumerate(names):
        if i and action == "start":
            time.sleep(max(0, min(int(stagger), 60)))
        res = (start_avd(paths, name, cold) if action == "start"
               else stop_avd(paths, name))
        results.append({"name": name, "ok": bool(res.get("ok")),
                        "err": res.get("err", "")})
    return {"ok": any(r["ok"] for r in results), "action": action,
            "results": results,
            "note": f"{action}ed {sum(r['ok'] for r in results)}/{len(results)} AVDs"}


def job_state(name: str) -> dict:
    job = dict(_JOBS.get(name, {"state": "unknown"}))
    if "t0" in job:
        job["elapsed_s"] = round(time.time() - job["t0"], 1)
    return job


# --------------------------------------------------------------------------
# Wipe / snapshots
# --------------------------------------------------------------------------
def wipe_data(name: str) -> dict:
    """Delete derived state for a clean boot.

    Removes generated hardware config, the snapshot image and user data. The
    old version computed a `snapshots` dir and then never used it, and never
    deleted directories — so "wipe" left the quickboot chain intact and the
    next boot still restored a stale snapshot.
    """
    if not re.fullmatch(r"[\w.\-]+", name or ""):
        return {"ok": False, "err": "invalid AVD name"}
    root = avd_dir(name)
    if not root.exists():
        return {"ok": False, "err": f"AVD not found: {name}"}
    removed: list[str] = []
    for f in ("hardware-qemu.ini", "snapshots.img", "userdata-qemu.img",
              "userdata-qemu.img.qcow2", "cache.img", "cache.img.qcow2"):
        p = root / f
        try:
            if p.is_file():
                p.unlink()
                removed.append(f)
        except OSError:
            pass
    snapdir = root / "snapshots"
    if snapdir.is_dir():
        # A quickboot chain lives here; leaving it means the next boot still
        # restores the old state, which is exactly what "wipe" promises not to.
        shutil.rmtree(snapdir, ignore_errors=True)
        if not snapdir.exists():
            removed.append("snapshots/")
    _JOBS.pop(name, None)
    _SERIAL_MAP.pop(name, None)
    return {"ok": True, "removed": removed, "avd": name,
            "note": ("Wiped user data + quickboot snapshots. Next boot is a "
                     "clean first boot (minutes on a Play image).")}


def snapshots(paths: ToolPaths, serial: str) -> dict:
    """List quickboot snapshots on a running AVD."""
    if not serial or not paths.adb:
        return {"ok": False, "err": "no running device selected"}
    r = run([paths.adb, "-s", serial, "emu", "avd", "snapshot", "list"], timeout=40)
    raw = (r["out"] + "\n" + r["err"]).strip()
    names = [l.strip() for l in raw.splitlines()
             if l.strip() and l.strip().lower() not in ("ok", "ko")
             and not l.lower().startswith(("name", "id", "---"))]
    return {"ok": bool(r["ok"]), "snapshots": names, "raw": raw[:1500]}


def snapshot(paths: ToolPaths, serial: str, action: str, name: str = "") -> dict:
    """save / load / del a quickboot snapshot on a running AVD."""
    if not serial or not paths.adb:
        return {"ok": False, "err": "no running device selected"}
    action = (action or "").strip().lower()
    if action not in ("save", "load", "del"):
        return {"ok": False, "err": "action must be save, load or del"}
    if not re.fullmatch(r"[\w.\-]*", name or ""):
        return {"ok": False, "err": "invalid snapshot name"}
    cmd = [paths.adb, "-s", serial, "emu", "avd", "snapshot", action]
    if action == "save":
        cmd.append(name or "default_boot")
    elif action == "load":
        cmd.append(name or "default_boot")
    else:
        cmd.append(name or "")
    r = run([a for a in cmd if a], timeout=180)
    return {"ok": bool(r["ok"]), "action": action,
            "log": (r["out"] + r["err"]).strip()[-800:]}
