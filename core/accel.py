"""Acceleration / hypervisor probing (WHPX-first, AEHD-legacy).

Industry fact: AEHD sunsets Dec 31 2026; WHPX (Windows Hypervisor Platform)
is the mandatory path. `emulator-check accel` returns 0 when ready.
"""
from __future__ import annotations

from .config import ToolPaths
from .runner import run


def accel_status(paths: ToolPaths) -> dict:
    info: dict = {"accel": "unknown", "ready": False,
                  "detail": "", "whpx_usable": False, "sunset_note": ""}
    if paths.emulator_check:
        r = run([paths.emulator_check, "accel"], timeout=30)
        text = (r["out"] + "\n" + r["err"]).strip()
        info["detail"] = text
        # emulator-check prints numeric code; 0 == ANDROID_CPU_ACCELERATION_READY
        info["ready"] = r["ok"] or text.startswith("accel:\n0") or "\n0\n" in text
        info["whpx_usable"] = "WHPX" in text and "usable" in text
        info["accel"] = "whpx" if info["whpx_usable"] else ("ready" if info["ready"] else "missing")
    else:
        info["detail"] = "emulator-check.exe not found; install SDK emulator package."
    info["sunset_note"] = ("AEHD sunsets Dec 31 2026 — use WHPX. "
                           "Enable: Windows Features > Windows Hypervisor Platform + BIOS VT-x/AMD-V.")
    # Lightweight Hyper-V/WHPX feature probe (best effort, never fails hard)
    r = run(["powershell", "-NoProfile", "-Command",
             "Get-WindowsOptionalFeature -Online | "
             "Where-Object {$_.FeatureName -match "
             "'HypervisorPlatform|VirtualMachinePlatform|Microsoft-Hyper-V'} | "
             "Select-Object FeatureName,State | Format-Table -HideTableHeaders | "
             "Out-String"], timeout=45)
    if r["ok"] and r["out"].strip():
        info["windows_features"] = r["out"].strip()[:2000]
        # Turn the wall of text into something the UI can act on.
        enabled = [l.split()[0] for l in r["out"].splitlines()
                   if l.strip().endswith("Enabled") and l.split()]
        info["features_enabled"] = enabled
    else:
        info["windows_features"] = f"probe skipped: {r['err']}"[:400]
    return info
