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

from . import safe
from .config import ToolPaths
from .runner import run

# Anything that could break out into a second remote command is refused.
# v5 also blocks redirects: `> /sdcard/x` needs no shell metacharacter other
# than `>` and was enough to overwrite arbitrary guest files.
_SHELL_DENY = re.compile(r"[;&|`$<>]|&&|\|\||\n|\r|\x00")


def _sh(paths: ToolPaths, serial: str, script: str, timeout: int = 30) -> dict:
    """Run `script` through the device shell as one argv element."""
    if not paths.adb:
        return {"ok": False, "err": "adb not found"}
    if not serial:
        return {"ok": False, "err": "no device selected — Quickboot an AVD first"}
    if not safe.valid_serial(serial):
        return {"ok": False, "err": "invalid device serial"}
    return run([paths.adb, "-s", serial, "shell", script], timeout=timeout)


def _safe(text: str, limit: int = 500) -> str | None:
    """Return the text if it is safe to interpolate into a shell string."""
    if text is None:
        return None
    text = str(text)[:limit]
    return None if _SHELL_DENY.search(text) else text


def _pkg(package) -> str | None:
    """An Android application ID — never an arbitrary shell fragment."""
    p = str(package or "").strip()
    return p if safe.valid_package(p) else None


def _dq(text: str) -> str:
    r"""Escape for a double-quoted remote shell word.

    v4 forgot the backtick: `cmd clipboard set "\`id\`"` executed `id` inside
    the double quotes. Inside `"…"` only \\, ", $ and ` are re-interpreted.
    """
    return (text.replace("\\", "\\\\").replace('"', '\\"')
            .replace("$", "\\$").replace("`", "\\`"))


def _check(paths: ToolPaths, serial: str) -> dict | None:
    if not paths.adb:
        return {"ok": False, "err": "adb not found"}
    if not serial:
        return {"ok": False, "err": "no device selected — Quickboot an AVD first"}
    if not safe.valid_serial(serial):
        return {"ok": False, "err": "invalid device serial"}
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
    t0 = time.time()
    seen_online = False
    while time.time() < deadline:
        st = boot_state(paths, serial)
        if st.get("ok"):
            seen_online = True
            if st.get("ready"):
                return {"ok": True, "booted": True,
                        "waited_s": round(time.time() - t0, 1)}
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
    pkg = _pkg(package)
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
    pkg = _pkg(package)
    if not pkg:
        return {"ok": False, "err": "invalid package name"}
    r = run([paths.adb, "-s", serial, "shell",
             f"cmd package resolve-activity --brief {pkg}"], timeout=30)
    activity = ""
    for line in r["out"].splitlines():
        line = line.strip()
        if "/" in line and line.startswith(pkg):
            cand = line.split("/", 1)[1].strip()
            if re.fullmatch(r"[A-Za-z0-9_.$/]+", cand):
                activity = cand
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
    pkg = _pkg(package)
    if not pkg:
        return {"ok": False, "err": "invalid package name"}
    r = run([paths.adb, "-s", serial, "shell", f"am force-stop {pkg}"], timeout=30)
    return {"ok": bool(r["ok"]), "package": pkg, "log": (r["out"] + r["err"])[-300:]}


def clear_app(paths: ToolPaths, serial: str, package: str) -> dict:
    """Wipe an app's data (keeps the APK installed)."""
    if (e := _check(paths, serial)):
        return e
    pkg = _pkg(package)
    if not pkg:
        return {"ok": False, "err": "invalid package name"}
    r = run([paths.adb, "-s", serial, "shell", f"pm clear {pkg}"], timeout=60)
    return {"ok": bool(r["ok"]), "package": pkg,
            "log": (r["out"] or "Success")[-200:]}


def uninstall(paths: ToolPaths, serial: str, package: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    pkg = _pkg(package)
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
    """Grant every runtime permission (kills first-run dialogs in QA).

    One shell loop, not one adb process per permission: a navigation app
    requests ~35 permissions, and 35 sequential `adb shell pm grant` calls
    took ~7 seconds and felt like a hang. The loop is a single round-trip.
    """
    if (e := _check(paths, serial)):
        return e
    pkg = _pkg(package)
    if not pkg:
        return {"ok": False, "err": "invalid package name"}
    listed = run([paths.adb, "-s", serial, "shell",
                  f"dumpsys package {pkg} | grep -o 'android.permission.[A-Z_.]*' "
                  f"| sort -u"], timeout=40)
    wanted = [p for p in dict.fromkeys(listed["out"].split())
              if p.startswith("android.permission.")]
    if not wanted:
        return {"ok": False, "package": pkg, "granted": [], "count": 0,
                "err": "no runtime permissions found (is the package installed?)"}
    # Grant, then echo back only the ones that actually stuck.
    script = ("for p in " + " ".join(wanted) + "; do "
              f'pm grant {pkg} "$p" >/dev/null 2>&1 && echo "$p"; done')
    r = run([paths.adb, "-s", serial, "shell", script], timeout=90)
    granted = [line.strip() for line in r["out"].splitlines()
               if line.strip().startswith("android.permission.")]
    return {"ok": bool(granted), "package": pkg, "granted": granted,
            "count": len(granted), "requested": len(wanted),
            "note": f"{len(granted)}/{len(wanted)} permissions granted"}


# --------------------------------------------------------------------------
# Logcat
# --------------------------------------------------------------------------
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
def _clipboard_ok(r: dict) -> bool:
    blob = ((r.get("out") or "") + (r.get("err") or "")).lower()
    return r.get("ok", False) and not any(s in blob for s in _CLIP_UNSUPPORTED)


def clipboard_get(paths: ToolPaths, serial: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    r = _sh(paths, serial, "cmd clipboard get", timeout=20)
    if not _clipboard_ok(r):
        blob = (r.get("out") or r.get("err") or "").strip()
        return {"ok": False, "text": "", "supported": False,
                "err": f"clipboard console unavailable on this image ({blob[:80] or 'no shell command'}). "
                       "Use 'Send' — it types into the focused field via `input text`."}
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
    if safe.has_text_control(text):
        return {"ok": False, "err": "clipboard text contains control characters"}
    r = _sh(paths, serial, f'cmd clipboard set "{_dq(text)}"', timeout=25)
    if not _clipboard_ok(r):
        blob = (r.get("out") or r.get("err") or "").strip()
        return {"ok": False, "supported": False,
                "err": f"clipboard console unavailable on this image ({blob[:80] or 'no shell command'}). "
                       "Use 'Send' — it types into the focused field via `input text`."}
    return {"ok": True, "chars": len(text), "supported": True}


# --------------------------------------------------------------------------
# Screen recording
# --------------------------------------------------------------------------
REC_DIR = Path.home() / "NovaDroid-Shared" / "Recordings"
_REMOTE = "/sdcard/novadroid-record.mp4"
_REC_JOBS: dict[str, dict] = {}
_REC_JOBS_LOCK = threading.RLock()
MAX_RECORD_S = 180  # screenrecord hard limit


def _rec_set_state(job: dict, state: str, err: str = "") -> None:
    """Always transition through a terminal state, even on a worker crash."""
    with _REC_JOBS_LOCK:
        job["state"] = state
        job["err"] = err
        job["done"] = time.time()


def record_start(paths: ToolPaths, serial: str, seconds: int = 30,
                 bitrate_mbps: int = 8) -> dict:
    """Start `screenrecord` in the background and track it.

    screenrecord exits on its own at --time-limit; we keep the Popen so the
    user can stop early (SIGINT makes it finalise the mp4 cleanly, whereas
    SIGKILL leaves a corrupt file).

    v5: every `_REC_JOBS` access is locked, the job is marked "recording"
    BEFORE the worker thread starts (previously the worker could finish first
    and then be overwritten back to "recording" forever), and the worker has
    a `finally` so a crash can never leave `record_stop` waiting 20 s for a
    terminal state that never arrives.
    """
    if (e := _check(paths, serial)):
        return e
    with _REC_JOBS_LOCK:
        old = _REC_JOBS.get(serial)
        if old and old.get("proc") and old["proc"].poll() is None:
            return {"ok": False, "err": f"already recording on {serial}"}
    try:
        secs = max(1, min(int(seconds or 30), MAX_RECORD_S))
        mbps = max(1, min(int(bitrate_mbps or 8), 50))
    except (TypeError, ValueError):
        return {"ok": False, "err": "recording duration/bitrate must be numbers"}
    try:
        REC_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {"ok": False, "err": "could not create the Recordings folder"}
    script = (f"screenrecord --time-limit {secs} --bit-rate {mbps * 1000000} "
              f"--verbose {_REMOTE}")
    try:
        proc = subprocess.Popen(
            [paths.adb, "-s", serial, "shell", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        # Never echo str(ex): it can contain a full local executable path.
        return {"ok": False, "err": "could not start screenrecord (adb launch failed)"}
    job = {"proc": proc, "t0": time.time(), "seconds": secs, "serial": serial,
           "file": None, "stopping": False, "state": "recording", "err": ""}

    def _finish():
        try:
            try:
                proc.wait(timeout=secs + 30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            run([paths.adb, "-s", serial, "shell", "sync"], timeout=20)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            local = REC_DIR / f"{serial}-{stamp}.mp4"
            r = run([paths.adb, "-s", serial, "pull", _REMOTE, str(local)],
                    timeout=180)
            job["file"] = str(local) if r["ok"] and local.exists() else None
            _rec_set_state(job, "done" if job["file"] else "failed",
                           "" if job["file"] else "could not pull the recording")
        except Exception as ex:
            # Log for the operator, return a generic message to the client.
            print(f"[record] worker failed ({type(ex).__name__}): {ex}")
            _rec_set_state(job, "failed", "recording transfer failed")
        finally:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                run([paths.adb, "-s", serial, "shell", f"rm -f {_REMOTE}"],
                    timeout=20)
            except Exception:
                pass

    with _REC_JOBS_LOCK:
        _REC_JOBS[serial] = job
    threading.Thread(target=_finish, daemon=True,
                     name=f"nova-record-{serial}").start()
    return {"ok": True, "state": "recording", "seconds": secs,
            "note": f"recording up to {secs}s — Stop early to save now."}


def record_stop(paths: ToolPaths, serial: str) -> dict:
    with _REC_JOBS_LOCK:
        job = _REC_JOBS.get(serial)
    if not job:
        return {"ok": False, "err": f"no recording running on {serial}"}
    proc = job.get("proc")
    if proc and proc.poll() is None:
        # SIGINT finalises the mp4; a hard kill produces an unplayable file.
        # `pkill` is not on every image (older toybox), so fall back to
        # pidof + kill rather than silently leaving the recording running.
        r = run([paths.adb, "-s", serial, "shell", "pkill -INT screenrecord"],
                timeout=20)
        if not r["ok"]:
            run([paths.adb, "-s", serial, "shell",
                 "kill -INT $(pidof screenrecord) 2>/dev/null"], timeout=20)
        try:
            proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            if proc.poll() is None:
                proc.terminate()
    for _ in range(40):   # wait for the pull thread to land the file
        with _REC_JOBS_LOCK:
            state = job.get("state")
            if state in ("done", "failed"):
                break
        time.sleep(0.5)
    with _REC_JOBS_LOCK:
        return {"ok": job.get("state") == "done",
                "state": job.get("state", "stopping"),
                "file": job.get("file"), "err": job.get("err", "")}


def record_state(serial: str) -> dict:
    with _REC_JOBS_LOCK:
        job = _REC_JOBS.get(serial)
        if not job:
            return {"ok": True, "state": "idle"}
        # Snapshot under the lock: the worker mutates this dict concurrently.
        proc = job.get("proc")
        state = job.get("state", "recording")
        err = job.get("err", "")
        file = job.get("file")
        t0, secs = job["t0"], job["seconds"]
    elapsed = round(time.time() - t0, 1)
    running = bool(proc and proc.poll() is None) and state == "recording"
    return {"ok": True, "state": state, "running": running,
            "elapsed_s": elapsed,
            "remaining_s": max(0, round(secs - elapsed, 1)) if running else 0,
            "file": file, "err": err}


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
    joined = ":".join(str(v).strip()[:24] for v in vals[:3])
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
    # A message the filter refused must be an error, not a silently swapped
    # "NovaDroid test" the sender never wrote.
    if text and not body:
        return {"ok": False,
                "err": "message contains characters the emulator console refuses"}
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


# --------------------------------------------------------------------------
# v4.0.0 — deep links, proxy, eco mode, wireless ADB
# --------------------------------------------------------------------------
def open_url(paths: ToolPaths, serial: str, url: str) -> dict:
    """Open a deep link / URL on the device the way a browser link would.

    `am start -a VIEW -d <uri>` resolves through the same intent machinery a
    tapped link uses, so http(s), app deep links (myapp://...) and Play Store
    (market://details?id=...) all work.
    """
    if (e := _check(paths, serial)):
        return e
    url = str(url or "").strip()
    if safe.has_control(url) or len(url) > 2000:
        return {"ok": False, "err": "invalid URL"}
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.\-]*://\S+", url) and \
            not url.startswith(("mailto:", "tel:")):
        return {"ok": False, "err": "give a full URL with a scheme, e.g. "
                                    "https://… , myapp://screen , market://details?id=…"}
    # v4 wrapped the URL in single quotes without escaping: a deep link
    # containing `'` broke out and appended a second `am`/shell command.
    r = run([paths.adb, "-s", serial, "shell",
             "am start -a android.intent.action.VIEW -d " + safe.sh_quote(url)],
            timeout=25)
    out = r["out"] + r["err"]
    ok = bool(r["ok"]) and "Error" not in out and "Exception" not in out
    return {"ok": ok, "url": url, "log": out[-400:]}


def set_proxy(paths: ToolPaths, serial: str, host_port: str) -> dict:
    """Route guest traffic through an HTTP proxy (live, no reboot).

    Writes the global http_proxy setting — the same switch the emulator's own
    extended-controls Proxy pane uses — and reverses with clear_proxy().
    """
    if (e := _check(paths, serial)):
        return e
    hp = str(host_port or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9.\-_]+:\d{1,5}", hp):
        return {"ok": False, "err": "proxy must look like host:port (e.g. 127.0.0.1:8888)"}
    r = run([paths.adb, "-s", serial, "shell",
             f"settings put global http_proxy {hp}"], timeout=20)
    chk = run([paths.adb, "-s", serial, "shell",
               "settings get global http_proxy"], timeout=15)
    applied = (chk["out"] or "").strip()
    return {"ok": bool(r["ok"]) and applied.startswith(hp.split(":")[0]),
            "proxy": hp, "verified": applied,
            "note": "Apps that ignore the system proxy (raw sockets) are not affected."}


def clear_proxy(paths: ToolPaths, serial: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    r = run([paths.adb, "-s", serial, "shell",
             "settings put global http_proxy :0"], timeout=20)
    return {"ok": bool(r["ok"]), "log": (r["out"] + r["err"])[-200:],
            "note": "proxy cleared"}


def eco_mode(paths: ToolPaths, serial: str, on: bool = True) -> dict:
    """LDPlayer/MuMu-style Eco mode, guest-side: animations off + caches trimmed.

    Animations are the biggest visible cost on an idle/background instance —
    killing the three animation scales plus trimming file caches typically
    frees several hundred MB of guest RAM and drops idle CPU noticeably.
    Turning Eco OFF restores default animation scales.
    """
    if (e := _check(paths, serial)):
        return e
    scales = "0 0 0" if on else "1 1 1"
    r = run([paths.adb, "-s", serial, "shell",
             f"settings put global window_animation_scale {scales.split()[0]}; "
             f"settings put global transition_animation_scale {scales.split()[1]}; "
             f"settings put global animator_duration_scale {scales.split()[2]}"],
            timeout=25)
    trimmed = 0
    if on:
        t = run([paths.adb, "-s", serial, "shell",
                 "pm trim-caches 999999999 2>/dev/null; echo done"], timeout=40)
        trimmed = 1 if "done" in t["out"] else 0
    return {"ok": bool(r["ok"]), "eco": on, "trim_ran": bool(trimmed),
            "note": ("Animations off, caches trimmed — instance runs lean. "
                     if on else "Animations restored to default speed.")}


def tcpip(paths: ToolPaths, serial: str, port: int = 5555) -> dict:
    """Switch the device to ADB-over-Wi-Fi (`adb tcpip`).

    After this, `adb connect <device-ip>:5555` works — the emulator's virtual
    network exposes the guest IP on the host, so the device IP is reported too.
    """
    if (e := _check(paths, serial)):
        return e
    port = max(1024, min(int(port or 5555), 65535))
    r = run([paths.adb, "-s", serial, "tcpip", str(port)], timeout=25)
    ip = run([paths.adb, "-s", serial, "shell",
              "ip -f inet addr show wlan0 2>/dev/null | grep -o 'inet [0-9.]*'"],
             timeout=20)
    device_ip = (ip["out"].split()[1] if len(ip["out"].split()) > 1 else "")
    return {"ok": bool(r["ok"]), "port": port, "device_ip": device_ip,
            "log": (r["out"] + r["err"])[-300:],
            "note": (f"reconnect later with: adb connect {device_ip}:{port}"
                     if device_ip else "TCP mode on; find the guest IP with 'ip addr'.")}


# --------------------------------------------------------------------------
# v5.1 — Smart App Archive (BlueStacks 16 parity): remove the APK, keep data
# --------------------------------------------------------------------------
ARCHIVE_DIR = Path.home() / "NovaDroid-Shared" / "Archived"


def archive_app(paths: ToolPaths, serial: str, package: str) -> dict:
    """Smart App Archive: pull the base APK to the host, then remove the
    INSTALLATION while keeping app data (`pm uninstall -k --user 0`).

    BlueStacks 16's headline disk feature (Oct 2026) is plain adb: the APK
    bytes leave the guest, data stays, and reinstalling the archived APK
    brings the app back with its state intact — a farm instance can shed
    gigabytes of games it only plays on weekends.
    """
    if (e := _check(paths, serial)):
        return e
    if not safe.valid_package(package):
        return {"ok": False, "err": "invalid package name"}
    r = run([paths.adb, "-s", serial, "shell", "pm", "path", package], timeout=20)
    apk_lines = [l.split(":", 1)[1] for l in r["out"].splitlines()
                 if l.startswith("package:")]
    if not r["ok"] or not apk_lines:
        return {"ok": False,
                "err": f"package not found on {serial}: {package}"}
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE_DIR / f"{package}.apk"
    p = run([paths.adb, "-s", serial, "pull", apk_lines[0], str(dest)],
            timeout=300)
    if not p["ok"]:
        return {"ok": False, "err": "could not pull the base APK before "
                                    "archiving — nothing was changed"}
    u = run([paths.adb, "-s", serial, "shell", "pm", "uninstall", "-k",
             "--user", "0", package], timeout=30)
    out = u["out"] + u["err"]
    if not u["ok"] or "Success" not in out:
        return {"ok": False, "archive": str(dest),
                "err": f"APK saved but uninstall failed: {out.strip()[-200:]}"}
    return {"ok": True, "package": package, "archived_to": str(dest),
            "bytes": dest.stat().st_size if dest.exists() else 0,
            "note": ("APK removed, app DATA kept on the device. Restore it "
                     "from the Archived list — the app returns with its "
                     "state intact.")}


def archived() -> dict:
    """List smart-archived APKs on the host."""
    items = []
    try:
        for f in sorted(ARCHIVE_DIR.glob("*.apk")):
            try:
                items.append({"package": f.stem, "bytes": f.stat().st_size,
                              "file": str(f)})
            except OSError:
                continue
    except OSError:
        pass
    return {"ok": True, "archived": items, "dir": str(ARCHIVE_DIR)}


def restore_app(paths: ToolPaths, serial: str, package: str) -> dict:
    """Reinstall a smart-archived APK; kept data is picked up automatically."""
    if (e := _check(paths, serial)):
        return e
    if not safe.valid_package(package):
        return {"ok": False, "err": "invalid package name"}
    apk = ARCHIVE_DIR / f"{package}.apk"
    if not apk.is_file():
        return {"ok": False, "err": f"no archived APK for {package}"}
    from . import adb as ADB
    return ADB.install_apk(paths, serial, str(apk))
