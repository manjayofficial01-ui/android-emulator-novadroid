"""AI-era capability layer for NovaDroid.

Three things, all stdlib-only and all honest about what they can and cannot do:

1. `ai_status()` — is this AVD ready for on-device AI?
   Gemini Nano runs inside **AICore**, an Android system service shipped to
   Pixel/partner devices via Google Play system modules. It is *not* part of
   the emulator's system image, so a stock AVD reports "not available" — and
   pretending otherwise is how developers lose a day. This probe says so,
   shows exactly which layer is missing, and lists what you *can* validate on
   an emulator (NNAPI delegation, LiteRT/ML Kit fallbacks, ARM translation).

2. `triage_logcat()` — turn 1000 lines of logcat into ranked, deduplicated
   findings. Pure function, no model, no network.

3. `interpret()` / `assist()` — natural-language device control
   ("rotate to landscape", "battery 20%", "why is it laggy?"). A deterministic
   intent parser, so the dashboard gets a command box that works offline and
   never hallucinates an adb flag.
"""
from __future__ import annotations

import re
from collections import Counter, OrderedDict

from . import safe
from .config import ToolPaths
from .runner import run

# Packages that make up Google's on-device AI stack.
AI_PACKAGES = {
    "com.google.android.aicore":
        ("AICore", "System service that hosts Gemini Nano"),
    "com.google.android.as":
        ("Private Compute Services", "Network/safety proxy AICore requires"),
    "com.google.android.gms":
        ("Google Play services", "Delivery vehicle for on-device AI modules"),
}

# ML runtimes that DO work in an emulator.
ML_PACKAGES = {
    "org.tensorflow": "TensorFlow / LiteRT",
    "com.google.android.gms.vision": "ML Kit (legacy vision)",
    "com.google.mlkit": "ML Kit",
}


def _check(paths: ToolPaths, serial: str) -> dict | None:
    if not paths.adb:
        return {"ok": False, "err": "adb not found"}
    if not serial:
        return {"ok": False, "err": "no device selected — Quickboot an AVD first"}
    if not safe.valid_serial(serial):
        return {"ok": False, "err": "invalid device serial"}
    return None


def _sh(paths: ToolPaths, serial: str, script: str, timeout: int = 30) -> dict:
    if not paths.adb or not safe.valid_serial(serial):
        return {"ok": False, "out": "", "err": "invalid device serial"}
    return run([paths.adb, "-s", serial, "shell", script], timeout=timeout)


# --------------------------------------------------------------------------
# 1. On-device AI readiness
# --------------------------------------------------------------------------
def _package_list(paths: ToolPaths, serial: str) -> list[str]:
    """Full package list in ONE round-trip (the old probe shelled out once per
    prefix — six calls, ~1s of pure latency on the AI panel)."""
    r = _sh(paths, serial, "pm list packages", timeout=30)
    return [l.replace("package:", "").strip()
            for l in r["out"].splitlines() if l.strip().startswith("package:")]


def _pkg_versions(paths: ToolPaths, serial: str, packages: list[str]) -> dict[str, str]:
    """versionName for several packages in one adb round-trip."""
    if not packages:
        return {}
    loop = " ".join(safe.sh_quote(p) for p in packages)
    # A named variable keeps `$p` out of a nested `$( ... )` quoting context;
    # Android's /system/bin/sh (mksh) is picky about that.
    script = (f'for p in {loop}; do '
              'v=$(dumpsys package "$p" 2>/dev/null | grep -m1 versionName); '
              'echo "$p=$v"; done')
    r = _sh(paths, serial, script, timeout=40)
    out: dict[str, str] = {}
    for line in r["out"].splitlines():
        k, _, v = line.partition("=")
        k = k.strip()
        if k in packages:
            out[k] = v.strip()[:60]
    return out


def ai_status(paths: ToolPaths, serial: str) -> dict:
    """Is Gemini Nano / on-device GenAI usable here, and if not, why not?"""
    if (e := _check(paths, serial)):
        return e
    from . import adb as ADB

    props = ADB.getprops(paths, serial, [
        "ro.build.version.sdk", "ro.build.version.release",
        "ro.product.model", "ro.product.cpu.abi",
        "ro.product.cpu.abilist", "ro.dalvik.vm.native.bridge",
    ])
    # One `pm list packages` for every probe below.
    all_pkgs = set(_package_list(paths, serial))

    def _has(prefix: str) -> bool:
        return any(p.startswith(prefix) for p in all_pkgs)

    versions = _pkg_versions(paths, serial,
                             [p for p in AI_PACKAGES if p in all_pkgs])
    found: dict[str, dict] = OrderedDict()
    for pkg, (label, role) in AI_PACKAGES.items():
        found[pkg] = {"package": pkg, "label": label or pkg, "role": role or "",
                      "installed": _has(pkg),
                      "version": versions.get(pkg, "")}

    aicore = found.get("com.google.android.aicore", {})
    gemini_ready = bool(aicore.get("installed"))

    # NNAPI / accelerator layer — this part DOES work in an emulator.
    nnapi = nnapi_status(paths, serial)

    ml_runtimes = [label for prefix, label in ML_PACKAGES.items() if _has(prefix)]

    layers = [
        {"layer": "AICore system service", "ok": aicore.get("installed", False),
         "detail": (aicore.get("version") or
                    "absent — not shipped in emulator system images")},
        {"layer": "Private Compute Services",
         "ok": found.get("com.google.android.as", {}).get("installed", False),
         "detail": "required network/safety proxy for AICore"},
        {"layer": "NNAPI / hardware acceleration",
         "ok": nnapi.get("available", False),
         "detail": nnapi.get("detail", "unknown")},
        {"layer": "ML runtime (LiteRT / ML Kit)",
         "ok": bool(ml_runtimes),
         "detail": ", ".join(ml_runtimes) or "none detected in installed apps"},
    ]

    if gemini_ready:
        level, headline = "ready", "Gemini Nano (AICore) is present on this device."
    elif nnapi.get("available"):
        level = "partial"
        headline = ("AICore is absent, so Gemini Nano cannot run — but the "
                    "NNAPI layer works, so on-device ML (LiteRT/ML Kit) can be "
                    "tested with a CPU/accelerator delegate.")
    else:
        level = "unavailable"
        headline = "No on-device AI stack detected on this AVD."

    return {
        "ok": True, "serial": serial, "level": level, "headline": headline,
        "gemini_nano": gemini_ready,
        "aicore_version": aicore.get("version", ""),
        "layers": layers, "nnapi": nnapi,
        "device": props, "ml_runtimes": ml_runtimes,
        "guidance": [
            "Emulator system images do not ship AICore, so Gemini Nano / ML Kit "
            "GenAI (Prompt, Summarization, Image Description) cannot run here — "
            "that is true of every emulator, not just NovaDroid.",
            "Validate your Gemini Nano code path on a physical Pixel 8+/Pixel "
            "Tablet, or gate it behind an `isAvailable()` check and exercise "
            "the fallback branch in the emulator.",
            "What you CAN test here: NNAPI delegation, LiteRT/TFLite inference, "
            "ML Kit non-GenAI APIs, and ARM-translated native ML libraries.",
        ],
        "checks": found,
    }


def nnapi_status(paths: ToolPaths, serial: str) -> dict:
    """NNAPI / neural-network HAL availability (best effort)."""
    if (e := _check(paths, serial)):
        return {"available": False, "detail": e.get("err", "unavailable")}
    r = _sh(paths, serial, "dumpsys neuralnetworks 2>&1 | head -20", timeout=25)
    blob = (r["out"] + " " + r["err"]).strip()
    unavailable = (not r["ok"]) or "Can't find service" in blob or \
        "not found" in blob.lower()
    if unavailable:
        # Fall back to looking for the HAL libraries directly.
        libs = _sh(paths, serial,
                   "ls /vendor/lib64/libneuralnetworks* "
                   "/vendor/lib64/hw/android.hardware.neuralnetworks* "
                   "2>/dev/null | head -5", timeout=20)
        names = [l.strip() for l in libs["out"].splitlines() if l.strip()]
        if names:
            return {"available": True, "detail": ", ".join(names[:3]),
                    "via": "hal-libraries", "raw": blob[:200]}
        return {"available": False,
                "detail": "no NNAPI service or HAL libraries found",
                "via": "probe", "raw": blob[:200]}
    devices = re.findall(r"^\s*([A-Za-z0-9_\-]+)\s*:\s*(?:\[|V?\d)",
                         blob, re.M)
    return {"available": True, "detail": ", ".join(devices[:4]) or "service present",
            "via": "dumpsys", "raw": blob[:400]}


# --------------------------------------------------------------------------
# 2. Logcat triage (pure)
# --------------------------------------------------------------------------
_SEVERITY = {"crash": 4, "anr": 4, "native": 4, "error": 3,
             "oom": 4, "warning": 1}

_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"FATAL EXCEPTION", re.I), "crash"),
    (re.compile(r"AndroidRuntime: (?:FATAL|Shutting down VM)", re.I), "crash"),
    (re.compile(r"Fatal signal \d+ \(SIG\w+\)", re.I), "native"),
    (re.compile(r"ANR in |am_anr|ActivityManager: ANR", re.I), "anr"),
    (re.compile(r"OutOfMemoryError|Failed to allocate", re.I), "oom"),
    (re.compile(r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+\d+\s+E ",
                re.M), "error"),
)

_HINTS = {
    "crash": "Uncaught exception killed the app — grab the stack trace above "
             "the FATAL EXCEPTION line for the throwing frame.",
    "anr": "Main thread was blocked for 5s+. Look for disk/network work or a "
           "lock held on the UI thread at the ANR timestamp.",
    "native": "Native crash (SIGSEGV/SIGABRT) — usually JNI or an ARM library "
              "misbehaving under translation. Check `ndk-stack` on the tombstone.",
    "oom": "Out of memory — often a bitmap leak or an unbounded cache. Raise "
           "vm.heapSize or profile allocations.",
    "error": "Tagged error. Check whether it repeats (a loop) or is one-off.",
    "warning": "Warning only.",
}


def _signature(line: str) -> str:
    """Collapse a log line into a stable signature so repeats group together."""
    s = re.sub(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+", "<ts>", line)
    s = re.sub(r"\bpid=\d+|\bPID: \d+|\b\d{3,}\b", "#", s)
    s = re.sub(r"0x[0-9a-fA-F]+", "0x#", s)
    s = re.sub(r"/[^\s:]+/", "<path>/", s)
    s = re.sub(r"[\w.]+@[0-9a-f]+", "<obj>", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:160]


def _exception_of(text: str) -> str:
    m = re.search(r"([A-Za-z_$][\w$.]*(?:Exception|Error|Throwable))", text)
    return m.group(1) if m else ""


def triage_logcat(lines: list[str]) -> dict:
    """Ranked, deduplicated findings from raw logcat lines. Pure function."""
    buckets: "OrderedDict[str, dict]" = OrderedDict()
    counts = Counter()
    for raw in lines or []:
        line = str(raw).rstrip()
        if not line.strip():
            continue
        kind = next((k for pat, k in _RULES if pat.search(line)), "")
        if not kind:
            continue
        sig = _signature(line)
        exc = _exception_of(line)
        key = f"{kind}|{exc or sig}"
        counts[kind] += 1
        b = buckets.setdefault(key, {"kind": kind, "count": 0,
                                     "exception": exc, "sample": line.strip(),
                                     "signature": sig})
        b["count"] += 1
        # Keep the most informative sample: prefer one carrying an exception.
        if exc and not b["exception"]:
            b["exception"] = exc
            b["sample"] = line.strip()

    findings = sorted(
        buckets.values(),
        key=lambda f: (-_SEVERITY.get(f["kind"], 1) * 1000 - f["count"]))
    for f in findings:
        f["severity"] = _SEVERITY.get(f["kind"], 1)
        f["hint"] = _HINTS.get(f["kind"], "")

    total = sum(counts.values())
    worst = findings[0]["kind"] if findings else "none"
    return {
        "ok": True, "lines": len(lines or []), "issues": total,
        "counts": dict(counts), "findings": findings[:25],
        "worst": worst,
        "summary": _triage_summary(findings, total, len(lines or [])),
    }


def _triage_summary(findings: list[dict], total: int, scanned: int) -> str:
    if not findings:
        return f"Clean — no crashes, ANRs or errors in {scanned} lines."
    top = findings[0]
    bits = [f"{top['count']}× {top['kind']}" +
            (f" ({top['exception']})" if top["exception"] else "")]
    for f in findings[1:4]:
        bits.append(f"{f['count']}× {f['kind']}")
    return f"{total} issue line(s) in {scanned} scanned — " + ", ".join(bits)


def triage(paths: ToolPaths, serial: str, lines: int = 1000,
           spec: str = "") -> dict:
    """Fetch logcat and triage it in one call."""
    from . import apps as APPS
    if (e := _check(paths, serial)):
        return e
    lc = APPS.logcat(paths, serial, lines=lines, spec=spec)
    if not lc.get("ok"):
        return lc
    result = triage_logcat(lc.get("lines", []))
    result["serial"] = serial
    result["scanned_lines"] = len(lc.get("lines", []))
    return result


# --------------------------------------------------------------------------
# 3. Natural-language command interpreter (pure)
# --------------------------------------------------------------------------
_NUM = r"(-?\d+(?:\.\d+)?)"
_PKG = r"((?:[A-Za-z][\w]*(?:\.[\w]+){2,}))"
_PATH = r"([A-Za-z]:\\[^\s\"']+|\/[^\s\"']+)"


def _f(text, pat, group=1, default=None, cast=str):
    m = re.search(pat, text, re.I)
    if not m:
        return default
    try:
        return cast(m.group(group))
    except (TypeError, ValueError):
        return default


# (regex, intent, arg-builder) — first match wins; ordered most-specific first.
_INTENTS: tuple[tuple[re.Pattern, str, object], ...] = (
    (re.compile(r"\b(why|what).*(slow|lag|jank|stutter|sluggish)|\bdiagnos|"
                r"\bsmoothness\b|\bperf(ormance)?\b|\bfps\b", re.I),
     "perf.diagnose", None),
    (re.compile(r"\b(optimi[sz]e|tune|auto-?tune|make it fast|speed up)\b", re.I),
     "perf.autotune", None),
    (re.compile(r"\b(triage|analy[sz]e|summari[sz]e).*(log|crash|logcat)|"
                r"\bwhy.*(crash|fail)|what('s| is) (wrong|broken)", re.I),
     "ai.triage", None),
    # `availab\w*` not `availab\b`: a trailing \b can never match inside
    # "available", so "is gemini nano available?" fell through to no match.
    (re.compile(r"(\b(ai|gemini|nano|ml)\b.*\b(ready|status|availab\w*|"
                r"check|support)\b|\bai status\b|\bon-?device ai\b)", re.I),
     "ai.status", None),
    (re.compile(r"\b(landscape|portrait|seascape|rotate|orientation)\b", re.I),
     "display.orientation", "orientation"),
    (re.compile(r"\b(battery|charge|charging)\b", re.I), "device.battery", "battery"),
    (re.compile(r"\b(gps|location|geo|coordinates?)\b", re.I), "device.gps", "gps"),
    (re.compile(r"\b(screenshot|capture screen|screen shot)\b", re.I),
     "screen.capture", None),
    (re.compile(r"\b(record|screen ?record)\b", re.I), "screen.record", "record"),
    (re.compile(r"\b(install|sideload)\b", re.I), "apk.install", "apk"),
    (re.compile(r"\b(uninstall|remove app)\b", re.I), "app.uninstall", "package"),
    (re.compile(r"\b(force[- ]?stop|kill|close)\b", re.I), "app.stop", "package"),
    (re.compile(r"\b(clear|wipe)\b.*\b(data|app)\b", re.I), "app.clear", "package"),
    (re.compile(r"\b(launch|open|start)\b", re.I), "app.launch", "package"),
    (re.compile(r"\b(swipe|scroll)\b", re.I), "input.swipe", "swipe"),
    (re.compile(r"\b(long[- ]?press|press and hold)\b", re.I),
     "input.long_press", "point"),
    (re.compile(r"\b(tap|click|touch)\b", re.I), "input.tap", "point"),
    (re.compile(r"\b(type|enter|input)\b", re.I), "input.text", "text"),
    (re.compile(r"\b(home|back|recents?|recent apps|power)\b", re.I),
     "input.key", "navkey"),
    (re.compile(r"\b(airplane|wifi|network|signal|bars)\b", re.I),
     "device.network", "network"),
)


def _extract(text: str, kind: str) -> dict:
    t = text
    if kind == "orientation":
        m = re.search(r"\b(landscape|portrait|seascape|auto)\b", t, re.I)
        return {"mode": m.group(1).lower()} if m else {}
    if kind == "battery":
        lvl = _f(t, r"(\d{1,3})\s*%?", 1, None, int)
        st = _f(t, r"\b(charging|discharging|not-charging|full)\b", 1)
        return {k: v for k, v in (("level", lvl), ("status", st)) if v is not None}
    if kind == "gps":
        m = re.search(rf"{_NUM}\s*[, ]+\s*{_NUM}", t)
        if m:
            return {"lat": float(m.group(1)), "lng": float(m.group(2))}
        return {}
    if kind == "apk":
        p = _f(t, _PATH)
        return {"apk_path": p} if p else {}
    if kind == "package":
        p = _f(t, _PKG)
        if p and "." in p:
            return {"package": p}
        return {}
    if kind == "point":
        m = re.search(rf"{_NUM}\s*[,x ]+\s*{_NUM}", t)
        return {"x": int(float(m.group(1))), "y": int(float(m.group(2)))} if m else {}
    if kind == "swipe":
        m = re.search(rf"{_NUM}\s*[,x ]+\s*{_NUM}\s*(?:->|to|=>)\s*"
                      rf"{_NUM}\s*[,x ]+\s*{_NUM}", t)
        if m:
            return {"x1": int(float(m.group(1))), "y1": int(float(m.group(2))),
                    "x2": int(float(m.group(3))), "y2": int(float(m.group(4)))}
        m = _f(t, r"\b(up|down|left|right)\b", 1)
        return {"direction": m.lower()} if m else {}
    if kind == "text":
        m = re.search(r"(?:type|enter|input|send)\s+(?:the\s+)?(?:text\s+)?"
                      r"[\"']?([^\"']{1,200})[\"']?$", t, re.I)
        return {"text": m.group(1).strip()} if m else {}
    if kind == "navkey":
        m = _f(t, r"\b(home|back|recents?|recent apps|power)\b", 1)
        return {"key": m.lower()} if m else {}
    if kind == "record":
        s = _f(t, rf"{_NUM}\s*(?:s\b|sec|seconds?)", 1, None, float)
        return {"seconds": int(s)} if s else {}
    if kind == "network":
        sp = _f(t, r"\b(gsm|gprs|edge|umts|hspa|lte|full)\b", 1)
        ap = "airplane" if re.search(r"airplane", t, re.I) else ""
        return {k: v for k, v in (("speed", sp), ("mode", ap)) if v}
    return {}


# Intents that need coordinates/packages the sentence failed to supply.
_REQUIRED = {
    "input.tap": ("x", "y"),
    "input.long_press": ("x", "y"),
    "input.text": ("text",),
    "apk.install": ("apk_path",),
    "app.launch": ("package",),
    "app.stop": ("package",),
    "app.uninstall": ("package",),
    "app.clear": ("package",),
    "device.gps": ("lat", "lng"),
    "display.orientation": ("mode",),
    "input.key": ("key",),
}

# Intents that are meaningful with no device selected (everything else is a
# device operation and must be rejected rather than run against nothing).
_DEVICELESS = {"perf.autotune", "ai.status"}


def interpret(text: str) -> dict:
    """Map a sentence to a NovaDroid intent. Pure, deterministic, offline."""
    t = (text or "").strip()
    if not t:
        return {"ok": False, "err": "empty command"}
    match = next(((pat, intent, kind) for pat, intent, kind in _INTENTS
                  if pat.search(t)), None)
    if not match:
        return {"ok": False, "err": f"did not understand: {t[:80]}",
                "hint": "Try: 'rotate to landscape', 'battery 20%', "
                        "'launch com.example.app', 'why is it laggy?'"}
    _pat, intent, kind = match
    args = _extract(t, kind) if kind else {}
    missing = [k for k in _REQUIRED.get(intent, ()) if k not in args]
    # An incomplete command is still understood — report what is missing so
    # the UI can ask for exactly that instead of guessing coordinates.
    conf = 0.95 if not missing else 0.55
    if len(t.split()) <= 2:
        conf = min(conf, 0.8)
    return {
        "ok": not missing, "intent": intent, "args": args,
        "confidence": round(conf, 2), "missing": missing,
        "needs_device": intent not in _DEVICELESS,
        "reply": _describe(intent, args, missing),
        "text": t,
    }


def _describe(intent: str, args: dict, missing: list[str]) -> str:
    if missing:
        return (f"Understood '{intent}', but I need: {', '.join(missing)}.")
    human = {
        "perf.diagnose": "measure frame jank and find the bottleneck",
        "perf.autotune": "auto-tune the AVD config for smoothness",
        "ai.triage": "triage logcat for crashes and ANRs",
        "ai.status": "check on-device AI (Gemini Nano / AICore) readiness",
        "display.orientation": f"set orientation to {args.get('mode')}",
        "device.battery": f"set battery to {args.get('level', 85)}%"
                          + (f" ({args['status']})" if args.get("status") else ""),
        "device.gps": f"set GPS to {args.get('lat')}, {args.get('lng')}",
        "screen.capture": "take a screenshot",
        "screen.record": f"record {args.get('seconds', 30)}s of screen",
        "apk.install": f"install {args.get('apk_path')}",
        "app.uninstall": f"uninstall {args.get('package')}",
        "app.stop": f"force-stop {args.get('package')}",
        "app.clear": f"clear data for {args.get('package')}",
        "app.launch": f"launch {args.get('package')}",
        "input.swipe": f"swipe {args}",
        "input.long_press": f"long-press at {args.get('x')},{args.get('y')}",
        "input.tap": f"tap at {args.get('x')},{args.get('y')}",
        "input.text": f"type \"{args.get('text')}\"",
        "input.key": f"send the {args.get('key')} key",
        "device.network": f"network {args}",
    }
    return human.get(intent, intent)


def assist(paths: ToolPaths, serial: str, text: str) -> dict:
    """Interpret a sentence and run it. Returns what was understood AND done."""
    plan = interpret(text)
    if not plan.get("ok"):
        return plan
    intent, args = plan["intent"], plan["args"]
    if plan.get("needs_device") and not serial:
        return {**plan, "ok": False, "ran": False,
                "err": "this command needs a device — Quickboot an AVD and "
                       "pick it from the Device list"}
    from . import adb as ADB
    from . import apps as APPS
    from . import display as DISP
    from . import perf as PERF

    dispatch = {
        "perf.diagnose": lambda: PERF.diagnose(paths, serial, args.get("package", "")),
        "ai.triage": lambda: triage(paths, serial, args.get("lines", 1000)),
        "ai.status": lambda: ai_status(paths, serial),
        "display.orientation": lambda: DISP.set_orientation(paths, serial,
                                                            args.get("mode", "")),
        "device.battery": lambda: ADB.battery(paths, serial,
                                              args.get("level", 85),
                                              args.get("status", "not-charging")),
        "device.gps": lambda: ADB.geo_fix(paths, serial, args.get("lat", 0.0),
                                          args.get("lng", 0.0)),
        "screen.capture": lambda: ADB.screenshot(paths, serial),
        "screen.record": lambda: APPS.record_start(paths, serial,
                                                   args.get("seconds", 30)),
        "apk.install": lambda: ADB.install_apk(paths, serial, args.get("apk_path", "")),
        "app.uninstall": lambda: APPS.uninstall(paths, serial, args.get("package", "")),
        "app.stop": lambda: APPS.stop_app(paths, serial, args.get("package", "")),
        "app.clear": lambda: APPS.clear_app(paths, serial, args.get("package", "")),
        "app.launch": lambda: APPS.launch(paths, serial, args.get("package", "")),
        "input.tap": lambda: ADB.tap(paths, serial, args.get("x", 0), args.get("y", 0)),
        "input.long_press": lambda: ADB.long_press(paths, serial,
                                                   args.get("x", 0), args.get("y", 0)),
        "input.text": lambda: ADB.input_text(paths, serial, args.get("text", "")),
        "input.key": lambda: ADB.keyevent(paths, serial,
                                          {"home": 3, "back": 4, "recent": 187,
                                           "recents": 187, "recent apps": 187,
                                           "power": 26}.get(args.get("key", ""), 3)),
        "device.network": lambda: APPS.network(paths, serial,
                                               args.get("speed", ""),
                                               args.get("delay", "")),
        "perf.autotune": lambda: PERF.autotune(paths, args.get("name", ""),
                                               apply=bool(args.get("apply"))),
    }
    fn = dispatch.get(intent)
    if fn is None:
        return {**plan, "ok": False, "ran": False,
                "err": f"intent '{intent}' is understood but not wired up yet"}
    try:
        result = fn()
    except Exception as e:  # never let a bad command take down the agent
        # `str(e)` used to go straight to the dashboard and could contain host
        # paths, adb arguments or SDK internals. Log the detail locally instead.
        print(f"[ai] {intent} failed ({type(e).__name__}): {e}")
        return {**plan, "ok": False, "ran": False,
                "err": f"command failed ({type(e).__name__})"}
    return {**plan, "ran": True, "ok": bool((result or {}).get("ok", True)),
            "result": result}
