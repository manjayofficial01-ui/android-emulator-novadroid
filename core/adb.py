"""ADB wrappers: devices, APK install, screenshots, sensors, perf, macros, input.

Screenshot pipeline matters for "smooth" — see `screenshot()`: the primary
path is `adb exec-out screencap -p`, which streams the PNG straight to our
stdout in ONE process. The old path spawned `screencap` + `pull` across two
processes plus a temp file per frame, which is what made 3-second live view
feel like 8.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from pathlib import Path

from .config import ToolPaths
from .runner import run, run_bytes, try_json

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _check(paths: ToolPaths, serial: str) -> dict | None:
    if not paths.adb:
        return {"ok": False, "err": "adb not found"}
    if not serial:
        return {"ok": False, "err": "no device selected — Quickboot an AVD first"}
    return None


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------
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
        if serial in ("List", "*"):
            continue
        props = {}
        for p in parts[2:]:
            if ":" in p:
                k, v = p.split(":", 1)
                props[k] = v
        devs.append({"serial": serial, "state": state, **props})
    # Enrich online devices in PARALLEL: one getprop round-trip per property
    # per device is the single biggest latency in a 3-instance fleet.
    online = [d for d in devs if d["state"] == "device"]
    if online:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(online))) as pool:
            for d, extra in zip(online, pool.map(
                    lambda d: device_props(paths, d["serial"]), online)):
                d.update(extra)
    return {"ok": True, "devices": devs}


def device_props(paths: ToolPaths, serial: str) -> dict:
    out: dict = {}
    if not paths.adb or not serial:
        return out
    script = ("getprop ro.product.model; getprop ro.build.version.release; "
              "getprop ro.build.version.sdk; getprop ro.product.cpu.abi; "
              "getprop ro.product.cpu.abilist; getprop ro.dalvik.vm.native.bridge")
    r = run([paths.adb, "-s", serial, "shell", script], timeout=20)
    vals = [v.strip() for v in r["out"].splitlines()]
    keys = ("model", "android", "sdk", "abi", "abilist", "bridge")
    for k, v in zip(keys, vals):
        if v:
            out[k] = v
    return out


def online(paths: ToolPaths, serial: str) -> bool:
    if not paths.adb or not serial:
        return False
    r = run([paths.adb, "devices"], timeout=10)
    return any(parts[:2] == [serial, "device"]
               for parts in (l.split() for l in r["out"].splitlines()))


# --------------------------------------------------------------------------
# APK install
# --------------------------------------------------------------------------
def install_apk(paths: ToolPaths, serial: str, apk_path: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    p = Path(apk_path or "")
    if not p.exists():
        return {"ok": False, "err": f"APK not found: {apk_path}"}
    if paths.android_cli:
        r = run([paths.android_cli, "install", "--device", serial,
                 "--apks", str(p)], timeout=300)
        if r["ok"]:
            return {"ok": True, "tool": "android-install(delta)", "log": r["out"][-1500:]}
    r = run([paths.adb, "-s", serial, "install", "-r", "-g", str(p)], timeout=300)
    return {"ok": r["ok"], "tool": "adb", "log": (r["out"] + r["err"])[-1500:],
            "note": ("Installed via package manager (-g = all runtime permissions "
                     "granted); no 'unknown sources' prompt needed.")}


# --------------------------------------------------------------------------
# Screenshots
# --------------------------------------------------------------------------
def screenshot(paths: ToolPaths, serial: str) -> dict:
    """Returns {ok, png_b64}. Three tiers, fastest first.

      1. `adb exec-out screencap -p`  — 1 process, no temp file, binary-safe.
      2. `screencap` to /sdcard + `pull` — works on ancient adb.
      3. `android screen capture` CLI — last resort.
    """
    if (e := _check(paths, serial)):
        return e

    rb = run_bytes([paths.adb, "-s", serial, "exec-out", "screencap", "-p"], timeout=25)
    data = rb["out"] or b""
    if data.startswith(PNG_MAGIC) and data[-8:] == b"IEND\xaeB`\x82" or \
            (data.startswith(PNG_MAGIC) and len(data) > 1024):
        return {"ok": True, "png_b64": base64.b64encode(data).decode(),
                "bytes": len(data), "via": "exec-out"}

    with tempfile.TemporaryDirectory() as td:
        fp = os.path.join(td, "s.png")
        r1 = run([paths.adb, "-s", serial, "shell", "screencap", "-p", "/sdcard/s.png"],
                 timeout=30)
        if r1["ok"]:
            run([paths.adb, "-s", serial, "pull", "/sdcard/s.png", fp], timeout=30)
        if os.path.exists(fp):
            raw = Path(fp).read_bytes()
            if raw.startswith(PNG_MAGIC):
                return {"ok": True, "png_b64": base64.b64encode(raw).decode(),
                        "bytes": len(raw), "via": "screencap+pull"}

    if paths.android_cli:
        with tempfile.TemporaryDirectory() as td:
            fp = os.path.join(td, "shot.png")
            run([paths.android_cli, "screen", "capture", "--device", serial, fp],
                timeout=30)
            if os.path.exists(fp):
                raw = Path(fp).read_bytes()
                if raw.startswith(PNG_MAGIC):
                    return {"ok": True, "png_b64": base64.b64encode(raw).decode(),
                            "bytes": len(raw), "via": "android-cli"}
    return {"ok": False, "err": (rb["err"] or "screencap produced no PNG")[-400:]}


def layout(paths: ToolPaths, serial: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    if paths.android_cli:
        r = run([paths.android_cli, "layout", "--device", serial], timeout=30)
        if r["ok"] and r["out"]:
            return {"ok": True, "tree": try_json(r["out"]) or r["out"][:8000],
                    "via": "android-layout"}
    run([paths.adb, "-s", serial, "shell", "uiautomator", "dump", "/sdcard/ui.xml"],
        timeout=30)
    r2 = run([paths.adb, "-s", serial, "shell", "cat", "/sdcard/ui.xml"], timeout=15)
    return {"ok": r2["ok"], "tree": r2["out"][:8000], "err": r2["err"][-500:],
            "via": "uiautomator"}


# --------------------------------------------------------------------------
# Input primitives (used by macros AND by the interactive live view)
# --------------------------------------------------------------------------
def _clamp_int(value, lo: int, hi: int, default: int = 0) -> int:
    try:
        return max(lo, min(int(float(value)), hi))
    except (TypeError, ValueError):
        return default


def tap(paths: ToolPaths, serial: str, x, y) -> dict:
    if (e := _check(paths, serial)):
        return e
    x, y = _clamp_int(x, 0, 20000), _clamp_int(y, 0, 20000)
    return run([paths.adb, "-s", serial, "shell", "input", "tap", str(x), str(y)],
               timeout=15)


def swipe(paths: ToolPaths, serial: str, x1, y1, x2, y2, ms: int = 300) -> dict:
    if (e := _check(paths, serial)):
        return e
    pts = [_clamp_int(v, 0, 20000) for v in (x1, y1, x2, y2)]
    ms = _clamp_int(ms, 0, 60000, 300)
    return run([paths.adb, "-s", serial, "shell", "input", "swipe",
                *(str(p) for p in pts), str(ms)], timeout=20)


def long_press(paths: ToolPaths, serial: str, x, y, ms: int = 1000) -> dict:
    """adb's `input swipe` doubles as a long-press when start == end."""
    return swipe(paths, serial, x, y, x, y, max(300, ms))


def input_text(paths: ToolPaths, serial: str, text: str) -> dict:
    """Type text. Spaces become %s (adb's escape), which is what `input text`
    expects; everything else is percent-decoded by the device."""
    if (e := _check(paths, serial)):
        return e
    text = str(text or "")
    if not text:
        return {"ok": False, "err": "no text given"}
    payload = text.replace(" ", "%s").replace("'", "")[:400]
    return run([paths.adb, "-s", serial, "shell", "input", "text", payload], timeout=20)


def keyevent(paths: ToolPaths, serial: str, code) -> dict:
    if (e := _check(paths, serial)):
        return e
    code = _clamp_int(code, 0, 300, 0)
    return run([paths.adb, "-s", serial, "shell", "input", "keyevent", str(code)],
               timeout=15)


# --------------------------------------------------------------------------
# Sensors / power
# --------------------------------------------------------------------------
def geo_fix(paths: ToolPaths, serial: str, lat: float, lng: float) -> dict:
    if (e := _check(paths, serial)):
        return e
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return {"ok": False, "err": "lat/lng must be numbers"}
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return {"ok": False, "err": "lat must be -90..90 and lng -180..180"}
    # The emulator console takes longitude FIRST.
    r = run([paths.adb, "-s", serial, "emu", "geo", "fix", f"{lng}", f"{lat}"], timeout=15)
    if not r["ok"]:
        r = run([paths.adb, "-s", serial, "emu", "geo", "fix",
                 f"{lat}", f"{lng}"], timeout=15)
    return {"ok": r["ok"], "lat": lat, "lng": lng,
            "log": (r["out"] + r["err"])[-500:]}


# `dumpsys battery set status` accepts the numeric BatteryManager constants.
# Passing the human names ("charging") replies "Bad value: charging" on
# Android 12+ — which the old code swallowed and reported as success.
BATTERY_STATUS = {"unknown": 1, "charging": 2, "discharging": 3,
                  "not-charging": 4, "full": 5}


def battery(paths: ToolPaths, serial: str, level: int = 85,
            status: str = "not-charging") -> dict:
    if (e := _check(paths, serial)):
        return e
    level = _clamp_int(level, 0, 100, 85)
    key = (status or "").strip().lower()
    code = BATTERY_STATUS.get(key)
    if code is None:
        code = int(key) if key.isdigit() and 1 <= int(key) <= 5 else 4
    script = (f"dumpsys battery set level {level}; "
              f"dumpsys battery set status {code}")
    r = run([paths.adb, "-s", serial, "shell", script], timeout=20)
    applied = r["ok"] and "Bad value" not in (r["out"] + r["err"])
    if not applied:
        return {"ok": False, "level": level, "status": key,
                "err": (r["out"] + r["err"]).strip()[-300:] or "battery set failed"}
    # Read back so the UI shows what the device actually reports, not what
    # we hoped it would do.
    chk = run([paths.adb, "-s", serial, "shell",
               "dumpsys battery | grep -E 'level|status'"], timeout=20)
    return {"ok": True, "level": level, "status": key, "status_code": code,
            "log": (chk["out"] or "set")[-300:], "verified": True}


def battery_reset(paths: ToolPaths, serial: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    r = run([paths.adb, "-s", serial, "shell", "dumpsys battery reset"], timeout=20)
    return {"ok": bool(r["ok"]), "log": (r["out"] + r["err"])[-300:],
            "note": "battery mocking off — real (emulated) charge reported again"}


# --------------------------------------------------------------------------
# Perf — structured, not a wall of text
# --------------------------------------------------------------------------
def _parse_meminfo(text: str) -> dict:
    vals: dict[str, int] = {}
    for line in text.splitlines():
        k, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            vals[k.strip()] = int(parts[0])   # kB
    total = vals.get("MemTotal", 0)
    avail = vals.get("MemAvailable", vals.get("MemFree", 0))
    return {
        "total_mb": total // 1024,
        "avail_mb": avail // 1024,
        "used_mb": max(0, (total - avail)) // 1024,
        "pct": round(100.0 * (total - avail) / total, 1) if total else None,
        "cached_mb": vals.get("Cached", 0) // 1024,
    }


def _parse_loadavg(text: str) -> dict:
    parts = text.split()
    if len(parts) < 3:
        return {}
    try:
        return {"load1": float(parts[0]), "load5": float(parts[1]),
                "load15": float(parts[2])}
    except ValueError:
        return {}


def perf(paths: ToolPaths, serial: str) -> dict:
    """Guest perf snapshot: load, memory, uptime, top CPU consumers."""
    if (e := _check(paths, serial)):
        return e
    r = run([paths.adb, "-s", serial, "shell",
             "cat /proc/loadavg; echo ---; cat /proc/meminfo; echo ---; "
             "uptime; echo ---; top -b -n 1 -m 8 -q 2>/dev/null || "
             "dumpsys cpuinfo | head -20"], timeout=30)
    raw = r["out"]
    sections = raw.split("---")
    load = _parse_loadavg(sections[0].strip()) if sections else {}
    mem = _parse_meminfo(sections[1]) if len(sections) > 1 else {}
    uptime = sections[2].strip() if len(sections) > 2 else ""
    top = sections[3].strip() if len(sections) > 3 else ""
    if not top:
        top = run([paths.adb, "-s", serial, "shell", "dumpsys cpuinfo | head -18"],
                  timeout=20)["out"]
    return {"ok": r["ok"], "perf": {
        "serial": serial, "t": time.time(), "load": load, "mem": mem,
        "uptime": uptime[:200], "top": top[:1200], "raw": raw[:2000],
    }}


def shell(paths: ToolPaths, serial: str, cmd: str) -> dict:
    """Guarded adb shell (blocks obvious destructive ops)."""
    if (e := _check(paths, serial)):
        return e
    banned = ["reboot -p", "rm -rf /", "mkfs", "dd if=", ":(){:|:&};:",
              "wipe", "format ", "fastboot"]
    low = (cmd or "").lower()
    if any(b in low for b in banned):
        return {"ok": False, "err": "blocked by safety filter"}
    r = run([paths.adb, "-s", serial, "shell", cmd], timeout=45)
    return {"ok": r["ok"], "out": r["out"][:6000], "err": r["err"][-1000:]}


# --------------------------------------------------------------------------
# Macros / keymapping (Nox/LDPlayer-style automation, file-based)
# --------------------------------------------------------------------------
MACRO_DIR = Path.home() / ".novadroid" / "macros"
MACRO_DIR.mkdir(parents=True, exist_ok=True)

_MACRO_TYPES = {"tap", "swipe", "text", "key", "sleep"}


def _validate_steps(steps) -> tuple[list[dict], list[str]]:
    """Drop anything malformed instead of KeyError-ing mid-replay."""
    good: list[dict] = []
    bad: list[str] = []
    for i, st in enumerate(steps or []):
        if not isinstance(st, dict):
            bad.append(f"#{i} not an object")
            continue
        t = st.get("type")
        if t not in _MACRO_TYPES:
            bad.append(f"#{i} unknown type {t!r}")
            continue
        try:
            if t == "tap":
                st = {"type": "tap", "x": _clamp_int(st["x"], 0, 20000),
                      "y": _clamp_int(st["y"], 0, 20000)}
            elif t == "swipe":
                st = {"type": "swipe", "x1": _clamp_int(st["x1"], 0, 20000),
                      "y1": _clamp_int(st["y1"], 0, 20000),
                      "x2": _clamp_int(st["x2"], 0, 20000),
                      "y2": _clamp_int(st["y2"], 0, 20000),
                      "ms": _clamp_int(st.get("ms", 300), 0, 60000, 300)}
            elif t == "text":
                if not str(st.get("text", "")):
                    bad.append(f"#{i} empty text")
                    continue
                st = {"type": "text", "text": str(st["text"])[:400]}
            elif t == "key":
                st = {"type": "key", "code": _clamp_int(st["code"], 0, 300)}
            else:
                st = {"type": "sleep", "s": _clamp_int(st.get("s", 1), 0, 60, 1)}
        except (KeyError, TypeError, ValueError) as ex:
            bad.append(f"#{i} {ex}")
            continue
        good.append(st)
    return good, bad


def _safe_macro_name(name: str) -> str:
    return "".join(c for c in str(name or "") if c.isalnum() or c in "-_")[:64]


def macro_save(name: str, steps: list) -> dict:
    safe = _safe_macro_name(name) or "macro"
    good, bad = _validate_steps(steps)
    fp = MACRO_DIR / f"{safe}.json"
    fp.write_text(json.dumps({"name": safe, "steps": good}, indent=1), encoding="utf-8")
    return {"ok": True, "file": str(fp), "steps": len(good), "rejected": bad}


def macro_list() -> dict:
    items = []
    for fp in sorted(MACRO_DIR.glob("*.json")):
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
            items.append({"name": d.get("name", fp.stem),
                          "steps": len(d.get("steps", [])), "file": str(fp)})
        except Exception:
            continue
    return {"ok": True, "macros": items}


def macro_read(name: str) -> dict:
    fp = MACRO_DIR / f"{_safe_macro_name(name)}.json"
    if not fp.exists():
        return {"ok": False, "err": f"macro not found: {name}"}
    try:
        return {"ok": True, "macro": json.loads(fp.read_text(encoding="utf-8"))}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def macro_delete(name: str) -> dict:
    fp = MACRO_DIR / f"{_safe_macro_name(name)}.json"
    if not fp.exists():
        return {"ok": False, "err": f"macro not found: {name}"}
    try:
        fp.unlink()
        return {"ok": True, "deleted": fp.stem}
    except OSError as e:
        return {"ok": False, "err": str(e)}


def macro_run(paths: ToolPaths, serial: str, name: str) -> dict:
    """Replay tap/swipe/text/key/sleep steps via adb input."""
    if (e := _check(paths, serial)):
        return e
    safe = _safe_macro_name(name)
    fp = MACRO_DIR / f"{safe}.json"
    if not fp.exists():
        return {"ok": False, "err": f"macro not found: {safe}"}
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as ex:
        return {"ok": False, "err": f"macro is unreadable: {ex}"}
    log: list[str] = []
    for i, st in enumerate(d.get("steps", [])):
        t = st.get("type")
        if t == "tap":
            r = tap(paths, serial, st["x"], st["y"])
        elif t == "swipe":
            r = swipe(paths, serial, st["x1"], st["y1"], st["x2"], st["y2"],
                      st.get("ms", 300))
        elif t == "text":
            r = input_text(paths, serial, st["text"])
        elif t == "key":
            r = keyevent(paths, serial, st["code"])
        elif t == "sleep":
            time.sleep(float(st.get("s", 1)))
            log.append("sleep ok")
            continue
        else:
            log.append(f"skip unknown {t}")
            continue
        log.append(f"{i}:{t} {'ok' if r.get('ok') else 'FAIL'}")
    return {"ok": True, "log": log, "macro": safe}
