"""ADB wrappers: devices, APK install, screenshots, sensors, perf, macros.

Uses `android install` (delta-install, faster than raw adb) when available,
falls back to adb. Screenshots via `android screen capture` w/ adb fallback.
"""
from __future__ import annotations

import base64
import os
import tempfile
import time
from pathlib import Path

from .config import ToolPaths
from .runner import run


def devices(paths: ToolPaths) -> dict:
    if not paths.adb:
        return {"ok": False, "devices": [], "err": "adb not found"}
    r = run([paths.adb, "devices", "-l"], timeout=15)
    devs = []
    for line in r["out"].splitlines()[1:]:
        parts = line.split()
        if not parts:
            continue
        serial, state = parts[0], parts[1] if len(parts) > 1 else "?"
        if serial == "List":
            continue
        props = {}
        for p in parts[2:]:
            if ":" in p:
                k, v = p.split(":", 1)
                props[k] = v
        devs.append({"serial": serial, "state": state, **props})
    # Enrich online devices with model/android version (best effort)
    for d in devs:
        if d["state"] == "device":
            d.update(device_props(paths, d["serial"]))
    return {"ok": True, "devices": devs}


def device_props(paths: ToolPaths, serial: str) -> dict:
    out: dict = {}
    for prop, key in (("ro.product.model", "model"),
                      ("ro.build.version.release", "android"),
                      ("ro.build.version.sdk", "sdk"),
                      ("ro.product.cpu.abi", "abi")):
        r = run([paths.adb, "-s", serial, "shell", "getprop", prop], timeout=10)
        if r["ok"] and r["out"]:
            out[key] = r["out"].strip()
    return out


def install_apk(paths: ToolPaths, serial: str, apk_path: str) -> dict:
    p = Path(apk_path)
    if not p.exists():
        return {"ok": False, "err": f"APK not found: {apk_path}"}
    if paths.android_cli:
        r = run([paths.android_cli, "install", "--device", serial,
                 "--apks", str(p)], timeout=180)
        if r["ok"]:
            return {"ok": True, "tool": "android-install(delta)", "log": r["out"][-1500:]}
    r = run([paths.adb, "-s", serial, "install", "-r", "-g", str(p)], timeout=180)
    return {"ok": r["ok"], "tool": "adb", "log": (r["out"] + r["err"])[-1500:],
            "note": "Installed via package manager (-g = all runtime permissions granted); no 'unknown sources' prompt needed."}


def screenshot(paths: ToolPaths, serial: str) -> dict:
    """Returns {ok, png_b64}. Prefers android CLI, falls back to adb screencap."""
    if paths.android_cli:
        with tempfile.TemporaryDirectory() as td:
            fp = os.path.join(td, "shot.png")
            r = run([paths.android_cli, "screen", "capture", fp,
                     "--device", serial] if False else
                    [paths.android_cli, "screen", "capture", "--device", serial, fp],
                    timeout=30)
            # android CLI arg order varies by version; try both spellings
            if not os.path.exists(fp):
                r = run([paths.android_cli, "screen", "capture", fp], timeout=30)
            if os.path.exists(fp):
                data = Path(fp).read_bytes()
                return {"ok": True, "png_b64": base64.b64encode(data).decode(),
                        "bytes": len(data)}
    # adb fallback — industry standard path
    with tempfile.TemporaryDirectory() as td:
        fp = os.path.join(td, "s.png")
        r1 = run([paths.adb, "-s", serial, "shell", "screencap", "-p", "/sdcard/s.png"], timeout=30)
        if not r1["ok"]:
            return {"ok": False, "err": r1["err"][-500:]}
        r2 = run([paths.adb, "-s", serial, "pull", "/sdcard/s.png", fp], timeout=30)
        if os.path.exists(fp):
            data = Path(fp).read_bytes()
            return {"ok": True, "png_b64": base64.b64encode(data).decode(),
                    "bytes": len(data)}
        return {"ok": False, "err": (r2["out"] + r2["err"])[-500:]}


def layout(paths: ToolPaths, serial: str) -> dict:
    if paths.android_cli:
        r = run([paths.android_cli, "layout", "--device", serial], timeout=30)
        if r["ok"] and r["out"]:
            from .runner import try_json
            return {"ok": True, "tree": try_json(r["out"]) or r["out"][:8000]}
    r = run([paths.adb, "-s", serial, "shell", "uiautomator", "dump", "/sdcard/ui.xml"],
            timeout=30)
    r2 = run([paths.adb, "-s", serial, "shell", "cat", "/sdcard/ui.xml"], timeout=15)
    return {"ok": r2["ok"], "tree": r2["out"][:8000], "err": r2["err"][-500:]}


def geo_fix(paths: ToolPaths, serial: str, lat: float, lng: float) -> dict:
    r = run([paths.adb, "-s", serial, "emu", "geo", "fix",
             str(lng), str(lat)], timeout=15)
    if not r["ok"]:  # older avds: lat/lng order via telnet console differs; retry swapped
        r = run([paths.adb, "-s", serial, "emu", "geo", "fix",
                 str(lat), str(lng)], timeout=15)
    return {"ok": r["ok"], "log": (r["out"] + r["err"])[-500:]}


def battery(paths: ToolPaths, serial: str, level: int = 85, status: str = "not-charging") -> dict:
    run([paths.adb, "-s", serial, "shell", "dumpsys", "battery", "set", "level", str(level)], timeout=10)
    r = run([paths.adb, "-s", serial, "shell", "dumpsys", "battery", "set", "status", status], timeout=10)
    return {"ok": True, "log": (r["out"] + r["err"])[-300:]}


def perf(paths: ToolPaths, serial: str) -> dict:
    """Guest perf snapshot: cpu, mem, uptime — feeds the dashboard."""
    out: dict = {"serial": serial, "t": time.time()}
    r = run([paths.adb, "-s", serial, "shell",
             "cat /proc/loadavg; echo ---; cat /proc/meminfo | head -4; echo ---; uptime"], timeout=15)
    out["raw"] = r["out"][:2000]
    r2 = run([paths.adb, "-s", serial, "shell", "dumpsys", "cpuinfo", "|", "head", "-20"], timeout=15)
    out["cpuinfo"] = (r2["out"] or r2["err"])[:1500]
    return {"ok": r["ok"], "perf": out}


def shell(paths: ToolPaths, serial: str, cmd: str) -> dict:
    """Guarded adb shell (blocks obvious destructive ops)."""
    banned = ["reboot -p", "rm -rf /", "mkfs", "dd if=", ":(){:|:&};:"]
    if any(b in cmd for b in banned):
        return {"ok": False, "err": "blocked by safety filter"}
    r = run([paths.adb, "-s", serial, "shell", cmd], timeout=30)
    return {"ok": r["ok"], "out": r["out"][:6000], "err": r["err"][-1000:]}


# --- Macros / keymapping (Nox/LDPlayer-style automation, file-based) ---
MACRO_DIR = Path.home() / ".novadroid" / "macros"
MACRO_DIR.mkdir(parents=True, exist_ok=True)


def macro_save(name: str, steps: list) -> dict:
    safe = "".join(c for c in name if c.isalnum() or c in "-_")[:64] or "macro"
    fp = MACRO_DIR / f"{safe}.json"
    import json as _j
    fp.write_text(_j.dumps({"name": safe, "steps": steps}), encoding="utf-8")
    return {"ok": True, "file": str(fp), "steps": len(steps)}


def macro_list() -> dict:
    import json as _j
    items = []
    for fp in sorted(MACRO_DIR.glob("*.json")):
        try:
            d = _j.loads(fp.read_text(encoding="utf-8"))
            items.append({"name": d.get("name", fp.stem),
                          "steps": len(d.get("steps", [])), "file": str(fp)})
        except Exception:
            pass
    return {"ok": True, "macros": items}


def macro_run(paths: ToolPaths, serial: str, name: str) -> dict:
    """Replay tap/swipe/text/key/sleep steps via adb input."""
    import json as _j
    safe = "".join(c for c in name if c.isalnum() or c in "-_")[:64]
    fp = MACRO_DIR / f"{safe}.json"
    if not fp.exists():
        return {"ok": False, "err": f"macro not found: {safe}"}
    d = _j.loads(fp.read_text(encoding="utf-8"))
    log = []
    for st in d.get("steps", []):
        t = st.get("type")
        if t == "tap":
            r = run([paths.adb, "-s", serial, "shell", "input", "tap",
                     str(st["x"]), str(st["y"])], timeout=10)
        elif t == "swipe":
            r = run([paths.adb, "-s", serial, "shell", "input", "swipe",
                     str(st["x1"]), str(st["y1"]), str(st["x2"]), str(st["y2"]),
                     str(st.get("ms", 300))], timeout=10)
        elif t == "text":
            r = run([paths.adb, "-s", serial, "shell", "input", "text",
                     str(st["text"]).replace(" ", "%s")], timeout=10)
        elif t == "key":
            r = run([paths.adb, "-s", serial, "shell", "input", "keyevent",
                     str(st["code"])], timeout=10)
        elif t == "sleep":
            time.sleep(min(float(st.get("s", 1)), 10))
            log.append("sleep ok")
            continue
        else:
            log.append(f"skip unknown {t}")
            continue
        log.append(f"{t}: {'ok' if r['ok'] else 'FAIL'}")
    return {"ok": True, "log": log}
