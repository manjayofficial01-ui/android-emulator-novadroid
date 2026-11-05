"""Engine & compatibility report — `/api/engine`.

Turns the researched Android Emulator release notes into checks the operator
can act on, instead of a static "latest version" string in a README. Facts
are pinned to the official release notes and carry a verification date so a
stale build is reported as *unknown*, never as a false "up to date".

Verified from https://developer.android.com/studio/releases/emulator :
  * 37.2.13 Patch (Oct 28 2026) — host-memory leak fix in OpenGL ES emulation.
  * 37.2.12 Stable (Oct  1 2026) — gRPC streamScreenshot / MMAP fixes.
  * 37.1.11 Stable (Jul 30 2026) — AEHD sunset announced for Dec 31 2026;
    unit suffixes ("6G", "32M") accepted for RAM/heap config values.
  * 36.6.11 Stable (Jun  2 2026) — API 37 phone AVDs hard-floor RAM at 4 GB;
    "environment" camera mode backed by the AVD's environment.ini.
  * 36.5.10 Stable (Apr  2 2026) — zero-configuration multi-device networking.
  * 36.4.9  Stable (Feb 10 2026) — Lavapipe becomes the default software
    Vulkan renderer; -gpu software and -feature VulkanNativeSwapchain added.
  * 36.2.11 Stable (Oct  9 2025) — HAXM support removed.

Everything here is stdlib-only and needs no running emulator. The slow parts
(`emulator -version`, `emulator-check accel`, AVD scan) are cached by the
router for 60 seconds.
"""
from __future__ import annotations

import datetime
import re

from . import safe
from .config import ToolPaths
from .runner import run

LATEST_STABLE = "37.2.12"
LATEST_PATCH = "37.2.13"
#: Newest canary verified to exist (via the official fixed-bugs page for the
#: 37 line). Release dates for canaries are not published, so the report
#: never claims one is newer/older than the stable line — just that it exists.
LATEST_CANARY = "37.3.6"
NEXT_GEN_NOTE = ("Next Generation Android Emulator (Preview): default for "
                 "API 37+ AVDs from Android Studio Rabbit 3 Canary 1 — "
                 "upstream QEMU v11 replaces the QEMU 2 fork. Raw flags and "
                 "snapshots may behave differently; test raw-flag AVDs on the "
                 "classic engine before pinning a fleet on the preview.")
VERIFIED_ON = "2026-11-06"
SOURCE_URL = "https://developer.android.com/studio/releases/emulator"
AEHD_SUNSET = datetime.date(2026, 12, 31)
API37_RAM_FLOOR_MB = 4096
MAX_CORES = 4

#: The one command that actually clears an `outdated` verdict. surfaced in the
#: Engine card so a warning is never a dead end. Run from
#: $ANDROID_HOME/cmdline-tools/latest/bin (or anywhere with sdkmanager on PATH),
#: then restart the NovaDroid agent — `emulator -version` is re-read on demand.
UPDATE_COMMAND = 'sdkmanager "emulator"'

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:\.\d+)?)")
_API_RE = re.compile(r"android-(\d+)")


def version_tuple(text: str) -> tuple[int, ...]:
    """`37.2.12` -> (37, 2, 12); unparseable -> ()."""
    m = _VERSION_RE.search(str(text or ""))
    if not m:
        return ()
    try:
        return tuple(int(p) for p in m.group(1).split("."))
    except ValueError:
        return ()


def channel_for(installed: str, stable: str = LATEST_STABLE,
                patch: str = LATEST_PATCH) -> str:
    """current | outdated | unknown. Pure function.

    `unknown` is deliberate: an SDK we cannot query must never be reported
    as up to date just because the comparison code got a blank string.
    `patch` is accepted for a complete release-line call site; classification
    is against the Stable floor, so anything newer (including a canary) is
    "current".
    """
    got, floor = version_tuple(installed), version_tuple(stable)
    if not got:
        return "unknown"
    if not floor:
        return "current"
    return "current" if got >= floor else "outdated"


def is_canary(installed: str) -> bool:
    """True when the installed build is from a newer (canary) line than the
    pinned stable, e.g. 37.3.x while stable is 37.2.x. Pure function."""
    got, floor = version_tuple(installed), version_tuple(LATEST_STABLE)
    if not got or not floor:
        return False
    return got[:2] > floor[:2]


def emulator_version(paths: ToolPaths) -> str:
    """Installed emulator version, or "" when the binary cannot be queried."""
    if not paths.emulator:
        return ""
    r = run([paths.emulator, "-version"], timeout=25)
    blob = (r.get("out") or "") + "\n" + (r.get("err") or "")
    m = _VERSION_RE.search(blob)
    return m.group(1) if m else ""


def avd_rows(avds: list[dict]) -> list[dict]:
    """Per-AVD readiness. Pure — takes `AVD.list_avds()["avds"]` shape.

    A missing `hw.ramSize` / `hw.cpu.ncore` is UNKNOWN, not a violation:
    a fresh AVD without those keys used to be listed as "below the RAM
    floor" / "check cores" even though the emulator default is healthy.
    """
    rows: list[dict] = []
    for a in avds or []:
        cfg = a.get("config") or {}
        m = _API_RE.search(str(cfg.get("image.sysdir.1", "")))
        api = int(m.group(1)) if m else 0
        ram = safe.memory_mb(cfg.get("hw.ramSize", "0"))
        try:
            cores = int(str(cfg.get("hw.cpu.ncore", "0")).strip() or 0)
        except ValueError:
            cores = 0
        gpu = str(cfg.get("hw.gpu.mode", "") or "")
        # Vulkan + Quickboot snapshots are incompatible as of 36.4.9. We can
        # only see the config, so flag the combination, not a live certainty.
        vulkan = "vulkan" in gpu.lower() or "vulkan" in str(
            cfg.get("hw.gpu.enabled", "")).lower()
        rows.append({
            "name": a.get("name", "?"),
            "api": api or None,
            "ram_mb": ram or None,
            "ram_ok": (ram >= (API37_RAM_FLOOR_MB if api >= 37 else 1536))
            if ram else True,
            "cores": cores or None,
            "cores_ok": (2 <= cores <= MAX_CORES) if cores else True,
            "gpu_mode": gpu or "?",
            "vulkan_snapshot_risk": bool(vulkan),
        })
    return rows


def _checks(version: str, acc: dict, rows: list[dict]) -> list[dict]:
    """Ordered, actionable compatibility checks."""
    out: list[dict] = []

    def add(cid, label, ok, detail=""):
        out.append({"id": cid, "label": label, "ok": bool(ok),
                    "detail": str(detail)[:180]})

    add("emulator", "Emulator binary found", bool(version),
        f"installed {version}" if version else "emulator.exe not discovered")
    add("whpx", "WHPX acceleration ready", acc.get("ready"),
        "WHPX is mandatory — AEHD support ends Dec 31 2026"
        if acc.get("ready") else (acc.get("detail") or "emulator-check accel failed")[:180])
    api37 = [r for r in rows if (r.get("api") or 0) >= 37]
    bad_ram = [r["name"] for r in api37 if not r["ram_ok"]]
    add("api37_ram", "API 37 AVDs meet the 4 GB RAM floor",
        not bad_ram,
        f"below floor: {', '.join(bad_ram)}" if bad_ram
        else f"{len(api37)} API 37+ AVD(s) checked")
    slow = [r["name"] for r in rows if r["ram_mb"] and r["ram_mb"] > 8192]
    add("ram_ceiling", "No AVD over 8 GB RAM (host headroom)", not slow,
        f"oversized: {', '.join(slow)}" if slow else "all within the useful range")
    bad_cores = [r["name"] for r in rows if not r["cores_ok"]]
    add("cores", "AVDs respect the 4-vCPU useful ceiling", not bad_cores,
        f"check: {', '.join(bad_cores)}" if bad_cores else "2–4 vCPU on every AVD")
    suffix_ok = (safe.memory_mb("6G") == 6144
                 and safe.memory_mb("32M") == 32
                 and safe.memory_mb("4096") == 4096)
    add("unit_suffix", "Config parser understands 6G / 32M suffixes",
        suffix_ok, "37.1.11+ accepts unit suffixes for RAM and heap values")
    from . import perf as PERF
    add("lavapipe", "Software-render detection includes Lavapipe",
        PERF.classify_renderer("lavapipe (LLVM 15.0.7)") == "lavapipe",
        "36.4.9+ ships Lavapipe as the default software Vulkan renderer")
    risky = [r["name"] for r in rows if r["vulkan_snapshot_risk"]]
    add("vulkan_snapshot", "Vulkan is not paired with Quickboot snapshots",
        not risky,
        f"launch these with -no-snapshot: {', '.join(risky)}" if risky
        else "no Vulkan/snapshot conflict detected in config.ini")
    return out


def _notes(acc: dict, channel: str, version: str = "",
           canary: bool = False) -> list[dict]:
    notes: list[dict] = [
        {"level": "info", "title": "Acceleration",
         "detail": "Always use x86_64 images with WHPX — 8–12× faster than "
                   "TCG. AEHD sunsets Dec 31 2026; HAXM was removed in 36.2.11."},
        {"level": "info", "title": "Snapshots",
         "detail": "Quickboot restores in 6–15 s vs 50 s+ cold. Vulkan and "
                   "snapshots do not mix (36.4.9+): use -no-snapshot with Vulkan."},
        {"level": "info", "title": "On-device AI",
         "detail": "AICore is not shipped in emulator system images, so "
                   "Gemini Nano / ML Kit GenAI cannot run here. This is an "
                   "image limitation, not a configuration problem."},
        {"level": "info", "title": "Next-generation engine (preview)",
         "detail": NEXT_GEN_NOTE},
    ]
    if canary:
        notes.insert(0, {
            "level": "info", "title": f"Canary line detected ({LATEST_CANARY})",
            "detail": (f"Installed {version or 'build'} is newer than the "
                       f"{LATEST_STABLE} stable line — canary fixes land in "
                       "stable weeks later. Report behaviour bugs upstream, "
                       "not to NovaDroid.")})
    if channel == "outdated":
        notes.insert(0, {
            "level": "warn", "title": "Emulator update available",
            "detail": (f"Installed {version or 'unknown'} predates "
                       f"{LATEST_STABLE} Stable / {LATEST_PATCH} Patch. Run "
                       f"`{UPDATE_COMMAND}` — or `android sdk update emulator` "
                       "with the newer CLI — then restart the NovaDroid agent."),
            "command": UPDATE_COMMAND,
        })
    elif channel == "unknown":
        notes.insert(0, {"level": "warn", "title": "Version unknown",
                         "detail": "Could not query emulator -version; "
                                   "check the SDK path in /api/info."})
    if not acc.get("ready"):
        notes.insert(0, {"level": "warn", "title": "Acceleration not ready",
                         "detail": "Enable Windows Features > Windows "
                                   "Hypervisor Platform and BIOS VT-x/AMD-V."})
    return notes


def report(paths: ToolPaths) -> dict:
    """Full `/api/engine` payload. Never raises for a missing/broken SDK."""
    version = emulator_version(paths)
    channel = channel_for(version)
    from . import accel as ACCEL
    from . import avd as AVD
    from . import perf as PERF
    try:
        acc = ACCEL.accel_status(paths)
    except Exception as ex:
        print(f"[engine] accel probe failed ({type(ex).__name__}): {ex}")
        acc = {"accel": "unknown", "ready": False, "detail": "probe failed",
               "features_enabled": []}
    try:
        avds = AVD.list_avds(paths).get("avds") or []
    except Exception as ex:
        print(f"[engine] AVD scan failed ({type(ex).__name__}): {ex}")
        avds = []
    rows = avd_rows(avds)
    days = (AEHD_SUNSET - datetime.date.today()).days
    try:
        suggested = PERF.suggest_flags(paths)
        flags, flags_note = suggested.get("flags", []), suggested.get("note", "")
    except Exception as ex:
        print(f"[engine] flag suggestion failed ({type(ex).__name__}): {ex}")
        flags, flags_note = [], "flag suggestion unavailable"
    try:
        hybrid = PERF.hybrid_note(paths)
    except Exception as ex:
        print(f"[engine] hybrid probe failed ({type(ex).__name__}): {ex}")
        hybrid = None
    notes = _notes(acc, channel, version, is_canary(version))
    if hybrid:
        notes.insert(1, hybrid)   # after any blocking warnings, before trivia
    engine = {
        "emulator_version": version or "unknown",
        "latest_stable": LATEST_STABLE,
        "latest_patch": LATEST_PATCH,
        "latest_canary": LATEST_CANARY,
        "canary": is_canary(version),
        "channel": channel,
        "verified_on": VERIFIED_ON,
        "source": SOURCE_URL,
        "accel": {"accel": acc.get("accel", "unknown"),
                  "ready": bool(acc.get("ready")),
                  "detail": str(acc.get("detail", ""))[:400],
                  "features_enabled": acc.get("features_enabled") or []},
        "aehd": {"deprecated": True, "sunset": "Dec 31 2026",
                 "days_left": max(0, days), "ended": days < 0},
        "update": {
            "available": channel == "outdated",
            "installed": version or "unknown",
            "target": LATEST_PATCH,
            "command": UPDATE_COMMAND,
            "alt_command": "android sdk update emulator",
            "note": ("Run from $ANDROID_HOME/cmdline-tools/latest/bin, then "
                     "restart the NovaDroid agent to refresh this card."),
        },
        "checks": _checks(version, acc, rows),
        "notes": notes,
        "hybrid": PERF.detect_hybrid(),
    }
    return {"ok": True, "engine": engine, "avds": rows, "flags": flags,
            "flags_note": flags_note}
