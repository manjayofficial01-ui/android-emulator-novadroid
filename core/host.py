"""Host-machine telemetry — the "is my PC keeping up?" half of the dashboard.

A smooth emulator is two machines agreeing: the guest Android and the host
PC feeding it. Everything else in NovaDroid measures the guest; this module
measures the host that runs QEMU, so an over-subscribed PC is obvious.

Stdlib-only. On Windows it talks to kernel32 directly (GetSystemTimes for
CPU, GlobalMemoryStatusEx for RAM, GetDiskFreeSpaceExW for disk) because
there is no stdlib `psutil`. Elsewhere it degrades to /proc + os.statvfs.
Every probe is best-effort: a missing counter yields None, never an
exception, so a telemetry glitch can never 500 the dashboard.
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path

from .runner import run

IS_WINDOWS = os.name == "nt"

# Emulator-side processes worth surfacing (QEMU children + the Qt launcher
# + Google's crash reporter, which leaks a process per AVD).
EMU_PROCS = ("qemu-system-x86_64", "qemu-system-aarch64", "qemu-system-armel",
             "qemu-system-i386", "emulator", "crash-service", "crashpad_handler")

_lock = threading.Lock()
_last_times: tuple[int, int, int] | None = None
_last_pct: float | None = None


# --------------------------------------------------------------------------
# CPU
# --------------------------------------------------------------------------
def _times_win() -> tuple[int, int, int] | None:
    """(idle, kernel, user) FILETIME-ish counters from GetSystemTimes."""
    try:
        idle, kern, user = (ctypes.c_ulonglong(), ctypes.c_ulonglong(),
                            ctypes.c_ulonglong())
        ok = ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
        if ok:
            return int(idle.value), int(kern.value), int(user.value)
    except (AttributeError, OSError, ValueError):
        pass
    return None


def _times_posix() -> tuple[int, int, int] | None:
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            parts = fh.readline().split()[1:]
        vals = [int(p) for p in parts]
        user = vals[0] + vals[1]          # user + nice
        system = vals[2]                  # system
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        # GetSystemTimes' "kernel" includes idle; mirror that shape so the
        # maths below is identical on both platforms.
        return idle, system + idle, user
    except (OSError, ValueError, IndexError):
        return None


def _times() -> tuple[int, int, int] | None:
    return _times_win() if IS_WINDOWS else _times_posix()


def cpu_percent(sample: float = 0.25) -> float | None:
    """Host CPU utilisation 0–100, or None when the counter is unavailable.

    The very first call blocks for `sample` seconds to get a real delta;
    later calls are non-blocking and report the load since the last call
    (the dashboard polls every second, so that is exactly what we want).
    """
    global _last_times, _last_pct
    now = _times()
    if now is None:
        return None

    with _lock:
        prev = _last_times
        _last_times = now

    if prev is None:
        time.sleep(sample)          # once per process: give it a real window
        nxt = _times()
        if nxt is None:
            return None
        with _lock:
            _last_times = nxt
        prev, now = now, nxt

    idle0, kern0, user0 = prev
    idle1, kern1, user1 = now
    # kernel time already includes idle; total busy+idle is kernel + user.
    total = (kern1 - kern0) + (user1 - user0)
    idle = idle1 - idle0
    if total <= 0:
        return _last_pct
    pct = 100.0 * (total - idle) / total
    _last_pct = round(min(100.0, max(0.0, pct)), 1)
    return _last_pct


def cpu_count() -> int:
    return os.cpu_count() or 1


def load_avg() -> tuple[float, float, float] | None:
    """1/5/15-minute load averages (None on Windows — not exposed by kernel32)."""
    try:
        return tuple(round(x, 2) for x in os.getloadavg())  # type: ignore[return-value]
    except (AttributeError, OSError):
        return None


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------
class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def memory() -> dict:
    """Physical RAM in MB. {} when unavailable."""
    if IS_WINDOWS:
        try:
            m = _MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                total, avail = int(m.ullTotalPhys), int(m.ullAvailPhys)
                return {
                    "total_mb": total >> 20,
                    "avail_mb": avail >> 20,
                    "used_mb": (total - avail) >> 20,
                    "pct": round(100.0 * (total - avail) / total, 1) if total else None,
                }
        except (AttributeError, OSError, ValueError):
            pass
        return {}
    # Linux/macOS fallback
    info: dict[str, int] = {}
    try:
        for line in open("/proc/meminfo", encoding="utf-8"):
            k, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                info[k.strip()] = int(parts[0])  # kB
    except OSError:
        return {}
    if not info:
        return {}
    total = info.get("MemTotal", 0) // 1024
    avail = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
    return {"total_mb": total, "avail_mb": avail, "used_mb": max(0, total - avail),
            "pct": round(100.0 * (total - avail) / total, 1) if total else None}


# --------------------------------------------------------------------------
# Disk
# --------------------------------------------------------------------------
def disk_free(path: str | Path = "") -> dict:
    """Free/total MB for the volume holding `path` (defaults to the AVD home)."""
    target = str(path or Path.home())
    try:
        if IS_WINDOWS:
            free = ctypes.c_ulonglong()
            total = ctypes.c_ulonglong()
            tot_free = ctypes.c_ulonglong()
            ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(target), ctypes.byref(free),
                ctypes.byref(total), ctypes.byref(tot_free))
            if ok:
                return {"path": target, "total_mb": int(total.value) >> 20,
                        "free_mb": int(free.value) >> 20}
        else:
            st = os.statvfs(target)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            return {"path": target, "total_mb": total >> 20, "free_mb": free >> 20}
    except (AttributeError, OSError, ValueError):
        pass
    return {"path": target}


# --------------------------------------------------------------------------
# Emulator processes
# --------------------------------------------------------------------------
def _mem_to_mb(text: str) -> int:
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) // 1024 if digits else 0


def emulator_processes() -> list[dict]:
    """Live QEMU/emulator processes with their resident memory.

    This is the number that matters for multi-instance work: three "4 GB"
    AVDs do not cost 12 GB, they cost what QEMU has actually touched.
    """
    out: list[dict] = []
    if IS_WINDOWS:
        r = run(["tasklist", "/FO", "CSV", "/NH"], timeout=20)
        for line in r["out"].splitlines():
            parts = [p.strip().strip('"') for p in line.split('","')]
            if len(parts) < 5:
                continue
            name, pid, mem = parts[0], parts[1], parts[4]
            base = name[:-4].lower() if name.lower().endswith(".exe") else name.lower()
            if base in EMU_PROCS and pid.isdigit():
                out.append({"name": name, "pid": int(pid), "mem_mb": _mem_to_mb(mem)})
    else:
        r = run(["ps", "-eo", "pid,rss,comm"], timeout=20)
        for line in r["out"].splitlines()[1:]:
            parts = line.split()
            if len(parts) < 3:
                continue
            pid, rss, comm = parts
            if comm.lower() in EMU_PROCS and pid.isdigit():
                out.append({"name": comm, "pid": int(pid), "mem_mb": int(rss) // 1024})
    return out


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------
def snapshot() -> dict:
    """Everything the dashboard's System panel shows, in one call."""
    emus = emulator_processes()
    mem = memory()
    return {
        "ok": True,
        "t": time.time(),
        "cpu_pct": cpu_percent(),
        "cpu_count": cpu_count(),
        "load": load_avg(),
        "mem": mem,
        "disk": disk_free(),
        "emulators": emus,
        "emulator_count": len(emus),
        "emulator_mem_mb": sum(p["mem_mb"] for p in emus),
    }
