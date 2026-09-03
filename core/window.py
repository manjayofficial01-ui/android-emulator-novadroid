"""Emulator window placement fixes (Windows).

Root cause of "booted above the screen": the emulator persists its Qt window
position in <avd>.avd/emulator-user.ini (window.x / window.y) and restores it
on next boot. If it was closed while off-screen — or the display setup changed
(resolution, scaling, unplugged monitor) — it reopens there with the title bar
unreachable. Cold boot / wipe does NOT fix it (not a snapshot issue).

Three layers, all stdlib-only:
 1. reset_saved_position(): rewrite window.x/y in emulator-user.ini while the
    AVD is STOPPED (the emulator overwrites this file on exit, so editing a
    running instance is pointless). Next boot lands centered with title bar
    at y=60.
 2. move_running(): SetWindowPos the live window via PowerShell. Matching is
    deliberately strict — process must be emulator/qemu-system-* AND title
    must case-sensitively match '^Android Emulator - ' — so IDEs or browsers
    with similar words in their titles are never touched.
 3. FIT: if a live window is taller/wider than the work area it is also
    rescaled (aspect preserved), which removes the last way it can end up
    with its title bar out of reach.

DPI note: GetSystemMetrics returns *virtualised* pixels unless the process is
DPI-aware, so on a 125%/150% scaled display it reported a smaller screen than
the user actually has and "fix" could still place the window off-screen. We
declare per-monitor DPI awareness before measuring.
"""
from __future__ import annotations

import ctypes
import subprocess
from ctypes import wintypes
from pathlib import Path

FALLBACK_SCREEN = (1920, 1080)
TITLE_MARGIN = 60   # keeps the title bar comfortably grabbable

_dpi_done = False


def _dpi_aware() -> None:
    """Make our measurements match what the user sees (best effort, once)."""
    global _dpi_done
    if _dpi_done:
        return
    _dpi_done = True
    try:
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE (Win 8.1+)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


def screen_size() -> tuple[int, int]:
    """Primary display size in REAL pixels (DPI-aware)."""
    _dpi_aware()
    try:
        u = ctypes.windll.user32
        return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    except (AttributeError, OSError, ValueError):
        return FALLBACK_SCREEN


def work_area() -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the primary monitor's work area.

    Excludes the taskbar, so centring against this never hides a window
    behind it. Falls back to the full screen.
    """
    _dpi_aware()
    try:
        r = _RECT()
        # SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            if r.right > r.left and r.bottom > r.top:
                return int(r.left), int(r.top), int(r.right), int(r.bottom)
    except (AttributeError, OSError, ValueError):
        pass
    sw, sh = screen_size()
    return 0, 0, sw, sh


def center_for(win_w: int = 500) -> tuple[int, int]:
    left, top, right, _bottom = work_area()
    x = max(left, left + (right - left - win_w) // 2)
    return x, top + TITLE_MARGIN


def max_window_size() -> tuple[int, int]:
    """Largest (w, h) that still fits on screen with a grabbable title bar."""
    left, top, right, bottom = work_area()
    return max(320, right - left - 40), max(320, bottom - top - TITLE_MARGIN - 20)


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
    except OSError as e:
        info["err"] = str(e)
    left, top, right, bottom = work_area()
    info["screen"] = f"{right - left}x{bottom - top}"
    info["work_area"] = f"{left},{top} {right}x{bottom - top}"
    try:
        x = int(float(info["x"])) if info["x"] not in (None, "") else None
        y = int(float(info["y"])) if info["y"] not in (None, "") else None
        # Off-screen means: no title bar reachable, or pushed past the edges.
        info["offscreen"] = (x is not None and (x < left - 8 or x > right - 80)) or \
                            (y is not None and (y < top - 8 or y > bottom - 40))
    except (TypeError, ValueError):
        info["offscreen"] = False
    return info


def reset_saved_position(name: str, x: int | None = None, y: int | None = None) -> dict:
    """Rewrite saved window coords. AVD must be STOPPED for this to stick."""
    from . import inifile
    avd_dir = Path.home() / ".android" / "avd" / f"{name}.avd"
    if not avd_dir.exists():
        return {"ok": False, "err": f"AVD not found: {name}"}
    if x is None or y is None:
        cx, cy = center_for()
        x, y = cx if x is None else x, cy if y is None else y
    try:
        inifile.upsert(user_ini(name), {"window.x": str(int(x)), "window.y": str(int(y))})
    except OSError as e:
        return {"ok": False, "err": f"could not write emulator-user.ini: {e}"}
    return {"ok": True, "avd": name, "x": int(x), "y": int(y),
            "note": "Takes effect on next boot. Reopen the AVD to verify."}


# Strict PowerShell: process allowlist + case-SENSITIVE title regex.
# Also rescales (aspect preserved) when the window cannot fit the work area.
_MOVE_PS = r"""
Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f); [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r); public struct RECT { public int L; public int T; public int R; public int B; }' -Name WinMove -Namespace W32;
$procs = @(Get-Process | Where-Object { ($_.ProcessName -in 'emulator','qemu-system-x86_64','qemu-system-aarch64') -and ($_.MainWindowTitle -cmatch '^Android Emulator - ') });
if ($procs.Count -eq 0) { Write-Output 'NOEMU'; exit 0 }
foreach ($e in $procs) {
  $r = New-Object W32.WinMove+RECT; [W32.WinMove]::GetWindowRect($e.MainWindowHandle, [ref]$r) | Out-Null;
  $w = $r.R - $r.L; $h = $r.B - $r.T;
  $cx = 0; $cy = 0; $flags = 5; $fit = 'no';
  if ($w -gt 0 -and $h -gt 0 -and ($w -gt __MAXW__ -or $h -gt __MAXH__)) {
    $s = [Math]::Min((__MAXW__ * 1.0) / $w, (__MAXH__ * 1.0) / $h);
    $cx = [int][Math]::Floor($w * $s); $cy = [int][Math]::Floor($h * $s);
    $flags = 4; $fit = 'yes';
  }
  [W32.WinMove]::SetWindowPos($e.MainWindowHandle, [IntPtr]::Zero, __X__, __Y__, $cx, $cy, $flags) | Out-Null;
  $r2 = New-Object W32.WinMove+RECT; [W32.WinMove]::GetWindowRect($e.MainWindowHandle, [ref]$r2) | Out-Null;
  Write-Output ("MOVED id=" + $e.Id + " L=" + $r2.L + " T=" + $r2.T + " W=" + ($r2.R - $r2.L) + " H=" + ($r2.B - $r2.T) + " fit=" + $fit);
}
"""


def move_running(x: int | None = None, y: int | None = None,
                 fit: bool = True) -> dict:
    """Bring live off-screen emulator window(s) back. Never touches other apps."""
    if x is None or y is None:
        cx, cy = center_for()
        x, y = cx if x is None else x, cy if y is None else y
    max_w, max_h = max_window_size() if fit else (10 ** 6, 10 ** 6)
    ps = (_MOVE_PS
          .replace("__X__", str(int(x)))
          .replace("__Y__", str(int(y)))
          .replace("__MAXW__", str(int(max_w)))
          .replace("__MAXH__", str(int(max_h))))
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=90)
        out = (p.stdout or "").strip()
        if "NOEMU" in out:
            return {"ok": False, "moved": [],
                    "note": "No running emulator window found — use reset (next boot) instead."}
        lines = [l for l in out.splitlines() if l.startswith("MOVED")]
        return {"ok": bool(lines), "moved": lines[-4:],
                "note": "Window moved live — no reboot needed."}
    except Exception as e:
        return {"ok": False, "moved": [], "err": str(e)}


def fix_window(name: str) -> dict:
    """One-click fix: live-move (+ refit) if running, and always reset the
    saved position so the next boot lands on screen too."""
    saved = reset_saved_position(name)
    live = move_running()
    left, top, right, bottom = work_area()
    return {"ok": saved["ok"], "saved": saved, "live": live,
            "screen": f"{right - left}x{bottom - top}",
            "tip": (f"Window is placed for your {right - left}x{bottom - top} work area. "
                    "If the emulator still renders too large, switch to a smaller "
                    "resolution or device frame, or press Ctrl+Down in the emulator "
                    "window to zoom out.")}
