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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import inifile, safe
from .config import ToolPaths
from .runner import run, run_in_background

# Tracks background `emulator start` jobs so UI can poll state.
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.RLock()
# Raw boots are DETACHED processes. v4 threw away the Popen handle, so the OS
# kept a zombie entry until the agent exited and there was no way to report a
# launch that died instantly. Keep handles and reap finished ones on demand.
_RAW_PROCS: list = []
_RAW_LOCK = threading.Lock()
# name -> serial, rebuilt lazily (see _serial_map).
_SERIAL_MAP: dict[str, str] = {}
_MAP_TS = 0.0
_MAP_TTL = 5.0
# The dashboard is served by a THREADING server: `_serial_map` rebuilds a
# module-global while other requests read it. Without a lock two threads can
# interleave a read of a half-built dict (and the "is it stale" check raced
# with the write, so a refresh could see {}) — the fleet panel then flickered
# between "running" and "" on every poll.
_MAP_LOCK = threading.RLock()

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
    """AVD directory. Refuse traversal at the lowest possible layer.

    Every caller used to be responsible for validating `name` first; three
    forgot (window.py's read/write/fix), which turned `?name=../..` into an
    arbitrary `.ini` read/write primitive. Defence in depth belongs here.
    """
    if not safe.valid_avd_name(name):
        raise ValueError("invalid AVD name")
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
    # `r` must exist even when neither tool is present: the disk scan above is
    # the ground truth and can list AVDs that no CLI can see. Referencing `r`
    # unconditionally at the end used to raise UnboundLocalError -> HTTP 500
    # on every dashboard load for anyone with a partially installed SDK.
    r = {"ok": False, "out": "", "err": "no AVD listing tool available"}
    if paths.android_cli:
        r = run([paths.android_cli, "emulator", "list"], timeout=60)
        for line in (r["out"] + "\n" + r["err"]).splitlines():
            name = line.strip().split()[0] if line.strip() else ""
            if not name or name.startswith(("-", "Available", "Usage", "No ")):
                continue
            if re.fullmatch(r"[\w.\-]+", name):
                names.add(name)
    elif paths.emulator:
        # No `android` CLI: the emulator binary can list AVDs on its own.
        r = run([paths.emulator, "-list-avds"], timeout=45)
        for line in (r["out"] + "\n" + r["err"]).splitlines():
            name = line.strip()
            if name and re.fullmatch(r"[\w.\-]+", name):
                names.add(name)
    if not names:
        return {"ok": False, "avds": [], "raw": r["out"][:500],
                "err": "no AVDs found — create one above (or install the `android` CLI)"}
    avds = [{"name": n} for n in sorted(names)]
    serials = _serial_map(paths)
    for a in avds:
        a.update(_avd_config(a["name"]))
        a["serial"] = serials.get(a["name"], "")
        a["running"] = bool(a["serial"])
        with _JOBS_LOCK:
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
    with _MAP_LOCK:
        if not force and _SERIAL_MAP and now - _MAP_TS < _MAP_TTL:
            return dict(_SERIAL_MAP)
        if not paths.adb:
            return dict(_SERIAL_MAP)
        # Hold the lock across the whole rebuild: the adb round-trips take
        # ~200ms and without this a concurrent refresh could observe the map
        # mid-write (or the empty placeholder below) and report a running
        # fleet as stopped.
        r = run([paths.adb, "devices"], timeout=15)
        serials = [parts[0] for parts in (l.split() for l in r["out"].splitlines())
                   if len(parts) >= 2 and parts[1] == "device"
                   and parts[0].startswith("emulator-")]
        if not serials:
            _SERIAL_MAP, _MAP_TS = {}, now
            return {}

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
        return dict(mapping)


def serial_for_avd(paths: ToolPaths, name: str, refresh: bool = False) -> str:
    """The serial of the running AVD called `name`, else "".

    `refresh=True` bypasses the TTL — used right after a kill, where the
    cached map would otherwise still list the device we just stopped.
    """
    if refresh:
        return _serial_map(paths, force=True).get(name, "")
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
    with _JOBS_LOCK:
        _JOBS.pop(name, None)
    with _MAP_LOCK:
        _SERIAL_MAP.pop(name, None)
    return {"ok": removed, "out": out}


# --------------------------------------------------------------------------
# Start / stop
# --------------------------------------------------------------------------
def start_avd(paths: ToolPaths, name: str, cold: bool = False) -> dict:
    if not safe.valid_avd_name(name):
        return {"ok": False, "err": "invalid AVD name"}
    if not paths.android_cli:
        return {"ok": False, "err": "`android` CLI not on PATH"}
    if serial_for_avd(paths, name):
        with _JOBS_LOCK:
            existing = dict(_JOBS.get(name, {}))
        return {"ok": True, "already": True, "job": existing,
                "note": f"{name} is already running."}
    cmd = [paths.android_cli, "emulator", "start", name] + (["--cold"] if cold else [])
    job = {"state": "starting", "mode": "cold" if cold else "quickboot",
           "t0": time.time(), "elapsed_s": 0.0, "booted": False, "log": ""}
    with _JOBS_LOCK:
        _JOBS[name] = job

    def _commit(state: str) -> None:
        job["state"] = state
        job["elapsed_s"] = round(time.time() - job["t0"], 1)
        with _JOBS_LOCK:
            _JOBS[name] = job

    def _done(res: dict):
        job["log"] = ((res.get("out") or "") + (res.get("err") or ""))[-2000:]
        if not res.get("ok"):
            _commit("error")
            return
        # The CLI exits as soon as it has spawned QEMU — that is NOT a booted
        # device. Follow through to a real boot_completed so the dashboard
        # never shows "ready" on a black screen.
        _commit("booting")
        try:
            serial = _await_serial(paths, name, timeout=300)
            if not serial:
                job["log"] = (job.get("log", "") + " | no serial appeared").strip()
                _commit("error")
                return
            job["serial"] = serial
            from . import apps as APPS
            res2 = APPS.wait_boot(paths, serial, timeout=600)
            job["booted"] = bool(res2.get("booted"))
            if not job["booted"]:
                job["log"] = (job.get("log", "") + " | "
                              + str(res2.get("err", ""))).strip()
            _commit("ready" if job["booted"] else "error")
        except Exception as ex:
            # Any exception here used to leave the job pinned at "booting"
            # forever. Surface it and terminate the state machine honestly.
            job["log"] = (job.get("log", "") +
                          f" | boot watch failed: {type(ex).__name__}").strip()
            job["booted"] = False
            _commit("error")

    run_in_background(cmd, _done, timeout=1800)
    mode = "cold boot (no snapshot)" if cold else "quickboot snapshot"
    return {"ok": True, "job": dict(job), "mode": mode,
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


#: Flags that mount/redirect host-backed disk images or pass raw QEMU
#: arguments. shell=False already blocks second processes, but `-qemu -hda
#: file=C:/…` would mount an arbitrary host file into the guest (a read
#: primitive, and QEMU may write back to -sdcard/-data). Everything else the
#: operator legitimately needs (-gpu, -cores, -memory, -camera-*, -feature,
#: -no-*) stays available.
_QEMU_UNSAFE_FLAGS = {
    "-qemu", "-hda", "-hdb", "-hdc", "-hdd", "-drive", "-sdcard",
    "-initdata", "-initrd", "-data", "-cache", "-datadir",
}


def start_avd_raw(paths: ToolPaths, name: str, extra: list[str] | None = None,
                  affinity: int = 0) -> dict:
    """Power-user path: launch emulator.exe directly for flags the CLI hides.

    Exposes cutting-edge knobs: -gpu host/swiftshader_indirect/lavapipe,
    -feature VulkanNativeSwapchain, -no-snapshot, -memory, -cores.
    `affinity` (optional CPU mask) pins the process to P-cores on Intel
    hybrid CPUs — see core/perf.detect_hybrid for why that matters.
    """
    if not safe.valid_avd_name(name):
        return {"ok": False, "err": "invalid AVD name"}
    if not paths.emulator:
        return {"ok": False, "err": "emulator.exe not found in SDK"}
    extra = [str(a) for a in (extra or []) if str(a).strip()]
    # shell=False means these cannot launch a second process, but a malformed
    # flag can still make QEMU do something surprising. Allow only known-safe
    # flag characters and keep the count bounded.
    if len(extra) > 32 or any(len(a) > 120 or safe.has_control(a)
                              or not re.fullmatch(r"[A-Za-z0-9_\-./:=,+@]+", a)
                              for a in extra):
        return {"ok": False, "err": "illegal character in emulator flags"}
    blocked = [a for a in extra if a.lower() in _QEMU_UNSAFE_FLAGS]
    if blocked:
        return {"ok": False,
                "err": (f"refused: {', '.join(blocked)} can mount host files "
                        f"into the guest — not allowed via the API")}
    # 36.4.9+: Vulkan composition and Quickboot snapshots are incompatible.
    # Warn rather than refuse — the operator may know exactly what they want.
    note = ""
    low = " ".join(extra).lower()
    if ("vulkan" in low or "-gpu" in low and "host" not in low) and \
            "-no-snapshot" not in low:
        note = ("Vulkan + Quickboot snapshots do not mix on 36.4.9+ — "
                "add -no-snapshot if the boot hangs on a black screen.")
    cmd = [paths.emulator, f"@{name}"] + extra
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    except OSError:
        return {"ok": False, "err": "could not launch emulator.exe"}
    pinned = False
    if affinity and affinity > 0:
        try:
            import ctypes
            h = int(affinity) & 0xFFFFFFFFFFFFFFFF
            pinned = bool(ctypes.windll.kernel32.SetProcessAffinityMask(
                int(proc.handle), h))
        except Exception as e:
            print(f"[raw] affinity pin failed ({type(e).__name__}): {e}")
        if pinned:
            note = (note + " | pinned to P-cores").strip(" |")
    with _RAW_LOCK:
        _RAW_PROCS[:] = [p for p in _RAW_PROCS if p.poll() is None][-31:]
        _RAW_PROCS.append(proc)
    state = {"state": "booting", "mode": "raw", "t0": time.time(),
             "elapsed_s": 0.0, "booted": False, "log": " ".join(extra),
             "pid": proc.pid, "note": note}
    with _JOBS_LOCK:
        _JOBS[name] = state
    return {"ok": True, "cmd": " ".join(cmd), "pid": proc.pid,
            "flags": extra, "affinity_pinned": pinned, "note": note}


def stop_avd(paths: ToolPaths, name: str) -> dict:
    """Stop one AVD and report the TRUTH.

    The old version returned ok=True unconditionally, so a failed stop still
    told the dashboard "Stopped: 1/1" while the VM kept running — which is
    exactly how a zombie qemu ends up holding the multi-instance lock.
    """
    global _MAP_TS
    if not safe.valid_avd_name(name):
        return {"ok": False, "err": "invalid AVD name"}
    out, stopped, how = "", False, ""
    if paths.android_cli:
        r = run([paths.android_cli, "emulator", "stop", name], timeout=180)
        out = (r["out"] + r["err"]).strip()[-1000:]
        stopped = bool(r["ok"])
        how = "android-cli"
    # Fallback: kill by the serial that actually belongs to THIS AVD. This is
    # also the PRIMARY path on a CLI-less machine — a raw-started AVD used to
    # be un-stoppable through the API there.
    if not stopped and paths.adb:
        serial = serial_for_avd(paths, name)
        if serial:
            k = run([paths.adb, "-s", serial, "emu", "kill"], timeout=45)
            stopped, how = bool(k["ok"]), f"adb emu kill ({serial})"
            out = (out + " | " + (k["out"] + k["err"]).strip()).strip(" |")[-1000:]
    if not stopped and not paths.adb and not paths.android_cli:
        return {"ok": False,
                "err": "neither `android` CLI nor adb found — cannot stop the AVD"}
    with _JOBS_LOCK:
        _JOBS.pop(name, None)
    with _MAP_LOCK:
        _SERIAL_MAP.pop(name, None)
        _MAP_TS = 0.0
    # Confirm it is really gone before claiming success.
    if stopped and paths.adb:
        time.sleep(0.6)
        if serial_for_avd(paths, name, refresh=True):
            stopped = False
            out = (out + " | still online after kill").strip(" |")
    return {"ok": stopped, "via": how if stopped else "",
            "out": out or ("stopped" if stopped else f"could not stop {name}")}


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
    good = sum(r["ok"] for r in results)
    # v4 used `any()`: a fleet start where 3 of 4 AVDs failed still reported
    # success, and the dashboard toasted "Started: 1/4" as a win.
    return {"ok": good == len(results), "action": action,
            "results": results, "succeeded": good, "total": len(results),
            "note": f"{action}ed {good}/{len(results)} AVDs"}


def job_state(name: str) -> dict:
    with _JOBS_LOCK:
        job = dict(_JOBS.get(name, {"state": "unknown"}))
    if "t0" in job:
        job["elapsed_s"] = round(time.time() - job["t0"], 1)
    job.pop("proc", None)   # never serialise a Popen/Thread object
    return job


# --------------------------------------------------------------------------
# Wipe / snapshots
# --------------------------------------------------------------------------
def wipe_data(paths: ToolPaths, name: str) -> dict:
    """Delete derived state for a clean boot.

    Removes generated hardware config, the snapshot image and user data. The
    old version computed a `snapshots` dir and then never used it, and never
    deleted directories — so "wipe" left the quickboot chain intact and the
    next boot still restored a stale snapshot. v5.0.1 additionally refuses to
    run against a live AVD (Windows locks userdata-qemu.img, every unlink
    failed silently, and the toast still claimed success) and reports files
    it could not remove instead of hard-coding ok=True.
    """
    if not re.fullmatch(r"[\w.\-]+", name or ""):
        return {"ok": False, "err": "invalid AVD name"}
    root = avd_dir(name)
    if not root.exists():
        return {"ok": False, "err": f"AVD not found: {name}"}
    if serial_for_avd(paths, name, refresh=True):
        return {"ok": False,
                "err": f"{name} is running — stop it before wiping its data"}
    removed: list[str] = []
    locked: list[str] = []
    for f in ("hardware-qemu.ini", "snapshots.img", "userdata-qemu.img",
              "userdata-qemu.img.qcow2", "cache.img", "cache.img.qcow2"):
        p = root / f
        try:
            if p.is_file():
                p.unlink()
                removed.append(f)
        except OSError:
            locked.append(f)
    snapdir = root / "snapshots"
    if snapdir.is_dir():
        # A quickboot chain lives here; leaving it means the next boot still
        # restores the old state, which is exactly what "wipe" promises not to.
        shutil.rmtree(snapdir, ignore_errors=True)
        if not snapdir.exists():
            removed.append("snapshots/")
        else:
            locked.append("snapshots/")
    with _JOBS_LOCK:
        _JOBS.pop(name, None)
    with _MAP_LOCK:
        _SERIAL_MAP.pop(name, None)
    if locked:
        return {"ok": False, "avd": name, "removed": removed, "locked": locked,
                "err": (f"could not remove {len(locked)} item(s) — is another "
                        "emulator process holding the AVD open?")}
    return {"ok": True, "removed": removed, "avd": name,
            "note": ("Already clean — nothing to wipe." if not removed else
                     "Wiped user data + quickboot snapshots. Next boot is a "
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


# --------------------------------------------------------------------------
# v4.0.0 — AVD toolkit: disk usage, cleanup, backup/restore, rename
# --------------------------------------------------------------------------
def _dir_size(p: Path) -> int:
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


_RECLAIMABLE = ("cache.img", "cache.img.qcow2")


def disk_usage(name: str) -> dict:
    """Per-AVD disk accounting with a breakdown — BlueStacks' 'Disk Cleanup'
    panel, but honest about where the bytes actually are.

    Snapshots (quickboot chains) and cache images are the two silent multi-GB
    growers; userdata is the one you actually care about keeping.
    """
    if not re.fullmatch(r"[\w.\-]+", name or ""):
        return {"ok": False, "err": "invalid AVD name"}
    root = avd_dir(name)
    if not root.exists():
        return {"ok": False, "err": f"AVD not found: {name}"}
    groups = {"userdata": 0, "snapshots": 0, "cache": 0, "other": 0}
    snapdir = root / "snapshots"
    groups["snapshots"] = _dir_size(snapdir) if snapdir.exists() else 0
    for f in root.iterdir():
        try:
            if not f.is_file():
                continue
            size = f.stat().st_size
        except OSError:
            continue
        base = f.name
        if base.startswith("userdata") or base == "sdcard.img":
            groups["userdata"] += size
        elif base in _RECLAIMABLE:
            groups["cache"] += size
        else:
            groups["other"] += size
    total = sum(groups.values())
    return {"ok": True, "avd": name, "dir": str(root),
            "bytes": total, "mb": round(total / 1e6, 1),
            "breakdown_mb": {k: round(v / 1e6, 1) for k, v in groups.items()},
            "reclaimable_mb": round((groups["snapshots"] + groups["cache"]) / 1e6, 1)}


def cleanup(paths: ToolPaths, name: str, serial: str = "") -> dict:
    """Reclaim disk: drop quickboot snapshots + cache images, fstrim the guest.

    userdata is never touched, so installed apps and their data survive —
    this is 'clean the fat', not wipe_data's 'factory reset'. If the AVD is
    running, `sm fstrim` also returns free guest blocks to the host image.
    """
    if not re.fullmatch(r"[\w.\-]+", name or ""):
        return {"ok": False, "err": "invalid AVD name"}
    root = avd_dir(name)
    if not root.exists():
        return {"ok": False, "err": f"AVD not found: {name}"}
    freed = 0
    for f in root.iterdir():
        try:
            if f.is_file() and f.name in _RECLAIMABLE:
                freed += f.stat().st_size
                f.unlink()
        except OSError:
            pass
    snapdir = root / "snapshots"
    if snapdir.is_dir():
        freed += _dir_size(snapdir)
        shutil.rmtree(snapdir, ignore_errors=True)
        if snapdir.exists():
            return {"ok": False, "err": "could not remove snapshots/ (AVD running?)"}
    trimmed = False
    if serial and paths.adb:
        t = run([paths.adb, "-s", serial, "shell", "sm fstrim; echo ok"], timeout=60)
        trimmed = "ok" in (t["out"] or "")
    return {"ok": True, "avd": name,
            "freed_mb": round(freed / 1e6, 1), "guest_trimmed": trimmed,
            "note": ("Snapshots + cache images deleted (userdata kept). "
                     "Next boot is a fresh cold boot.")}


BACKUP_DIR = Path.home() / "NovaDroid-Shared" / "Backups"


def backup(name: str) -> dict:
    """Zip an AVD (config + userdata + .ini) into NovaDroid-Shared/Backups.

    MuMu/LDPlayer-style instance backup, but portable: the zip contains
    everything needed to recreate the instance on another machine. Volatile
    generated files (hardware-qemu.ini, cache) are excluded — they are
    recreated on boot and only bloat the archive.
    """
    import zipfile
    if not re.fullmatch(r"[\w.\-]+", name or ""):
        return {"ok": False, "err": "invalid AVD name"}
    root = avd_dir(name)
    ini = avd_home() / f"{name}.ini"
    if not root.exists() or not ini.exists():
        return {"ok": False, "err": f"AVD not found: {name}"}
    with _MAP_LOCK:
        serial = _SERIAL_MAP.get(name, "")
    if serial:
        return {"ok": False,
                "err": f"{name} is running — stop it first (userdata must be "
                       "quiesced or the backup captures a half-written image)"}
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"{name}-{stamp}.zip"
    n = 2
    while dest.exists():          # two backups in one second must not clobber
        dest = BACKUP_DIR / f"{name}-{stamp}-{n}.zip"
        n += 1
    skip = {"hardware-qemu.ini", "cache.img", "cache.img.qcow2"}
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as z:
            z.write(ini, arcname=f"{name}.ini")
            for f in root.rglob("*"):
                if f.is_file() and f.name not in skip and "snapshots" not in f.parts:
                    # Zips are forward-slash by spec: os.sep arcnames extract as
                    # literal filenames with '\' in them on other machines (and
                    # as broken paths on this one) — .as_posix() keeps the
                    # archive portable, which is the whole point of a backup.
                    z.write(f, arcname=f"{name}.avd/{f.relative_to(root).as_posix()}")
    except (OSError, zipfile.BadZipFile) as e:
        try:
            dest.unlink()
        except OSError:
            pass
        return {"ok": False, "err": f"backup failed: {e}"}
    return {"ok": True, "avd": name, "file": str(dest),
            "bytes": dest.stat().st_size,
            "note": "Full instance backup (userdata kept). Restore it from the AVD Tools panel."}


#: Bounds for untrusted archives. An AVD userdata image is legitimately big;
#: a decompression bomb is not 16 GiB of zeroes inside a 40 KB zip.
MAX_RESTORE_BYTES = 16 * 1024 ** 3
MAX_RESTORE_ENTRIES = 2000
MAX_RESTORE_RATIO = 1000


def _backup_zip(raw: str) -> tuple[Path | None, str]:
    """Confine a restore source to a known backup directory."""
    if not raw or not isinstance(raw, str) or safe.has_control(raw):
        return None, "no backup zip given"
    try:
        p = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None, "invalid backup zip path"
    label = p.name[:80]
    if p.suffix.lower() != ".zip":
        return None, "backup must be a .zip file"
    try:
        if not p.is_file():
            return None, f"backup zip not found: {label}"
    except OSError:
        return None, f"backup zip not found: {label}"
    roots = [Path(BACKUP_DIR).resolve(),
             (Path.home() / "NovaDroid-Shared" / "Backups").resolve()]
    if not any(safe._under(p, r) for r in roots):
        return None, ("backup zip must be inside NovaDroid-Shared/Backups "
                      "(copy it there first)")
    return p, ""


def restore(zip_path: str, new_name: str = "") -> dict:
    """Recreate an AVD from a backup zip — hardened against Zip-Slip.

    v4 trusted `z.namelist()` and did `dst_dir / n[len(prefix):]` with no `..`
    check, no absolute-path check, no resolved-containment assertion, no size
    cap and no symlink check. A crafted backup could therefore write files
    ANYWHERE the agent's user account can write. Every entry is now validated
    up front, resolved under `dst_dir`, and the whole partial tree is removed
    if anything fails.
    """
    import zipfile
    src, err = _backup_zip(zip_path)
    if err:
        return {"ok": False, "err": err}
    dst_dir = dst_ini = None
    started = False
    try:
        with zipfile.ZipFile(src) as z:
            infos = z.infolist()
            if len(infos) > MAX_RESTORE_ENTRIES:
                return {"ok": False, "err": "backup has too many entries"}
            names = [i.filename for i in infos]
            ini_name = next(
                (n for n in names if n.endswith(".ini") and "/" not in n
                 and "\\" not in n and not n.startswith("/")), "")
            if not ini_name:
                return {"ok": False,
                        "err": "not a NovaDroid AVD backup (no .ini at zip root)"}
            base = ini_name[:-4]
            if not safe.valid_avd_name(base):
                return {"ok": False, "err": "invalid AVD name in backup"}
            name = re.sub(r"[^\w.\-]", "_", str(new_name or base).strip())
            if not safe.valid_avd_name(name):
                return {"ok": False, "err": "invalid AVD name"}
            dst_dir = avd_dir(name)
            dst_ini = avd_home() / f"{name}.ini"
            if dst_dir.exists() or dst_ini.exists():
                return {"ok": False, "err": f"target already exists: {name} "
                                            "(delete it or give a new name)"}

            # Entries are "<base>.avd/…"; stream them out under the NEW name so
            # restoring "orig" as "copy" doesn't leave files in orig.avd/.
            prefix = base + ".avd/"
            plan: list[tuple] = []
            total = 0
            for info in infos:
                n = info.filename
                if n == ini_name or n.endswith("/") or not n.startswith(prefix):
                    continue
                rel = n[len(prefix):].replace("\\", "/")
                parts = rel.split("/")
                # Reject traversal, absolute paths, drive letters, symlinks,
                # implausible expansion ratios and total size overruns BEFORE
                # creating a single file.
                if (not rel or rel.startswith("/") or ".." in parts
                        or ":" in parts[0] or "" in parts
                        or ((info.external_attr >> 16) & 0o170000) == 0o120000):
                    return {"ok": False, "err": "unsafe path in backup archive"}
                if info.compress_size and info.file_size > 1_000_000 and \
                        info.file_size / info.compress_size > MAX_RESTORE_RATIO:
                    return {"ok": False, "err": "suspicious compression ratio in backup"}
                total += info.file_size
                if total > MAX_RESTORE_BYTES:
                    return {"ok": False, "err": "backup expands beyond the size cap"}
                target = safe.confine(dst_dir, rel)
                if target is None:
                    return {"ok": False, "err": "unsafe path in backup archive"}
                plan.append((info, target))

            started = True
            dst_dir.mkdir(parents=True, exist_ok=True)
            for info, target in plan:
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as fh_in, target.open("wb") as fh_out:
                    shutil.copyfileobj(fh_in, fh_out, 1 << 20)
            # The .ini points at the old machine's path; rewrite for this one.
            ini_kv = inifile.parse_text(z.read(ini_name).decode("utf-8", "replace"))
            ini_kv["path"] = str(dst_dir)
            ini_kv["path.rel"] = f"avd/{name}.avd"
            ini_kv["avd.ini.displayname"] = name
            dst_ini.write_text(
                "\n".join(f"{k}={v}" for k, v in ini_kv.items()) + "\n",
                encoding="utf-8")
            inifile.upsert(dst_dir / "config.ini", {"hw.avd.name": name})
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as e:
        if started and dst_dir:
            shutil.rmtree(dst_dir, ignore_errors=True)
            try:
                dst_ini.unlink()
            except OSError:
                pass
        # Never echo exception text — it can contain absolute host paths.
        return {"ok": False, "err": f"restore failed ({type(e).__name__})"}
    return {"ok": True, "avd": name, "from": src.name, "entries": len(plan),
            "note": "Restored — Quickboot it from the Virtual Devices panel."}


def rename_avd(paths: ToolPaths, src: str, dst: str) -> dict:
    """Rename an AVD on disk: move the dir + .ini and fix every self-reference."""
    src, dst = (src or "").strip(), (dst or "").strip()
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
    with _MAP_LOCK:
        if _SERIAL_MAP.get(src):
            return {"ok": False, "err": f"{src} is running — stop it first"}
    try:
        shutil.move(str(src_dir), str(dst_dir))
    except (OSError, shutil.Error) as e:
        return {"ok": False, "err": f"rename failed: {e}"}
    try:
        ini_kv = inifile.parse(src_ini)
        ini_kv["path"] = str(dst_dir)
        ini_kv["path.rel"] = f"avd/{dst}.avd"
        ini_kv["avd.ini.displayname"] = dst
        dst_ini.write_text(
            "\n".join(f"{k}={v}" for k, v in ini_kv.items()) + "\n", encoding="utf-8")
        src_ini.unlink()
        inifile.upsert(dst_dir / "config.ini", {"hw.avd.name": dst})
    except (OSError, shutil.Error) as e:
        # Roll the directory back so the AVD stays bootable under its old
        # name instead of being visible under neither (the old code left a
        # half-moved AVD behind when the .ini write failed).
        try:
            if dst_ini.exists():
                dst_ini.unlink()
            if not src_dir.exists():
                shutil.move(str(dst_dir), str(src_dir))
        except OSError:
            pass
        return {"ok": False, "err": f"rename failed: {e}"}
    with _JOBS_LOCK:
        _JOBS.pop(src, None)
    with _MAP_LOCK:
        _SERIAL_MAP.pop(src, None)
    return {"ok": True, "from": src, "name": dst}


def root_toggle(paths: ToolPaths, serial: str) -> dict:
    """Try `adb root` / `adb unroot` and report honestly.

    Only google_apis (non-Play) images accept adbd-as-root; Play images must
    refuse (that is Play Integrity, not a bug). The image type is detected
    from the sysdir so the answer explains itself.
    """
    if not serial or not paths.adb:
        return {"ok": False, "err": "no device selected"}
    status = run([paths.adb, "-s", serial, "shell",
                  "getprop ro.build.type; getprop ro.debuggable"], timeout=20)
    lines = [l.strip() for l in status["out"].splitlines()]
    debuggable = "1" in lines
    cur = "root" if debuggable else "user"
    want = "unroot" if debuggable else "root"
    r = run([paths.adb, "-s", serial, want], timeout=30)
    out = (r["out"] + r["err"]).strip()
    ok = bool(r["ok"]) and "cannot run as root" not in out
    return {"ok": ok, "was": cur, "requested": want, "log": out[-300:],
            "note": ("Play Store images refuse adbd root by design — for root "
                     "testing create a `google_apis` (non-Play) AVD instead."
                     if not ok else "")}
