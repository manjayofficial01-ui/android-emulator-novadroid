"""Per-instance GPU preference (MuMu V6.4.7 'manual GPU selection' parity).

Windows decides per-executable which GPU renders D3D/Vulkan: the mapping
lives in ``HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences`` as one
REG_SZ value per executable path with data like ``GpuPreference=2;``
(0 = let Windows decide, 1 = power saving / iGPU, 2 = high performance /
dGPU).

For the Google emulator the pixels are actually drawn by the child
``qemu-system-x86_64.exe``, so NovaDroid sets the preference for BOTH the
launcher (emulator.exe) and the worker (qemu-system-x86_64.exe) found in the
same SDK directory. Applies to the NEXT launch — a running instance keeps
its current GPU; the response says so.

Pure stdlib (winreg), Windows-only — every other platform gets an honest
"not applicable" instead of a fake success.
"""
from __future__ import annotations

import sys

from .config import ToolPaths

GPUPREF_KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"

#: Windows' documented values; "auto" deletes the override entirely.
MODES = {"auto": 0, "power": 1, "performance": 2}
MODE_NAMES = {0: "let Windows decide", 1: "power saving (iGPU)",
              2: "high performance (dGPU)"}


def _executables(paths: ToolPaths) -> list[str]:
    """The two binaries that matter, lowercase full paths (registry is
    case-insensitive; keeping one canonical spelling avoids duplicates)."""
    out: list[str] = []
    for p in (paths.emulator, None):
        if p:
            out.append(str(p))
    # qemu lives next to emulator.exe in the SDK emulator/ directory.
    if paths.emulator:
        from pathlib import Path
        q = Path(paths.emulator).with_name("qemu-system-x86_64.exe")
        if q.exists():
            out.append(str(q))
    return [s.lower() for s in out]


def get(paths: ToolPaths) -> dict:
    """Current GPU preference for the emulator binaries."""
    if sys.platform != "win32":
        return {"ok": False,
                "err": "GPU preference is a Windows-only setting on this host"}
    if not paths.emulator:
        return {"ok": False, "err": "emulator.exe not found in SDK"}
    import winreg
    out: dict = {"ok": True, "entries": [], "modes": MODE_NAMES}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, GPUPREF_KEY) as k:
            for exe in _executables(paths):
                try:
                    raw, _ = winreg.QueryValueEx(k, exe)
                except FileNotFoundError:
                    out["entries"].append({"exe": exe, "mode": "auto"})
                    continue
                code = -1
                for tok in str(raw).split(";"):
                    if tok.strip().startswith("GpuPreference="):
                        try:
                            code = int(tok.split("=", 1)[1])
                        except ValueError:
                            pass
                out["entries"].append(
                    {"exe": exe, "mode": MODE_NAMES.get(code, f"unknown ({code})"),
                     "raw": str(raw)})
    except OSError as e:
        return {"ok": False, "err": f"could not read GPU preferences: {e}"}
    return out


def set_pref(paths: ToolPaths, mode: str) -> dict:
    """Pin the emulator to a GPU class. Takes effect on the next launch."""
    if sys.platform != "win32":
        return {"ok": False,
                "err": "GPU preference is a Windows-only setting on this host"}
    if not paths.emulator:
        return {"ok": False, "err": "emulator.exe not found in SDK"}
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        return {"ok": False,
                "err": f"mode must be one of {sorted(MODES)} (auto/power/performance)"}
    import winreg
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, GPUPREF_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            for exe in _executables(paths):
                if MODES[mode] == 0:
                    try:               # auto = remove the override
                        winreg.DeleteValue(k, exe)
                    except FileNotFoundError:
                        pass
                else:
                    winreg.SetValueEx(k, exe, 0, winreg.REG_SZ,
                                      f"GpuPreference={MODES[mode]};")
    except OSError as e:
        return {"ok": False, "err": f"could not write GPU preference: {e}"}
    return {"ok": True, "mode": mode, "applies": "next launch",
            "note": (f"Instances will prefer {MODE_NAMES[MODES[mode]]}. "
                     "Running emulators keep their current GPU until "
                     "restarted. Verify with the render-path probe "
                     "(/api/smoothness/renderer) after the next boot.")}
