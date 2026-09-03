"""APK compatibility: will this phone APK run on our x86_64 emulator?

Pipeline (all local, no device needed for analysis):
  aapt2 dump badging app.apk  -> package, version, min/target SDK, ABIs,
                                 locales, launchable activity
  + device props (when a device is selected): cpu abilist, sdk, native bridge
  = verdict: NATIVE / ARM-TRANSLATED / PURE-JAVA / BLOCKED + reason.

Background: our images are x86_64 google_apis_playstore builds. On Android 11+
Google ships an ARM native bridge (libndk_translation / Houdini-class) in
these images, so ARMv7/ARM64 phone APKs execute via translation — slower than
native but working. Pure-Java APKs always work. Hard blocks are rare: minSdk
above the device API, or apps demanding hardware Play Integrity verdicts
(banking) which detect virtualization on ANY emulator, not just ours.
Installs via NovaDroid use the package manager directly, so "unknown sources"
prompts never appear.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import ToolPaths
from .runner import run


def find_aapt2(sdk: Path | None) -> str | None:
    if not sdk:
        return None
    cands = sorted((sdk / "build-tools").glob("*/aapt2.exe"),
                   key=lambda p: p.parent.name)
    return str(cands[-1]) if cands else None


def apk_badging(paths: ToolPaths, apk_path: str) -> dict:
    p = Path(apk_path)
    if not p.exists():
        return {"ok": False, "err": f"APK not found: {apk_path}"}
    aapt2 = find_aapt2(paths.sdk)
    if not aapt2:
        return {"ok": False, "err": "aapt2 not found (install SDK build-tools)"}
    r = run([aapt2, "dump", "badging", str(p)], timeout=60)
    if not r["ok"]:
        return {"ok": False, "err": f"not a valid APK: {r['err'][-300:]}"}
    out = r["out"]
    info: dict = {"ok": True, "file": p.name,
                  "bytes_mb": round(p.stat().st_size / 1e6, 1)}
    m = re.search(r"package: name='([^']+)' versionCode='([^']*)' versionName='([^']*)'", out)
    if m:
        info.update(package=m.group(1), versionCode=m.group(2), versionName=m.group(3))
    m = re.search(r"sdkVersion:'(\d+)'", out)
    info["minSdk"] = int(m.group(1)) if m else 1
    m = re.search(r"targetSdkVersion:'(\d+)'", out)
    info["targetSdk"] = int(m.group(1)) if m else info["minSdk"]
    info["abis"] = re.findall(r"native-code: '([^']+)'", out)
    m = re.search(r"application-label:'([^']+)'", out)
    if m:
        info["label"] = m.group(1)
    m = re.search(r"launchable-activity: name='([^']+)'", out)
    if m:
        info["launcher"] = m.group(1)
    return info


def device_cpu(paths: ToolPaths, serial: str) -> dict:
    cpu: dict = {"serial": serial}
    if not paths.adb or not serial:
        return cpu
    props = {"ro.product.cpu.abilist": "abilist",
             "ro.product.cpu.abi": "primary_abi",
             "ro.build.version.sdk": "sdk",
             "ro.build.version.release": "android",
             "ro.dalvik.vm.native.bridge": "bridge",
             "ro.product.model": "model"}
    for prop, key in props.items():
        r = run([paths.adb, "-s", serial, "shell", "getprop", prop], timeout=10)
        if r["ok"] and r["out"]:
            cpu[key] = r["out"].strip()
    try:
        cpu["sdk"] = int(cpu.get("sdk", 0))
    except ValueError:
        cpu["sdk"] = 0
    return cpu


def verdict(badging: dict, dev: dict) -> dict:
    """Pure function — unit-testable without any device."""
    sdk = int(dev.get("sdk", 0) or 0)
    if sdk and badging.get("minSdk", 1) > sdk:
        return {"level": "BLOCKED",
                "reason": (f"Needs Android API {badging['minSdk']}+, device is API {sdk}. "
                           "Use a newer AVD image."),
                "install_anyway": False}
    abis = set(badging.get("abis", []))
    if not abis:
        return {"level": "OK",
                "reason": "Pure Java/Kotlin app (no native code) — runs on any CPU.",
                "install_anyway": True}
    abilist = set((dev.get("abilist") or "").replace(",", " ").split())
    matched = abis & abilist
    primary = dev.get("primary_abi", "")
    bridge = dev.get("bridge", "")
    # The Play x86_64 image advertises ARM ABIs *through* its translation
    # bridge — a match there means translated execution, not native.
    if matched and primary.startswith("x86") and matched <= {"armeabi-v7a", "arm64-v8a"}:
        how = f"via ARM translation ({bridge})" if bridge else "via ARM translation"
        return {"level": "OK",
                "reason": (f"ARM-only native code {sorted(matched)} runs {how} — "
                           "most games/apps work, ~10-30% slower than native."),
                "install_anyway": True}
    if matched or (sdk == 0 and abis & {"x86_64", "x86"}):
        return {"level": "OK",
                "reason": f"Native {sorted(matched) or sorted(abis)} runs directly on this CPU.",
                "install_anyway": True}
    arm_only = abis <= {"armeabi-v7a", "arm64-v8a"}
    if arm_only and (bridge or sdk == 0):
        how = f"via ARM translation ({bridge})" if bridge else "via ARM translation (expected on Play x86_64 images)"
        return {"level": "OK",
                "reason": f"ARM-only native code {sorted(abis)} runs {how} — most games/apps work, ~10-30% slower.",
                "install_anyway": True}
    if arm_only:
        return {"level": "RISKY",
                "reason": ("ARM-only native code but no translation bridge detected on device. "
                           "Try installing — Play x86_64 images usually still cope."),
                "install_anyway": True}
    return {"level": "RISKY",
            "reason": f"Unusual ABIs {sorted(abis)} — try installing, check logcat on failure.",
            "install_anyway": True}


def check(paths: ToolPaths, apk_path: str, serial: str = "") -> dict:
    b = apk_badging(paths, apk_path)
    if not b.get("ok"):
        return b
    dev = device_cpu(paths, serial) if serial else {"sdk": 0}
    v = verdict(b, dev)
    v.update(apk=b, device=dev if serial else {"note": "no device selected — assuming Play x86_64 image"})
    v["integrity_note"] = ("Banking/payment apps with hardware Play Integrity checks "
                           "can refuse ANY emulator (BlueStacks/LDPlayer included) — "
                           "that is attestation, not CPU compatibility.")
    v["ok"] = True
    return v
