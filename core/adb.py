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
import re
import tempfile
import time
from pathlib import Path

from . import safe
from .config import ToolPaths
from .runner import run, run_bytes, try_json

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_IEND = b"IEND\xaeB`\x82"
#: Android system property names: `ro.build.version.sdk`, `qemu.gles`, ...
_PROP_RE = re.compile(r"[A-Za-z0-9_.\-]+")


def _is_png(data: bytes) -> bool:
    """A frame is only a frame if it is a complete PNG.

    `adb exec-out` happily returns a truncated buffer (or adb's own error
    text) with exit code 0, and the old one-line `and`/`or` chain was both
    precedence-fragile and accepted any 1 KB blob starting with the magic —
    so a half-written frame could reach the browser and render as a broken
    image instead of falling through to the next capture tier.
    """
    if not data or len(data) < 1024:
        return False
    if not data.startswith(PNG_MAGIC):
        return False
    # A well-formed PNG ends with the IEND chunk; tolerate trailing bytes
    # (some adb builds append a newline) but require the marker to exist.
    return PNG_IEND in data[-16:]


def _check(paths: ToolPaths, serial: str) -> dict | None:
    if not paths.adb:
        return {"ok": False, "err": "adb not found"}
    if not serial:
        return {"ok": False, "err": "no device selected — Quickboot an AVD first"}
    # `serial` arrives straight from a dashboard JSON body. Even though
    # subprocess uses shell=False, an arbitrary string after `-s` can be read
    # as an adb option or a different target; validate at the lowest layer so
    # every ADB wrapper inherits the same rule.
    if not safe.valid_serial(serial):
        return {"ok": False, "err": "invalid device serial"}
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


#: (short key used by the dashboard, full Android property name)
DEVICE_PROPS: tuple[tuple[str, str], ...] = (
    ("model", "ro.product.model"),
    ("android", "ro.build.version.release"),
    ("sdk", "ro.build.version.sdk"),
    ("abi", "ro.product.cpu.abi"),
    ("abilist", "ro.product.cpu.abilist"),
    ("bridge", "ro.dalvik.vm.native.bridge"),
)


def getprops(paths: ToolPaths, serial: str, props: list[str] | tuple[str, ...]) -> dict:
    """Read many system properties in ONE adb round-trip.

    Each `adb shell` costs ~150 ms; reading six properties the old way spent
    ~900 ms per device, and with a 4-instance fleet open that was nearly four
    seconds of the dashboard doing nothing. One shell invocation that walks
    the list is ~150 ms regardless of how many we ask for.

    Prints `key=value` per line rather than bare values: an unset property
    emits an empty line, and positional zip() then shifts every later field
    by one — which is how `sdk` used to be read as `abi`.
    """
    if not paths.adb or not serial or not props:
        return {}
    if not safe.valid_serial(serial):
        return {}
    # v4 interpolated the property names raw into a `for p in ...; do` loop.
    # Every current caller uses a hardcoded list, but that makes the safety of
    # this function depend on all future callers remembering the rule. Only
    # Android property syntax is legal here, and each word is POSIX-quoted so
    # an accidental `;`, backtick or glob can never become device shell code.
    props = [str(p) for p in props[:64]]
    if any(not _PROP_RE.fullmatch(p) for p in props):
        return {}
    quoted = " ".join(safe.sh_quote(p) for p in props)
    script = f'for p in {quoted}; do v=$(getprop "$p"); echo "$p=$v"; done'
    r = run([paths.adb, "-s", serial, "shell", script], timeout=25)
    out: dict = {}
    for line in r["out"].splitlines():
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k in props and v:
            out[k] = v
    return out


def device_props(paths: ToolPaths, serial: str) -> dict:
    raw = getprops(paths, serial, [p for _, p in DEVICE_PROPS])
    return {short: raw[full] for short, full in DEVICE_PROPS if raw.get(full)}


# --------------------------------------------------------------------------
# APK install
# --------------------------------------------------------------------------
def install_apk(paths: ToolPaths, serial: str, apk_path: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    # v4 accepted ANY host path, so a remote dashboard could probe for the
    # existence of arbitrary files and read the absolute path back in `err`.
    p, err = safe.apk_path(apk_path)
    if err:
        return {"ok": False, "err": err}
    if paths.android_cli:
        r = run([paths.android_cli, "install", "--device", serial,
                 "--apks", str(p)], timeout=300)
        if r["ok"]:
            return {"ok": True, "tool": "android-install(delta)", "log": r["out"][-1500:]}
    r = run([paths.adb, "-s", serial, "install", "-r", "-g", str(p)], timeout=300)
    return {"ok": r["ok"], "tool": "adb", "log": (r["out"] + r["err"])[-1500:],
            "note": ("Installed via package manager (-g = all runtime permissions "
                     "granted); no 'unknown sources' prompt needed.")}


def install_apk_all(paths: ToolPaths, apk_path: str) -> dict:
    """Install one APK on EVERY online emulator, in parallel.

    Multi-instance farming and QA fleets want "same build everywhere", not
    eight rounds of pick-device → install. Serial installs made a 5-instance
    fleet wait ~5 × install-time; parallel brings it to one install-time.
    """
    if not paths.adb:
        return {"ok": False, "err": "adb not found"}
    p, err = safe.apk_path(apk_path)
    if err:
        return {"ok": False, "err": err}
    r = run([paths.adb, "devices"], timeout=15)
    serials = [parts[0] for parts in (l.split() for l in r["out"].splitlines())
               if len(parts) >= 2 and parts[1] == "device"
               and parts[0].startswith("emulator-")]
    if not serials:
        return {"ok": False, "err": "no online emulator devices — Quickboot one first"}

    def _one(s: str) -> dict:
        res = install_apk(paths, s, str(p))
        return {"serial": s, "ok": bool(res.get("ok")),
                "err": res.get("err", "") or ("" if res.get("ok") else res.get("log", "")[-200:])}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(serials))) as pool:
        results = list(pool.map(_one, serials))
    good = [x["serial"] for x in results if x["ok"]]
    return {"ok": len(good) == len(results), "installed": good,
            "failed": [x for x in results if not x["ok"]],
            "count": len(good), "total": len(results), "parallel": True}


# --------------------------------------------------------------------------
# Screenshots
# --------------------------------------------------------------------------
def screenshot_bytes(paths: ToolPaths, serial: str) -> dict:
    """Returns {ok, png} with RAW PNG bytes. Three tiers, fastest first.

      1. `adb exec-out screencap -p`  — 1 process, no temp file, binary-safe.
      2. `screencap` to /sdcard + `pull` — works on ancient adb.
      3. `android screen capture` CLI — last resort.

    The MJPEG stream and /api/screen consume this directly: base64-encoding a
    500 KB frame just so the server can immediately decode it again costs
    ~8 ms CPU per frame, which at 10 fps is real money on the frame budget.
    """
    if (e := _check(paths, serial)):
        return e

    rb = run_bytes([paths.adb, "-s", serial, "exec-out", "screencap", "-p"], timeout=25)
    data = rb["out"] or b""
    if _is_png(data):
        return {"ok": True, "png": data, "bytes": len(data), "via": "exec-out"}

    # v4 used the FIXED path /sdcard/s.png. Two concurrent captures (a live
    # MJPEG stream plus a one-off screenshot, or two browser tabs) then pulled
    # each other's half-written file and left stale PNGs on the device forever.
    # A per-call name removes the race and `finally` always cleans up.
    remote = f"/sdcard/nd-shot-{os.getpid()}-{time.time_ns()}.png"
    try:
        with tempfile.TemporaryDirectory() as td:
            fp = os.path.join(td, "s.png")
            r1 = run([paths.adb, "-s", serial, "shell", "screencap", "-p", remote],
                     timeout=30)
            if r1["ok"]:
                run([paths.adb, "-s", serial, "pull", remote, fp], timeout=30)
            if os.path.exists(fp):
                raw = Path(fp).read_bytes()
                if _is_png(raw):
                    return {"ok": True, "png": raw, "bytes": len(raw),
                            "via": "screencap+pull"}
    finally:
        run([paths.adb, "-s", serial, "shell", "rm", "-f", remote], timeout=15)

    if paths.android_cli:
        with tempfile.TemporaryDirectory() as td:
            fp = os.path.join(td, "shot.png")
            run([paths.android_cli, "screen", "capture", "--device", serial, fp],
                timeout=30)
            if os.path.exists(fp):
                raw = Path(fp).read_bytes()
                if _is_png(raw):
                    return {"ok": True, "png": raw, "bytes": len(raw), "via": "android-cli"}
    # `rb["err"]` can contain adb's own diagnostics (device offline, permission
    # denied, protocol error). Keep them — they are useful — but never let a
    # raw subprocess message of unbounded length reach the dashboard.
    return {"ok": False,
            "err": (rb["err"] or "screencap produced no PNG "
                    "(device offline or capture blocked)")[-200:]}


def screenshot(paths: ToolPaths, serial: str) -> dict:
    """Returns {ok, png_b64} — the JSON-safe wrapper over screenshot_bytes."""
    r = screenshot_bytes(paths, serial)
    if not r.get("ok"):
        return r
    return {"ok": True, "png_b64": base64.b64encode(r["png"]).decode(),
            "bytes": r["bytes"], "via": r["via"]}


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
    except OverflowError:
        # json accepts bare Infinity/NaN and int(float("inf")) raises
        # OverflowError — one uncaught exception turned a tap into a 500.
        # Infinity clamps to the bound it is reaching for.
        return hi if float(value) > 0 else lo


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
    """Type text, preserving spaces and apostrophes.

    `input text` runs through the device shell — adb joins argv with spaces
    and hands the whole line to `/system/bin/sh -c` — so the payload MUST be
    wrapped in double quotes with the characters the shell re-interprets
    inside them ($ ` \\ ") escaped. The old version replaced spaces with %s
    and DELETED apostrophes outright; a v4 interim build escaped the right
    four characters but never added the quotes, so `x; reboot` executed as
    two shell commands. `input` itself does exactly one substitution on the
    final argument (`%s` -> space; verified in AOSP `Input.sendText`), so a
    literal % must NOT be doubled — `50%% done` used to arrive typed as
    "50%% done".
    """
    if (e := _check(paths, serial)):
        return e
    text = str(text or "")
    if not text:
        return {"ok": False, "err": "no text given"}
    # A newline/NUL inside a double-quoted remote word is the one case the
    # escaping below cannot make safe: adb joins argv and hands the string to
    # sh -c, where an embedded control char can terminate the command. Refuse
    # control characters instead of trying to escape them.
    if safe.has_control(text):
        return {"ok": False, "err": "text contains control characters"}
    if len(text) > 400:
        text = text[:400]
    # Inside double quotes only \\, ", $ and ` are re-interpreted by sh; `;`,
    # `|`, `&`, parens and redirects stay literal. Escaping those four plus
    # the surrounding quotes is therefore complete.
    payload = '"' + (text.replace("\\", "\\\\").replace("$", "\\$")
                     .replace("`", "\\`").replace('"', '\\"')) + '"'
    return run([paths.adb, "-s", serial, "shell", "input", "text", payload],
               timeout=20)


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
    """Arbitrary adb shell. OPT-IN ONLY.

    The v4 blacklist was never a security boundary — `sh -c` has hundreds of
    ways to express the same destructive command, and an authenticated
    operator is already trusted with `install`, `wipe` and `shell`. The real
    control is the router: `/api/shell` is disabled unless the agent was
    started with `--allow-shell`. The blacklist stays as seat-belt-only.
    """
    if (e := _check(paths, serial)):
        return e
    cmd = str(cmd or "")
    if not cmd or len(cmd) > 2000 or safe.has_control(cmd):
        return {"ok": False, "err": "invalid shell command"}
    banned = ["reboot -p", "rm -rf /", "mkfs", "dd if=", ":(){:|:&};:",
              "wipe", "format ", "fastboot", "rm -rf /sdcard", "> /dev/block"]
    low = cmd.lower()
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


#: Windows reserves these device names with any extension (pre-Win11 and
#: NTFS long-path edge cases); writing "CON.json" can fail or poke a device.
_MACRO_RESERVED = {"con", "prn", "aux", "nul",
                   *(f"com{i}" for i in range(1, 10)),
                   *(f"lpt{i}" for i in range(1, 10))}


def macro_save(name: str, steps: list) -> dict:
    safe = _safe_macro_name(name) or "macro"
    if safe.lower() in _MACRO_RESERVED:
        return {"ok": False, "err": f"'{safe}' is a reserved Windows name — pick another"}
    good, bad = _validate_steps(steps)
    fp = MACRO_DIR / f"{safe}.json"
    try:
        fp.write_text(json.dumps({"name": safe, "steps": good}, indent=1),
                      encoding="utf-8")
    except OSError as e:
        return {"ok": False, "err": f"could not save macro: {e}"}
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


def macro_run(paths: ToolPaths, serial: str, name: str,
              loops: int = 1, loop_delay_s: float = 1.0) -> dict:
    """Replay tap/swipe/text/key/sleep steps via adb input.

    `loops` repeats the whole macro N times (LDPlayer-style repeat) with
    `loop_delay_s` between passes — the standard shape of a farm choreography
    ("do the daily, wait, do it again on the next instance").
    """
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
    loops = max(1, min(int(loops or 1), 500))
    loop_delay_s = max(0.0, min(float(loop_delay_s or 0.0), 300.0))
    log: list[str] = []
    ok_steps = 0
    for run_i in range(loops):
        if run_i and loop_delay_s:
            time.sleep(loop_delay_s)
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
            if r.get("ok"):
                ok_steps += 1
            log.append(f"{run_i}:{i}:{t} {'ok' if r.get('ok') else 'FAIL'}")
    return {"ok": True, "log": log[-500:], "macro": safe, "loops": loops,
            "steps_ok": ok_steps}
