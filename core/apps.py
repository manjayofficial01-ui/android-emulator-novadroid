"""Device control that stock `adb` makes tedious: apps, logcat, clipboard,
screen recording, sensor/network/biometric injection, boot tracking.

Everything speaks to the emulator console (`adb emu ...`) or to the device
shell, so no guest app or root is required.

Note on quoting: `adb shell a b c` joins the arguments with spaces and hands
the result to `/system/bin/sh -c`. That means a single string argument gives
us full control of the remote shell — pipes, redirection and quoting all
work — which is why the commands below are built as one string via `_sh()`.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

from .config import ToolPaths
from .runner import run

# Anything that could break out into a second remote command is refused.
_SHELL_DENY = re.compile(r"[;&|`$]|&&|\|\||\n|\r")


def _sh(paths: ToolPaths, serial: str, script: str, timeout: int = 30) -> dict:
    """Run `script` through the device shell as one argv element."""
    if not paths.adb:
        return {"ok": False, "err": "adb not found"}
    if not serial:
        return {"ok": False, "err": "no device selected — Quickboot an AVD first"}
    return run([paths.adb, "-s", serial, "shell", script], timeout=timeout)


def _safe(text: str, limit: int = 500) -> str | None:
    """Return the text if it is safe to interpolate into a shell string."""
    if text is None:
        return None
    text = str(text)[:limit]
    return None if _SHELL_DENY.search(text) else text


def _dq(text: str) -> str:
    """Escape for a double-quoted remote shell word."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


def _check(paths: ToolPaths, serial: str) -> dict | None:
    if not paths.adb:
        return {"ok": False, "err": "adb not found"}
    if not serial:
        return {"ok": False, "err": "no device selected — Quickboot an AVD first"}
    return None


# --------------------------------------------------------------------------
# Boot state — the difference between "command returned" and "is usable"
# --------------------------------------------------------------------------
def boot_state(paths: ToolPaths, serial: str) -> dict:
    """Real boot readiness. `adb devices` says 'device' far too early."""
    if not serial or not paths.adb:
        return {"ok": False, "booted": False, "err": "no device selected"}
    r = run([paths.adb, "-s", serial, "shell",
             "getprop sys.boot_completed; getprop dev.bootcomplete; "
             "getprop init.svc.bootanim; getprop sys.usb.state"], timeout=20)
    # Positional read: an unset property emits an EMPTY line, and dropping
    # those shifted every later field by one (sys.usb.state was being read as
    # bootanim, so bootanim_stopped was never true and wait_boot timed out).
    vals = [v.strip() for v in r["out"].splitlines()]
    while len(vals) < 4:
        vals.append("")
    # sys.boot_completed is the modern signal; dev.bootcomplete is the fallback
    # some images set first. Either one means the framework is up.
    booted = bool(r["ok"]) and (vals[0] == "1" or vals[1] == "1")
    anim = vals[2]
    # An unset init.svc.bootanim means the animation service already exited.
    stopped = anim in ("", "stopped")
    return {
        "ok": bool(r["ok"]),
        "booted": bool(booted),
        "bootanim": anim,
        "bootanim_stopped": stopped,
        "usb": vals[3],
        "ready": bool(booted) and stopped,
        "raw": " | ".join(vals)[:300],
        "note": ("boot_completed=1 — safe to install and interact"
                 if booted else "still booting (boot_completed not set)"),
    }


def wait_boot(paths: ToolPaths, serial: str, timeout: int = 300) -> dict:
    """Block until sys.boot_completed=1. Used by start_avd to report truth."""
    deadline = time.time() + max(5, min(int(timeout), 900))
    seen_online = False
    while time.time() < deadline:
        st = boot_state(paths, serial)
        if st.get("ok"):
            seen_online = True
            if st.get("ready"):
                return {"ok": True, "booted": True, "waited_s": 0}
        time.sleep(2)
    return {"ok": False, "booted": False,
            "err": f"boot did not complete within {timeout}s"
                   + ("" if seen_online else " (device never came online)")}


# --------------------------------------------------------------------------
# Packages / apps
# --------------------------------------------------------------------------
def _parse_package_lines(raw: str) -> list[dict]:
    """Parse `pm list packages -f -U` output.

    Shapes seen in the wild:
        package:/data/app/~~abcd==/com.foo-1/base.apk=com.foo
        package:/data/app/.../base.apk=com.foo versionCode:12345
        package:com.foo
    """
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        body = line[len("package:"):]
        version = None
        m = re.search(r"versionCode:(\d+)", body)
        if m:
            version = int(m.group(1))
            body = body[:m.start()].strip()
        # "path=pkg" or bare "pkg"
        if "=" in body:
            apk, pkg = body.rsplit("=", 1)
        else:
            apk, pkg = "", body
        if pkg:
            out.append({"package": pkg, "apk": apk, "versionCode": version})
    return out


def list_packages(paths: ToolPaths, serial: str, third_party: bool = True) -> dict:
    """Installed packages. Third-party by default (that is what you manage)."""
    if (e := _check(paths, serial)):
        return e
    flag = "-3" if third_party else ""
    cmd = ["-s", serial, "shell", f"pm list packages {flag} -f -U".strip()]
    r = run([paths.adb] + cmd, timeout=60)
    items = _parse_package_lines(r["out"])
    if not items:  # -U needs a newer platform; retry without it
        r = run([paths.adb, "-s", serial, "shell", f"pm list packages {flag} -f".strip()],
                timeout=60)
        items = _parse_package_lines(r["out"])
    items.sort(key=lambda d: d["package"].lower())
    return {"ok": bool(r["ok"]), "packages": items, "count": len(items),
            "third_party": third_party}


def package_info(paths: ToolPaths, serial: str, package: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    pkg = _safe(package, 200)
    if not pkg:
        return {"ok": False, "err": "invalid package name"}
    r = run([paths.adb, "-s", serial, "shell",
             f"dumpsys package {pkg} | head -40"], timeout=40)
    text = r["out"]
    info: dict = {"package": pkg, "raw": text[:2500]}
    m = re.search(r"versionCode=(\d+)", text)
    if m:
        info["versionCode"] = int(m.group(1))
    m = re.search(r"versionName=([^\s]+)", text)
    if m:
        info["versionName"] = m.group(1)
    m = re.search(r"firstInstallTime=(\S+)", text)
    if m:
        info["installed"] = m.group(1)
    m = re.search(r"targetSdk=(\d+)", text)
    if m:
        info["targetSdk"] = int(m.group(1))
    return {"ok": bool(r["ok"]), "info": info}


def launch(paths: ToolPaths, serial: str, package: str) -> dict:
    """Open an app the way a launcher icon would.

    `monkey` is the universal fallback, but it can resolve to a non-default
    entry point; `cmd package resolve-activity` gives the real launcher
    activity first, so deep-linked/profile apps still open correctly.
    """
    if (e := _check(paths, serial)):
        return e
    pkg = _safe(package, 200)
    if not pkg:
        return {"ok": False, "err": "invalid package name"}
    r = run([paths.adb, "-s", serial, "shell",
             f"cmd package resolve-activity --brief {pkg}"], timeout=30)
    activity = ""
    for line in r["out"].splitlines():
        line = line.strip()
        if "/" in line and line.startswith(pkg):
            activity = line.split("/", 1)[1]
            break
    if activity:
        res = run([paths.adb, "-s", serial, "shell",
                   f"am start -n {pkg}/{activity}"], timeout=30)
        if res["ok"]:
            return {"ok": True, "via": "am start", "activity": activity,
                    "log": res["out"][:400]}
    res = run([paths.adb, "-s", serial, "shell",
               f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1"], timeout=40)
    return {"ok": bool(res["ok"]), "via": "monkey", "package": pkg,
            "log": (res["out"] + res["err"])[-400:]}


def stop_app(paths: ToolPaths, serial: str, package: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    pkg = _safe(package, 200)
    if not pkg:
        return {"ok": False, "err": "invalid package name"}
    r = run([paths.adb, "-s", serial, "shell", f"am force-stop {pkg}"], timeout=30)
    return {"ok": bool(r["ok"]), "package": pkg, "log": (r["out"] + r["err"])[-300:]}


def clear_app(paths: ToolPaths, serial: str, package: str) -> dict:
    """Wipe an app's data (keeps the APK installed)."""
    if (e := _check(paths, serial)):
        return e
    pkg = _safe(package, 200)
    if not pkg:
        return {"ok": False, "err": "invalid package name"}
    r = run([paths.adb, "-s", serial, "shell", f"pm clear {pkg}"], timeout=60)
    return {"ok": bool(r["ok"]), "package": pkg,
            "log": (r["out"] or "Success")[-200:]}


def uninstall(paths: ToolPaths, serial: str, package: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    pkg = _safe(package, 200)
    if not pkg:
        return {"ok": False, "err": "invalid package name"}
    r = run([paths.adb, "-s", serial, "uninstall", pkg], timeout=120)
    if r["ok"]:
        return {"ok": True, "package": pkg, "log": r["out"][-200:]}
    # Pre-installed app: uninstall for the current user only.
    r2 = run([paths.adb, "-s", serial, "shell", f"pm uninstall --user 0 {pkg}"],
             timeout=120)
    return {"ok": bool(r2["ok"]), "package": pkg, "via": "pm uninstall --user 0",
            "log": (r2["out"] + r2["err"])[-300:]}


def grant_all(paths: ToolPaths, serial: str, package: str) -> dict:
    """Grant every runtime permission (kills first-run dialogs in QA)."""
    if (e := _check(paths, serial)):
        return e
    pkg = _safe(package, 200)
    if not pkg:
        return {"ok": False, "err": "invalid package name"}
    r = run([paths.adb, "-s", serial, "shell",
             f"pm grant {pkg} android.permission.CAMERA"], timeout=30)
    perms = run([paths.adb, "-s", serial, "shell",
                 f"dumpsys package {pkg} | grep -o 'android.permission.[A-Z_.]*' | sort -u"],
                timeout=40)
    granted: list[str] = []
    for p in dict.fromkeys(perms["out"].split()):
        if not p.startswith("android.permission."):
            continue
        res = run([paths.adb, "-s", serial, "shell", f"pm grant {pkg} {p}"], timeout=20)
        if res["ok"]:
            granted.append(p)
    return {"ok": True, "package": pkg, "granted": granted,
            "count": len(granted), "probe": r["ok"]}


# --------------------------------------------------------------------------
# Logcat
# --------------------------------------------------------------------------
LOGCAT_LEVELS = ("V", "D", "I", "W", "E", "F", "S")


def logcat(paths: ToolPaths, serial: str, lines: int = 200,
           spec: str = "", since: str = "") -> dict:
    """Snapshot logcat. `spec` is a raw filter spec like 'ActivityManager:I *:S'."""
    if (e := _check(paths, serial)):
        return e
    n = max(20, min(int(lines or 200), 5000))
    spec = _safe(spec, 200) or ""
    if spec and not re.fullmatch(r"[A-Za-z0-9_.*:\s]+", spec):
        return {"ok": False, "err": "filter spec must look like 'Tag:I *:S'"}
    args = ["-v", "time", "-d", "-t", str(n)]
    if since:
        args += ["-T", _safe(since, 40) or ""]
        args = [a for a in args if a]
    cmd = "logcat " + " ".join(args) + (f" {spec}" if spec else "")
    r = run([paths.adb, "-s", serial, "shell", cmd], timeout=45)
    return {"ok": bool(r["ok"]), "lines": r["out"].splitlines()[-n:],
            "count": len(r["out"].splitlines()), "spec": spec or "*:*",
            "raw": r["out"][-8000:], "err": r["err"][-300:]}


def logcat_clear(paths: ToolPaths, serial: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    r = run([paths.adb, "-s", serial, "logcat", "-c"], timeout=20)
    return {"ok": bool(r["ok"]), "log": (r["out"] + r["err"])[-200:]}


# --------------------------------------------------------------------------
# Clipboard (PC <-> device)
# --------------------------------------------------------------------------
# Some system images register the `clipboard` service but ship no shell
# command for it — `cmd clipboard get` then prints "No shell command
# implementation." and still EXITS 0. Treating that as success is how a
# feature silently does nothing, so it is detected explicitly.
_CLIP_UNSUPPORTED = ("no shell command implementation", "unknown command",
                     "no such service", "not found")
_clip_support: dict[str, bool] = {}


def _clipboard_ok(r: dict) -> bool:
    blob = ((r.get("out") or "") + (r.get("err") or "")).lower()
    return r.get("ok", False) and not any(s in blob for s in _CLIP_UNSUPPORTED)


def clipboard_get(paths: ToolPaths, serial: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    r = _sh(paths, serial, "cmd clipboard get", timeout=20)
    if not _clipboard_ok(r):
        _clip_support[serial] = False
        blob = (r.get("out") or r.get("err") or "").strip()
        return {"ok": False, "text": "", "supported": False,
                "err": f"clipboard console unavailable on this image ({blob[:80] or 'no shell command'}). "
                       "Use 'Send' — it types into the focused field via `input text`."}
    _clip_support[serial] = True
    text = r["out"]
    if text in ("", "null", "NULL"):
        return {"ok": True, "text": "", "supported": True, "note": "clipboard is empty"}
    if len(text) > 20000:
        text = text[:20000] + "\n… (truncated)"
    return {"ok": True, "text": text, "supported": True}


def clipboard_set(paths: ToolPaths, serial: str, text: str) -> dict:
    """Push text to the device clipboard.

    `cmd clipboard set` takes the payload as shell words, so it goes inside
    double quotes with $, \\ and " escaped — spaces and emoji survive.
    """
    if (e := _check(paths, serial)):
        return e
    if text is None:
        return {"ok": False, "err": "no text given"}
    text = str(text)
    if len(text) > 100_000:
        return {"ok": False, "err": "clipboard payload too large (100 KB cap)"}
    r = _sh(paths, serial, f'cmd clipboard set "{_dq(text)}"', timeout=25)
    if not _clipboard_ok(r):
        _clip_support[serial] = False
        blob = (r.get("out") or r.get("err") or "").strip()
        return {"ok": False, "supported": False,
                "err": f"clipboard console unavailable on this image ({blob[:80] or 'no shell command'}). "
                       "Use 'Send' — it types into the focused field via `input text`."}
    _clip_support[serial] = True
    return {"ok": True, "chars": len(text), "supported": True}


# --------------------------------------------------------------------------
# Screen recording
# --------------------------------------------------------------------------
REC_DIR = Path.home() / "NovaDroid-Shared" / "Recordings"
_REMOTE = "/sdcard/novadroid-record.mp4"
_REC_JOBS: dict[str, dict] = {}
MAX_RECORD_S = 180  # screenrecord hard limit


def record_start(paths: ToolPaths, serial: str, seconds: int = 30,
                 bitrate_mbps: int = 8) -> dict:
    """Start `screenrecord` in the background and track it.

    screenrecord exits on its own at --time-limit; we keep the Popen so the
    user can stop early (SIGINT makes it finalise the mp4 cleanly, whereas
    SIGKILL leaves a corrupt file).
    """
    if (e := _check(paths, serial)):
        return e
    if _REC_JOBS.get(serial, {}).get("proc") and _REC_JOBS[serial]["proc"].poll() is None:
        return {"ok": False, "err": f"already recording on {serial}"}
    secs = max(1, min(int(seconds or 30), MAX_RECORD_S))
    mbps = max(1, min(int(bitrate_mbps or 8), 50))
    REC_DIR.mkdir(parents=True, exist_ok=True)
    script = (f"screenrecord --time-limit {secs} --bit-rate {mbps * 1000000} "
              f"--verbose {_REMOTE}")
    try:
        proc = subprocess.Popen(
            [paths.adb, "-s", serial, "shell", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as ex:
        return {"ok": False, "err": str(ex)}
    job = {"proc": proc, "t0": time.time(), "seconds": secs, "serial": serial,
           "file": None, "stopping": False}
    _REC_JOBS[serial] = job

    def _finish():
        try:
            proc.wait(timeout=secs + 30)
        except Exception:
            proc.kill()
        run([paths.adb, "-s", serial, "shell", "sync"], timeout=20)
        local = REC_DIR / f"{serial}-{time.strftime('%Y%m%d-%H%M%S')}.mp4"
        r = run([paths.adb, "-s", serial, "pull", _REMOTE, str(local)], timeout=180)
        job["file"] = str(local) if r["ok"] and local.exists() else None
        job["done"] = time.time()
        job["state"] = "done" if job["file"] else "failed"
        job["err"] = "" if job["file"] else (r["err"] or r["out"])[-300:]
        run([paths.adb, "-s", serial, "shell", f"rm -f {_REMOTE}"], timeout=20)

    threading.Thread(target=_finish, daemon=True).start()
    job["state"] = "recording"
    return {"ok": True, "state": "recording", "seconds": secs,
            "dir": str(REC_DIR), "note": f"recording up to {secs}s — Stop early to save now."}


def record_stop(paths: ToolPaths, serial: str) -> dict:
    job = _REC_JOBS.get(serial)
    if not job:
        return {"ok": False, "err": f"no recording running on {serial}"}
    proc = job.get("proc")
    if proc and proc.poll() is None:
        # SIGINT finalises the mp4; a hard kill produces an unplayable file.
        run([paths.adb, "-s", serial, "shell", "pkill -INT screenrecord"], timeout=20)
        try:
            proc.wait(timeout=25)
        except Exception:
            if proc.poll() is None:
                proc.terminate()
    for _ in range(40):   # wait for the pull thread to land the file
        if job.get("state") in ("done", "failed"):
            break
        time.sleep(0.5)
    return {"ok": job.get("state") == "done", "state": job.get("state", "stopping"),
            "file": job.get("file"), "err": job.get("err", "")}


def record_state(serial: str) -> dict:
    job = _REC_JOBS.get(serial)
    if not job:
        return {"ok": True, "state": "idle"}
    proc = job.get("proc")
    elapsed = round(time.time() - job["t0"], 1)
    running = bool(proc and proc.poll() is None)
    return {"ok": True,
            "state": job.get("state", "recording"),
            "running": running,
            "elapsed_s": elapsed,
            "remaining_s": max(0, round(job["seconds"] - elapsed, 1)) if running else 0,
            "file": job.get("file"), "err": job.get("err", "")}


# --------------------------------------------------------------------------
# Emulator console: sensors, network, biometrics, telephony
# --------------------------------------------------------------------------
SENSORS = {
    "acceleration": "Accelerometer (x:y:z m/s², z≈9.81 at rest)",
    "orientation": "Device orientation (azimuth:pitch:roll degrees)",
    "temperature": "Ambient temperature (°C)",
    "proximity": "Proximity (cm, 0 = against face)",
    "light": "Ambient light (lux)",
    "pressure": "Barometric pressure (hPa)",
    "humidity": "Relative humidity (%)",
    "magnetic-field": "Magnetometer (x:y:z µT)",
    "gyroscope": "Gyroscope (x:y:z rad/s)",
}

NET_SPEEDS = ("gsm", "hscsd", "gprs", "edge", "umts", "hspa", "hsdpa",
              "hsupa", "evdo", "lte", "full")
NET_DELAYS = ("none", "gsm", "hscsd", "gprs", "edge", "umts", "hsdpa", "lte", "evdo")


def sensor_set(paths: ToolPaths, serial: str, kind: str, values: list[str]) -> dict:
    """`adb emu sensor set <kind> <v1:v2:v3>` — injects a live sensor reading."""
    if (e := _check(paths, serial)):
        return e
    kind = (kind or "").strip().lower()
    if kind not in SENSORS:
        return {"ok": False, "err": f"unknown sensor; pick one of {sorted(SENSORS)}"}
    vals = [v for v in (values or []) if str(v).strip() != ""]
    if not vals:
        return {"ok": False, "err": "no values given"}
    joined = ":".join(str(v)[:24] for v in vals[:3])
    if _SHELL_DENY.search(joined):
        return {"ok": False, "err": "invalid sensor values"}
    r = run([paths.adb, "-s", serial, "emu", "sensor", "set", kind, joined], timeout=20)
    return {"ok": bool(r["ok"]), "sensor": kind, "value": joined,
            "log": (r["out"] + r["err"])[-300:]}


def sensor_status(paths: ToolPaths, serial: str, kind: str = "") -> dict:
    """Always returns the sensor catalogue so the UI can build its dropdown
    before any device is picked; `raw` is only filled in when one is."""
    out: dict = {"ok": True, "sensors": SENSORS, "hint": "sensor <name> <x:y:z>",
                 "raw": ""}
    if not serial or not paths.adb:
        return out
    r = run([paths.adb, "-s", serial, "emu", "sensor", "status"], timeout=20)
    out["raw"] = (r["out"][-1500:] or r["err"][-300:])
    return out


def network(paths: ToolPaths, serial: str, speed: str = "", delay: str = "") -> dict:
    """Throttle the emulated radio — the #1 missing piece in QA of mobile apps."""
    if (e := _check(paths, serial)):
        return e
    if not speed and not delay:
        return {"ok": False, "err": "give speed and/or delay",
                "speeds": list(NET_SPEEDS), "delays": list(NET_DELAYS)}
    log = {}
    if speed:
        if speed not in NET_SPEEDS:
            return {"ok": False, "err": f"speed must be one of {list(NET_SPEEDS)}"}
        log["speed"] = run([paths.adb, "-s", serial, "emu", "network", "speed", speed],
                           timeout=20)["ok"]
    if delay:
        if delay not in NET_DELAYS:
            return {"ok": False, "err": f"delay must be one of {list(NET_DELAYS)}"}
        log["delay"] = run([paths.adb, "-s", serial, "emu", "network", "delay", delay],
                           timeout=20)["ok"]
    return {"ok": all(log.values()), "applied": log,
            "note": "Applies to the emulated radio only; 'full'/'none' restores."}


def network_status(paths: ToolPaths, serial: str) -> dict:
    """The speed/delay catalogues are static — return them unconditionally."""
    out: dict = {"ok": True, "speeds": list(NET_SPEEDS),
                 "delays": list(NET_DELAYS), "raw": ""}
    if not serial or not paths.adb:
        return out
    sp = run([paths.adb, "-s", serial, "emu", "network", "status"], timeout=20)
    out["raw"] = (sp["out"] or sp["err"])[-800:]
    return out


def fingerprint(paths: ToolPaths, serial: str, touch: bool = True) -> dict:
    """Trigger the enrolled fingerprint sensor (API 24+ emulator images)."""
    if (e := _check(paths, serial)):
        return e
    for attempt in (touch, not touch):
        r = run([paths.adb, "-s", serial, "emu", "finger", "touch",
                 "1" if attempt else "0"], timeout=20)
        if r["ok"]:
            return {"ok": True, "pressed": attempt, "log": (r["out"] or "")[-200:]}
    return {"ok": False, "err": "fingerprint console unavailable on this image"}


def send_sms(paths: ToolPaths, serial: str, number: str, text: str = "") -> dict:
    if (e := _check(paths, serial)):
        return e
    num = _safe(number, 40)
    body = _safe(text, 300)
    if not num or not re.fullmatch(r"[+0-9\s\-]+", num):
        return {"ok": False, "err": "number may only contain digits, + - and spaces"}
    r = run([paths.adb, "-s", serial, "emu", "sms", "send", num,
             body or "NovaDroid test"], timeout=25)
    return {"ok": bool(r["ok"]), "to": num, "log": (r["out"] + r["err"])[-300:]}


def gsm_call(paths: ToolPaths, serial: str, number: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    num = _safe(number, 40)
    if not num or not re.fullmatch(r"[+0-9\s\-]+", num):
        return {"ok": False, "err": "number may only contain digits, + - and spaces"}
    r = run([paths.adb, "-s", serial, "emu", "gsm", "call", num], timeout=25)
    return {"ok": bool(r["ok"]), "number": num, "log": (r["out"] + r["err"])[-300:]}


def gsm_signal(paths: ToolPaths, serial: str, level: int = 4) -> dict:
    """0 = no bars … 4 = full (some images only honour 0/4 as none/good)."""
    if (e := _check(paths, serial)):
        return e
    lvl = max(0, min(int(level), 4))
    r = run([paths.adb, "-s", serial, "emu", "gsm", "signal", str(lvl)], timeout=20)
    return {"ok": bool(r["ok"]), "level": lvl, "log": (r["out"] + r["err"])[-200:]}


def power_toggle(paths: ToolPaths, serial: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    r = run([paths.adb, "-s", serial, "shell", "input", "keyevent", "26"], timeout=20)
    return {"ok": bool(r["ok"]), "log": "sent POWER keyevent"}


def emu_rotate(paths: ToolPaths, serial: str) -> dict:
    """Rotate via the emulator console (hits the sensor, apps react live)."""
    if (e := _check(paths, serial)):
        return e
    r = run([paths.adb, "-s", serial, "emu", "rotate"], timeout=20)
    return {"ok": bool(r["ok"]), "log": (r["out"] + r["err"])[-200:]}
