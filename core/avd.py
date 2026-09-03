"""AVD lifecycle via `android emulator` CLI + direct emulator binary fallback.

Cutting-edge defaults applied on top of stock AVDs:
 - Quickboot snapshots (fast boot 6-13s like BlueStacks, vs 50s cold)
 - gfxstream GPU auto (host -> ANGLE/SwiftShader/Lavapipe fallback)
 - Zero-config multi-instance networking (emulator 36.10+: Wi-Fi Direct/NSD
   work between AVDs with no manual port forwarding)
 - 4 vCPU / 4GB RAM baseline (API 37 floor + QEMU scaling research)
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from .config import ToolPaths
from .runner import run, run_in_background

# Tracks background `emulator start` jobs so UI can poll state.
_JOBS: dict[str, dict] = {}


def list_avds(paths: ToolPaths) -> dict:
    if not paths.android_cli:
        return {"ok": False, "avds": [], "err": "`android` CLI not on PATH"}
    r = run([paths.android_cli, "emulator", "list"], timeout=60)
    avds: list[dict] = []
    # CLI prints one AVD name per line (sometimes with extra columns)
    for line in (r["out"] + "\n" + r["err"]).splitlines():
        name = line.strip().split()[0] if line.strip() else ""
        if name and not name.startswith(("-", "Available", "Usage", "No ")):
            if re.match(r"^[\w.\-]+$", name):
                avds.append({"name": name})
    # Enrich with config.ini details when available
    for a in avds:
        cfg = _avd_config(a["name"])
        a.update(cfg)
    return {"ok": True, "avds": avds, "raw": r["out"][:2000]}


def _avd_config(name: str) -> dict:
    base = Path.home() / ".android" / "avd" / f"{name}.avd" / "config.ini"
    info: dict = {}
    try:
        if base.exists():
            for line in base.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k in ("hw.cpu.ncore", "hw.ramSize", "hw.gpu.mode",
                             "image.sysdir.1", "hw.device.name", "PlayStore.enabled"):
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


def create_avd(paths: ToolPaths, profile: str = "medium_phone") -> dict:
    if not paths.android_cli:
        return {"ok": False, "err": "`android` CLI not on PATH"}
    # android CLI auto-names the AVD; capture list before/after to find it.
    before = {a["name"] for a in list_avds(paths).get("avds", [])}
    r = run([paths.android_cli, "emulator", "create", profile], timeout=300)
    after = list_avds(paths).get("avds", [])
    new = [a for a in after if a["name"] not in before]
    name = new[0]["name"] if new else None
    ok = r["ok"] or name is not None
    if ok and name:
        _tune_avd_config(name)
    return {"ok": ok, "name": name, "avds": after,
            "out": (r["out"] + "\n" + r["err"]).strip()[-2000:]}


def _tune_avd_config(name: str) -> None:
    """Apply NovaDroid performance baseline to a fresh AVD config.ini."""
    cfg = Path.home() / ".android" / "avd" / f"{name}.avd" / "config.ini"
    try:
        if not cfg.exists():
            return
        text = cfg.read_text(encoding="utf-8", errors="ignore")
        set_kv = {"hw.cpu.ncore": "4", "hw.ramSize": "4096",
                  "vm.heapSize": "576", "hw.gpu.enabled": "yes",
                  "hw.gpu.mode": "auto", "hw.keyboard": "yes",
                  "hw.mainKeys": "no", "disk.dataPartition.size": "8G",
                  "fastboot.forceColdBoot": "no"}
        lines = []
        seen = set()
        for line in text.splitlines():
            if "=" in line:
                k = line.split("=", 1)[0].strip()
                if k in set_kv:
                    lines.append(f"{k}={set_kv[k]}")
                    seen.add(k)
                    continue
            lines.append(line)
        for k, v in set_kv.items():
            if k not in seen:
                lines.append(f"{k}={v}")
        cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def start_avd(paths: ToolPaths, name: str, cold: bool = False) -> dict:
    if not paths.android_cli:
        return {"ok": False, "err": "`android` CLI not on PATH"}
    cmd = [paths.android_cli, "emulator", "start", name] + (["--cold"] if cold else [])
    _JOBS[name] = {"state": "starting", "cold": cold, "t0": time.time()}
    def _done(res: dict):
        _JOBS[name] = {"state": "ready" if res["ok"] else "error",
                       "t0": _JOBS[name]["t0"], "log": (res["out"] + res["err"])[-2000:]}
    run_in_background(cmd, _done)
    mode = "cold boot (no snapshot)" if cold else "quickboot snapshot"
    return {"ok": True, "job": _JOBS[name], "mode": mode,
            "note": "First boot takes minutes (Play image provisioning); later quickboots take seconds."}


def start_avd_raw(paths: ToolPaths, name: str, extra: list[str] | None = None) -> dict:
    """Power-user path: launch emulator.exe directly for flags the CLI hides.

    Exposes cutting-edge knobs: -gpu host/swiftshader_indirect/lavapipe,
    -feature VulkanNativeSwapchain, -no-snapshot, -memory, -cores.
    """
    if not paths.emulator:
        return {"ok": False, "err": "emulator.exe not found in SDK"}
    cmd = [paths.emulator, f"@{name}"] + (extra or [])
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return {"ok": True, "cmd": " ".join(cmd)}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def stop_avd(paths: ToolPaths, name: str) -> dict:
    if not paths.android_cli:
        return {"ok": False, "err": "`android` CLI not on PATH"}
    r = run([paths.android_cli, "emulator", "stop", name], timeout=120)
    _JOBS.pop(name, None)
    # Fallback: adb emu kill if CLI stop missed
    if not r["ok"] and paths.adb:
        serial = _serial_for_avd(paths, name)
        if serial:
            run([paths.adb, "-s", serial, "emu", "kill"], timeout=30)
    return {"ok": True, "out": (r["out"] + r["err"]).strip()[-1000:]}


def remove_avd(paths: ToolPaths, name: str) -> dict:
    if not paths.android_cli:
        return {"ok": False, "err": "`android` CLI not on PATH"}
    r = run([paths.android_cli, "emulator", "remove", name], timeout=120)
    _JOBS.pop(name, None)
    return {"ok": r["ok"], "out": (r["out"] + r["err"]).strip()[-1000:]}


def job_state(name: str) -> dict:
    return _JOBS.get(name, {"state": "unknown"})


def _serial_for_avd(paths: ToolPaths, name: str) -> str | None:
    """Best-effort: first online emulator serial (multi-instance: map via adb)."""
    if not paths.adb:
        return None
    r = run([paths.adb, "devices"], timeout=15)
    for line in r["out"].splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device" and parts[0].startswith("emulator-"):
            return parts[0]
    return None


def wipe_data(name: str) -> dict:
    """Delete derived state (hardware-qemu.ini, snapshots) for a clean boot."""
    avd_dir = Path.home() / ".android" / "avd" / f"{name}.avd"
    removed = []
    for f in ("hardware-qemu.ini", "snapshots.img", "userdata-qemu.img"):
        p = avd_dir / f
        try:
            if p.exists():
                (p.unlink() if p.is_file() else None)
                removed.append(f)
        except Exception:
            pass
    # snapshots/ dir holds quickboot chains
    snapdir = avd_dir / "snapshots"
    return {"ok": True, "removed": removed,
            "note": "Snapshots dir kept; use cold boot to ignore it. Delete snapshots/ manually for full reset."}
