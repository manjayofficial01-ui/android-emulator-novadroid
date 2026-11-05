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

from . import safe
from .config import ToolPaths
from .runner import run


def _version_key(name: str) -> tuple:
    """Sort build-tools dirs numerically, not lexicographically.

    String sorting put "9.0.0" after "36.0.0", so the newest aapt2 was
    skipped in favour of a decade-old one whenever both were installed.

    Pre-release suffixes sort BELOW their release: "36.0.0-rc1" < "36.0.0".
    The naive regex grabbed the `1` out of "rc1", which sorted the rc ABOVE
    the release — fixed by taking the dotted-number prefix only and using a
    trailing `pre` flag (0 for a pre-release, 1 for the real release).
    """
    base = name.split("-", 1)[0]                 # "36.0.0-rc1" -> "36.0.0"
    nums = [int(p) for p in re.findall(r"\d+", base)[:4]]
    nums = (nums + [0, 0, 0, 0])[:4]
    pre = 0 if "-" in name else 1                # release ranks above pre-release
    return (nums[0], nums[1], nums[2], nums[3], pre)


def find_aapt2(sdk: Path | None) -> str | None:
    if not sdk:
        return None
    cands = sorted((sdk / "build-tools").glob("*/aapt2.exe"),
                   key=lambda p: _version_key(p.parent.name))
    return str(cands[-1]) if cands else None


def apk_badging(paths: ToolPaths, apk_path: str) -> dict:
    """Inspect an APK — but only one the operator is allowed to read.

    v4 accepted ANY absolute host path, so /api/apk/analyze doubled as an
    arbitrary-file-existence probe and echoed the full path back. `safe.apk_path`
    confines it to the user folder / shared folder / configured roots and
    only ever reports the basename.
    """
    p, err = safe.apk_path(apk_path)
    if err:
        return {"ok": False, "err": err}
    aapt2 = find_aapt2(paths.sdk)
    if not aapt2:
        return {"ok": False, "err": "aapt2 not found (install SDK build-tools)"}
    r = run([aapt2, "dump", "badging", str(p)], timeout=60)
    if not r["ok"]:
        return {"ok": False, "err": "not a valid APK (aapt2 could not parse it)"}
    out = r["out"]
    try:
        size_mb = round(p.stat().st_size / 1e6, 1)
    except OSError:
        size_mb = 0
    info: dict = {"ok": True, "file": p.name, "bytes_mb": size_mb}
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


_DEVICE_CPU_PROPS = {
    "ro.product.cpu.abilist": "abilist",
    "ro.product.cpu.abi": "primary_abi",
    "ro.build.version.sdk": "sdk",
    "ro.build.version.release": "android",
    "ro.dalvik.vm.native.bridge": "bridge",
    "ro.product.model": "model",
}


def device_cpu(paths: ToolPaths, serial: str) -> dict:
    """Device CPU/API facts — one adb round-trip, not six.

    This runs on every "Analyze APK" and every device refresh. Six sequential
    `adb shell getprop` calls made the compatibility verdict visibly laggy on
    a cold adb server (~1s); `adb.getprops` collapses it to one.
    """
    cpu: dict = {"serial": serial}
    if not paths.adb or not serial:
        return cpu
    from . import adb as ADB
    raw = ADB.getprops(paths, serial, list(_DEVICE_CPU_PROPS))
    for prop, key in _DEVICE_CPU_PROPS.items():
        if raw.get(prop):
            cpu[key] = raw[prop]
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
    if serial and not safe.valid_serial(serial):
        return {"ok": False, "err": "invalid device serial"}
    b = apk_badging(paths, apk_path)
    if not b.get("ok"):
        return b
    dev = device_cpu(paths, serial) if serial else {"sdk": 0}
    v = verdict(b, dev)
    v.update(apk=b, device=dev if serial else {"note": "no device selected — assuming Play x86_64 image"})
    v["integrity_note"] = (
        "Play Integrity expectations: MEETS_BASIC_INTEGRITY usually passes on "
        "Play-signed emulator images; MEETS_DEVICE_INTEGRITY and "
        "MEETS_STRONG_INTEGRITY will fail on ANY emulator (NovaDroid, "
        "BlueStacks, Genymotion alike) — Google rotated the platform root "
        "cert in Feb 2026 and explicitly targets emulator farms. Banking/"
        "payment/anti-cheat failures are attestation, not CPU compatibility.")
    v["ok"] = True
    return v
