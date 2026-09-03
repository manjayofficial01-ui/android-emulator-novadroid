"""Central configuration + path discovery for NovaDroid.

Resolution order for SDK: ANDROID_SDK_ROOT > ANDROID_HOME >
%LOCALAPPDATA%\\Android\\Sdk > ~/Android/Sdk. All tool paths derived from it.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def find_sdk() -> Path | None:
    for env in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        v = os.environ.get(env)
        if v and Path(v).exists():
            return Path(v)
    local = os.environ.get("LOCALAPPDATA", "")
    cand = Path(local) / "Android" / "Sdk" if local else None
    if cand and cand.exists():
        return cand
    home = Path.home() / "Android" / "Sdk"
    if home.exists():
        return home
    return None


@dataclass(frozen=True)
class ToolPaths:
    sdk: Path | None
    android_cli: str | None
    adb: str | None
    emulator: str | None
    emulator_check: str | None
    avdmanager: str | None

    @classmethod
    def discover(cls) -> "ToolPaths":
        sdk = find_sdk()
        cli = shutil.which("android")
        adb = None
        emu = None
        check = None
        avdman = None
        if sdk:
            pt = sdk / "platform-tools" / "adb.exe"
            if pt.exists():
                adb = str(pt)
            em = sdk / "emulator" / "emulator.exe"
            if em.exists():
                emu = str(em)
            ck = sdk / "emulator" / "emulator-check.exe"
            if ck.exists():
                check = str(ck)
            am = sdk / "cmdline-tools" / "latest" / "bin" / "avdmanager.bat"
            if am.exists():
                avdman = str(am)
        return cls(sdk=sdk, android_cli=cli, adb=adb or shutil.which("adb"),
                   emulator=emu, emulator_check=check, avdmanager=avdman)


# Performance defaults distilled from research:
# - 4 vCPU is the sweet spot (boot doesn't scale past 4, Linaro/QEMU data).
# - API 37+ phone AVDs force >= 4GB RAM (emulator 36.6.11 release notes).
# - x86_64 + WHPX is 8-12x faster than TCG software emulation.
DEFAULTS = {
    "cpus": 4,
    "ram_mb": 4096,
    "heap_mb": 576,
    "disk_mb": 8192,
    "gpu_mode": "auto",   # auto -> host w/ fallback to swiftshader/lavapipe
    "accel": "whpx",
}
