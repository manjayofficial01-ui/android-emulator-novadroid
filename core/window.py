"""Emulator window placement fixes (Windows).

Root cause of "booted above the screen": the emulator persists its Qt window
position in <avd>.avd/emulator-user.ini (window.x / window.y) and restores it
on next boot. If it was closed while off-screen — or the display setup changed
(resolution, scaling, unplugged monitor) — it reopens there with the title bar
unreachable. Cold boot / wipe does NOT fix it (not a snapshot issue).

Two-layer fix, both stdlib-only:
 1. reset_saved_position(): rewrite window.x/y in emulator-user.ini while the
    AVD is STOPPED (the emulator overwrites this file on exit, so editing a
    running instance is pointless). Next boot lands centered with title bar
    at y=60.
 2. move_running(): SetWindowPos the live window via PowerShell. Matching is
    deliberately strict — process must be emulator/qemu-system-* AND title
    must case-sensitively match '^Android Emulator - ' — so IDEs or browsers
    with similar words in their titles are never touched.
"""
from __future__ import annotations

import ctypes
import subprocess
from pathlib import Path


def screen_size() -> tuple[int, int]:
    """Primary display size via user32 (stdlib ctypes, no deps)."""
    try:
        u = ctypes.windll.user32
        return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    except Exception:
        return 1536, 864  # sane fallback (matches this dev machine)


def center_for(win_w: int = 500) -> tuple[int, int]:
    sw, _sh = screen_size()
    return max(0, (sw - win_w) // 2), 60  # y=60 guarantees a grabbable title bar


def user_ini(name: str) -> Path:
    return Path.home() / ".android" / "avd" / f"{name}.avd" / "emulator-user.ini"


def window_state(name: str) -> dict:
    p = user_ini(name)
    info: dict = {"avd": name, "ini_exists": p.exists(), "x": None, "y": None}
    try:
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("window.x"):
                    info["x"] = line.split("=", 1)[1].strip()
                elif line.startswith("window.y"):
                    info["y"] = line.split("=", 1)[1].strip()
    except Exception as e:
        info["err"] = str(e)
    sw, sh = screen_size()
    info["screen"] = f"{sw}x{sh}"
    try:
        y = int(str(info["y"]).split(".")[0]) if info["y"] is not None else None
        info["offscreen"] = y is not None and (y < 0 or y > sh - 60)
    except (ValueError, TypeError):
        info["offscreen"] = False
    return info


def reset_saved_position(name: str, x: int | None = None, y: int | None = None) -> dict:
    """Rewrite saved window coords. AVD must be STOPPED for this to stick."""
    avd_dir = Path.home() / ".android" / "avd" / f"{name}.avd"
    if not avd_dir.exists():
        return {"ok": False, "err": f"AVD not found: {name}"}
    if x is None or y is None:
        cx, cy = center_for()
        x, y = cx if x is None else x, cy if y is None else y
    p = user_ini(name)
    kv = {"window.x": str(int(x)), "window.y": str(int(y))}
    lines, seen = [], set()
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line and line.split("=", 1)[0].strip() in kv:
                k = line.split("=", 1)[0].strip()
                lines.append(f"{k}={kv[k]}")
                seen.add(k)
            else:
                lines.append(line)
    for k, v in kv.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True, "avd": name, "x": int(x), "y": int(y),
            "note": "Takes effect on next boot. Reopen the AVD to verify."}


# Strict PowerShell: process allowlist + case-SENSITIVE title regex.
_MOVE_PS = r"""
Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f); [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r); public struct RECT { public int L; public int T; public int R; public int B; }' -Name WinMove -Namespace W32;
$found = Get-Process | Where-Object { ($_.ProcessName -in 'emulator','qemu-system-x86_64','qemu-system-aarch64') -and ($_.MainWindowTitle -cmatch '^Android Emulator - ') };
if (-not $found) { Write-Output 'NOEMU'; exit 0 }
foreach ($e in $found) {
  [W32.WinMove]::SetWindowPos($e.MainWindowHandle, [IntPtr]::Zero, __X__, __Y__, 0, 0, 5) | Out-Null;
  $r = New-Object W32.WinMove+RECT; [W32.WinMove]::GetWindowRect($e.MainWindowHandle, [ref]$r) | Out-Null;
  Write-Output ("MOVED id=" + $e.Id + " L=" + $r.L + " T=" + $r.T);
}
"""


def move_running(x: int | None = None, y: int | None = None) -> dict:
    """Bring live off-screen emulator window(s) back. Never touches other apps."""
    if x is None or y is None:
        cx, cy = center_for()
        x, y = cx if x is None else x, cy if y is None else y
    ps = _MOVE_PS.replace("__X__", str(int(x))).replace("__Y__", str(int(y)))
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
        out = (p.stdout or "").strip()
        if "NOEMU" in out:
            return {"ok": False, "moved": [],
                    "note": "No running emulator window found — use reset (next boot) instead."}
        return {"ok": True, "moved": out.splitlines()[-4:],
                "note": "Window moved live — no reboot needed."}
    except Exception as e:
        return {"ok": False, "moved": [], "err": str(e)}


def fix_window(name: str) -> dict:
    """One-click fix: live-move if running + always reset saved pos for next boot."""
    saved = reset_saved_position(name)
    live = move_running()
    return {"ok": saved["ok"], "saved": saved, "live": live,
            "tip": ("If the window is still too tall for your 1536x864 screen, "
                    "click the emulator and press Ctrl+Down to zoom it smaller, "
                    "or switch to a smaller skin/resolution.")}
