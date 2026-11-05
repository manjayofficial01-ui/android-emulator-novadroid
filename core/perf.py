"""Smoothness engine — measure jank, find the bottleneck, and tune the AVD.

"Runs smoothly" is not a vibe, it is three numbers that have to agree:

  1. GUEST FRAMES    — `dumpsys gfxinfo` janky-frame % and p95 frame time.
  2. HOST PRESSURE   — CPU/RAM/disk headroom on the PC actually running QEMU.
  3. RENDER PATH     — which backend is live (host GPU vs SwiftShader/Lavapipe).

Most "the emulator is slow" reports are #3 wearing a #1 disguise: software
rendering produces janky frames that look like a CPU problem. This module
measures all three, names the bottleneck, and can fix the config in one call.

Auto-tune rules come from the release notes and QEMU scaling research:
  * 4 vCPU is the useful ceiling — more does not boot faster (Linaro/QEMU).
  * API 37+ forces >= 4 GB RAM; the emulator silently overrides anything less.
  * `-gpu host` (gfxstream to the real GPU) >> SwiftShader/Lavapipe software.
  * Vulkan and snapshots do not mix (36.4.9+) -> pair Vulkan with -no-snapshot.
"""
from __future__ import annotations

import re
import sys
import threading
import time
from collections import deque

from . import host as HOST
from . import inifile
from . import safe
from .config import ToolPaths
from .runner import run

# 60 fps budget. A frame is "janky" once it misses this.
TARGET_FRAME_MS = 16.7
# Ring buffer of recent samples so the dashboard can show a trend, not a point.
_HISTORY: deque[dict] = deque(maxlen=120)


def _check(paths: ToolPaths, serial: str) -> dict | None:
    if not paths.adb:
        return {"ok": False, "err": "adb not found"}
    if not serial:
        return {"ok": False, "err": "no device selected — Quickboot an AVD first"}
    return None


def _sh(paths: ToolPaths, serial: str, script: str, timeout: int = 30) -> dict:
    return run([paths.adb, "-s", serial, "shell", script], timeout=timeout)


# --------------------------------------------------------------------------
# Parsing (pure — unit-testable without a device)
# --------------------------------------------------------------------------
def parse_gfxinfo(text: str) -> dict:
    """Parse `dumpsys gfxinfo` into structured frame stats.

    Shape varies by Android version, so everything is optional and the
    newest synonym wins (36+ reports "Frame deadline missed", older builds
    only have "Janky frames").
    """
    out: dict = {"frames": None, "janky": None, "janky_pct": None,
                 "percentiles": {}, "counters": {}, "packages": []}
    if not text:
        return out
    m = re.search(r"Total frames rendered:\s*(\d+)", text)
    if m:
        out["frames"] = int(m.group(1))
    m = re.search(r"Janky frames:\s*(\d+)\s*\(([\d.]+)%\)", text)
    if m:
        out["janky"], out["janky_pct"] = int(m.group(1)), float(m.group(2))
    for pct, ms in re.findall(r"(\d+)th percentile:\s*(\d+)ms", text):
        out["percentiles"][int(pct)] = int(ms)
    for name, val in re.findall(r"Number ([A-Za-z][A-Za-z ]*):\s*(\d+)", text):
        out["counters"][name.strip()] = int(val)
    out["packages"] = re.findall(r"\*\* Graphics info for pid \d+ \[([^\]]+)\]",
                                 text)
    return out


def score(janky_pct: float | None, p95_ms: float | None,
          target_ms: float = TARGET_FRAME_MS) -> float:
    """0–100 smoothness score. Pure function.

    Jank dominates (it is what the eye notices), and frames that overrun the
    budget by a wide margin cost more than ones that just barely miss.
    """
    s = 100.0
    s -= min(55.0, float(janky_pct or 0) * 2.5)
    if p95_ms:
        over = max(0.0, float(p95_ms) - target_ms) / target_ms
        s -= min(45.0, over * 30.0)
    # Caps sum to 100, so a catastrophically janky device actually reaches 0
    # instead of bottoming out at a misleading "15/100".
    return round(max(0.0, min(100.0, s)), 1)


def verdict_for(score_value: float) -> str:
    if score_value >= 85:
        return "smooth"
    if score_value >= 70:
        return "mostly smooth"
    if score_value >= 50:
        return "janky"
    return "stuttering"


def classify_renderer(blob: str) -> str:
    """host | swiftshader | lavapipe | angle | software | unknown"""
    low = (blob or "").lower()
    if "lavapipe" in low or "llvmpipe" in low:
        return "lavapipe"
    if "swiftshader" in low or "swangle" in low:
        return "swiftshader"
    if "angle" in low:
        return "angle"
    if "virgl" in low or "virtio" in low:
        return "virgl"
    if re.search(r"software|softpipe", low):
        return "software"
    # A real GPU string (NVIDIA/AMD/Intel/Adreno/Mesa DRI) means host passthrough.
    if re.search(r"nvidia|geforce|radeon|amd |intel\(r\)|mesa|adreno|apple m\d",
                 low):
        return "host"
    return "unknown"


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------
def gfxinfo(paths: ToolPaths, serial: str, package: str = "") -> dict:
    """Frame stats. With no package the whole system is summarised."""
    if (e := _check(paths, serial)):
        return e
    pkg = re.sub(r"[^A-Za-z0-9._]", "", package or "")
    cmd = f"dumpsys gfxinfo {pkg}" if pkg else "dumpsys gfxinfo"
    r = _sh(paths, serial, cmd, timeout=40)
    stats = parse_gfxinfo(r["out"])
    if stats["frames"] is None and not pkg:
        # Some images only emit per-package stats; fall back to the top one.
        if stats["packages"]:
            return gfxinfo(paths, serial, stats["packages"][0])
    stats["ok"] = bool(r["ok"])
    stats["package"] = pkg or (stats["packages"][0] if stats["packages"] else "")
    return stats


def refresh_rate(paths: ToolPaths, serial: str) -> float | None:
    """Display refresh rate in Hz (best effort)."""
    r = _sh(paths, serial, "dumpsys display | grep -i -m5 refreshrate", timeout=25)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:Hz|fps)", r["out"], re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"refreshRate[=:\s]+(\d+(?:\.\d+)?)", r["out"], re.I)
    return float(m.group(1)) if m else None


def renderer(paths: ToolPaths, serial: str) -> dict:
    """Which graphics backend is actually live — the #1 smoothness factor."""
    if (e := _check(paths, serial)):
        return e
    from . import adb as ADB
    props = ADB.getprops(paths, serial, [
        "ro.hardware.egl", "ro.hardware.vulkan", "qemu.gles",
        "debug.hwui.renderer", "ro.opengles.version", "ro.board.platform",
    ])
    sf = _sh(paths, serial,
             "dumpsys SurfaceFlinger | grep -i -m3 -e 'GLES:' -e 'Vulkan:' -e renderer",
             timeout=25)
    blob = " ".join([str(v) for v in props.values()]) + " " + sf["out"]
    kind = classify_renderer(blob)
    return {"ok": True, "kind": kind,
            "software": kind in ("swiftshader", "lavapipe", "software", "angle"),
            "props": props, "raw": (sf["out"] or "")[:400],
            "note": {
                "host": "Host GPU via gfxstream — the fast path.",
                "swiftshader": "SwiftShader: CPU rasterisation. Correct but slow; "
                               "expect 3–5× slower UI. Try -gpu host.",
                "lavapipe": "Lavapipe: software Vulkan. Slower than a real GPU; "
                            "fine for compatibility testing.",
                "angle": "ANGLE translation layer — slower than native host.",
            }.get(kind, "Renderer could not be identified.")}


def _guest_pressure(paths: ToolPaths, serial: str) -> dict:
    from . import adb as ADB
    perf = ADB.perf(paths, serial)
    p = perf.get("perf", {}) if perf.get("ok") else {}
    return {"mem": p.get("mem", {}), "load": p.get("load", {}),
            "uptime": p.get("uptime", "")}


def measure(paths: ToolPaths, serial: str, package: str = "") -> dict:
    """One combined sample: guest frames + render path + host pressure."""
    if (e := _check(paths, serial)):
        return e
    gfx = gfxinfo(paths, serial, package)
    rend = renderer(paths, serial)
    hz = refresh_rate(paths, serial)
    guest = _guest_pressure(paths, serial)
    p95 = gfx.get("percentiles", {}).get(95)
    sc = score(gfx.get("janky_pct"), p95)
    sample = {
        "ok": True, "t": time.time(), "serial": serial,
        "package": gfx.get("package", ""),
        "frames": gfx.get("frames"), "janky": gfx.get("janky"),
        "janky_pct": gfx.get("janky_pct"),
        "percentiles": gfx.get("percentiles", {}),
        "counters": gfx.get("counters", {}),
        "p95_ms": p95, "refresh_hz": hz,
        "renderer": rend.get("kind"), "software_render": rend.get("software"),
        "guest": guest, "score": sc, "verdict": verdict_for(sc),
    }
    _HISTORY.append({"t": sample["t"], "serial": serial, "score": sc,
                     "janky_pct": gfx.get("janky_pct"), "p95_ms": p95,
                     "renderer": rend.get("kind")})
    return sample


def history(limit: int = 60) -> dict:
    items = list(_HISTORY)[-max(1, min(int(limit or 60), 120)):]
    scores = [h["score"] for h in items if h.get("score") is not None]
    return {"ok": True, "samples": items, "count": len(items),
            "avg": round(sum(scores) / len(scores), 1) if scores else None,
            "min": min(scores) if scores else None,
            "max": max(scores) if scores else None}


# --------------------------------------------------------------------------
# Bottleneck diagnosis
# --------------------------------------------------------------------------
def bottlenecks(sample: dict, host_snap: dict) -> list[dict]:
    """Ranked, human-readable causes of jank. Pure-ish (dicts in, list out)."""
    out: list[dict] = []
    rend = sample.get("renderer")
    if rend in ("swiftshader", "lavapipe", "software"):
        out.append({"id": "software_render", "severity": "high",
                    "what": f"Software rendering ({rend})",
                    "why": "Frames are rasterised on the host CPU. This is the "
                           "single biggest cause of emulator jank.",
                    "fix": "Set GPU to host/auto in the AVD, launch with "
                           "-gpu host, and update your GPU driver."})
    elif rend == "angle":
        out.append({"id": "angle", "severity": "medium",
                    "what": "ANGLE translation layer",
                    "why": "Draw calls are translated rather than passed through.",
                    "fix": "Try -gpu host if your driver supports gfxstream."})

    hpct = (host_snap or {}).get("cpu_pct")
    if hpct is not None and hpct >= 90:
        out.append({"id": "host_cpu", "severity": "high",
                    "what": f"Host CPU saturated ({hpct}%)",
                    "why": "QEMU is competing with the host for cores.",
                    "fix": "Close background apps, or lower AVD cores to 2–4 "
                           "(more than 4 does not boot faster)."})
    hmem = (host_snap or {}).get("mem") or {}
    if hmem.get("pct") is not None and hmem["pct"] >= 90:
        out.append({"id": "host_ram", "severity": "high",
                    "what": f"Host RAM pressure ({hmem['pct']}%)",
                    "why": "The host is swapping, so every guest memory access "
                           "hits disk.",
                    "fix": "Run fewer instances, or lower AVD RAM toward 4 GB."})

    gmem = ((sample.get("guest") or {}).get("mem") or {})
    if gmem.get("pct") is not None and gmem["pct"] >= 90:
        out.append({"id": "guest_ram", "severity": "medium",
                    "what": f"Guest RAM nearly full ({gmem['pct']}%)",
                    "why": "The guest is killing/restarting processes to cope.",
                    "fix": "Raise hw.ramSize, or reduce what is installed."})

    load1 = ((sample.get("guest") or {}).get("load") or {}).get("load1")
    if load1 is not None and load1 >= 4.0:
        out.append({"id": "guest_cpu", "severity": "medium",
                    "what": f"Guest load average {load1}",
                    "why": "Something inside Android is burning CPU.",
                    "fix": "Check the top output in Guest performance; often a "
                           "stuck sync or Play Services loop."})

    jp = sample.get("janky_pct")
    if jp is not None and jp >= 20 and not any(
            b["id"].startswith(("software_render", "host_", "guest_"))
            for b in out):
        out.append({"id": "app_jank", "severity": "medium",
                    "what": f"{jp}% janky frames with headroom to spare",
                    "why": "Host and renderer are fine, so this is app-side.",
                    "fix": "Profile the app (slow UI thread / bitmap uploads in "
                           "the counters below)."})
    return out


def diagnose(paths: ToolPaths, serial: str, package: str = "") -> dict:
    """Measure + explain + prescribe. One call for the dashboard."""
    sample = measure(paths, serial, package)
    if not sample.get("ok"):
        return sample
    snap = HOST.snapshot()
    causes = bottlenecks(sample, snap)
    return {"ok": True, "sample": sample, "host": snap,
            "bottlenecks": causes,
            "score": sample["score"], "verdict": sample["verdict"],
            "summary": _summary(sample, causes)}


def _summary(sample: dict, causes: list[dict]) -> str:
    if not causes:
        return (f"{sample['verdict'].title()} (score {sample['score']}) — no "
                "bottleneck detected.")
    top = causes[0]
    return (f"{sample['verdict'].title()} (score {sample['score']}) — "
            f"top cause: {top['what']}. {top['fix']}")


# --------------------------------------------------------------------------
# Auto-tune
# --------------------------------------------------------------------------
def api_level(config: dict) -> int:
    """API level from an AVD config dict (image.sysdir.1 = .../android-36/...)."""
    m = re.search(r"android-(\d+)", str(config.get("image.sysdir.1", "")))
    if m:
        return int(m.group(1))
    for key in ("ro.build.version.sdk", "image.android.api"):
        v = str(config.get(key, ""))
        if v.isdigit():
            return int(v)
    return 0


def recommend(host_snap: dict, avd_config: dict) -> list[dict]:
    """Config changes that will actually help. Pure function.

    `host_snap` is `host.snapshot()`-shaped; `avd_config` is the parsed
    AVD config.ini. Returns a list of {key, current, suggested, why}.
    """
    cpus = int(host_snap.get("cpu_count") or 4)
    total_mb = int((host_snap.get("mem") or {}).get("total_mb") or 8192)
    running = int(host_snap.get("emulator_count") or 0)
    api = api_level(avd_config)
    recs: list[dict] = []

    def add(key, current, suggested, why):
        recs.append({"key": key, "current": str(current),
                     "suggested": str(suggested), "why": why})

    # Cores: 4 is the useful ceiling; leave headroom for the host + QEMU I/O.
    want_cores = max(2, min(4, max(1, cpus - 1)))
    cur_cores = str(avd_config.get("hw.cpu.ncore", ""))
    if cur_cores != str(want_cores):
        add("hw.cpu.ncore", cur_cores or "?", want_cores,
            f"4 vCPU is the useful ceiling — boot time stops improving past it "
            f"(host has {cpus} cores, {running} emulator(s) running).")

    # RAM: API 37+ forces >= 4 GB; above ~8 GB the host starts to suffer.
    # 37.1.11 added unit suffixes to config values ("6G", "4096M"). The old
    # `re.sub(r"\D", ...)` turned "6G" into 6 MB and proposed a nonsensical
    # downgrade every single time. `safe.memory_mb` understands both forms.
    cur_ram = safe.memory_mb(avd_config.get("hw.ramSize", "0"))
    if total_mb >= 32768 and running <= 2:
        want_ram = 6144
    elif total_mb >= 16384:
        want_ram = 4096
    else:
        want_ram = 4096  # API 37 floor — below this the emulator overrides it
    if api >= 37 and want_ram < 4096:
        want_ram = 4096
    if cur_ram != want_ram:
        add("hw.ramSize", cur_ram or "?", want_ram,
            f"API {api or '?'} needs >= 4 GB; host has {total_mb} MB with "
            f"{running} instance(s) up.")

    if str(avd_config.get("hw.gpu.enabled", "")).lower() != "yes":
        add("hw.gpu.enabled", avd_config.get("hw.gpu.enabled", "?"), "yes",
            "GPU acceleration off forces CPU rasterisation.")
    mode = str(avd_config.get("hw.gpu.mode", "")).lower()
    if mode not in ("auto", "host"):
        add("hw.gpu.mode", mode or "?", "auto",
            "'auto' picks the real GPU via gfxstream and falls back to "
            "software only if the driver fails.")
    if str(avd_config.get("hw.keyboard", "")).lower() != "yes":
        add("hw.keyboard", avd_config.get("hw.keyboard", "?"), "yes",
            "Lets you type from the PC keyboard instead of the on-screen one.")
    if str(avd_config.get("fastboot.forceColdBoot", "")).lower() != "no":
        add("fastboot.forceColdBoot", avd_config.get("fastboot.forceColdBoot", "?"),
            "no", "Quickboot restores a snapshot in 6–15s vs 50s+ cold.")
    cur_heap = safe.memory_mb(avd_config.get("vm.heapSize", "0"))
    want_heap = 576 if want_ram >= 4096 else 384
    if cur_heap and cur_heap != want_heap:
        add("vm.heapSize", cur_heap, want_heap,
            f"Dalvik heap should track RAM ({want_ram} MB).")
    return recs


def runtime_flags(sample: dict, host_snap: dict) -> list[str]:
    """Best raw-boot flags for THIS machine right now. Pure function.

    Deliberately conservative: never recommends a flag that could make an
    API 37 AVD boot with less than its 4 GB hard floor, and never pairs
    Vulkan with a snapshot (36.4.9+ incompatibility).
    """
    sample = sample or {}
    host_snap = host_snap or {}
    flags: list[str] = []
    rend = sample.get("renderer")
    software = bool(sample.get("software_render")) or rend in (
        "swiftshader", "lavapipe", "software", "angle")
    if rend in ("swiftshader", "lavapipe", "software", "angle", "unknown"):
        flags.append("-gpu host")
    try:
        cpus = int(host_snap.get("cpu_count") or 4)
        total_mb = int((host_snap.get("mem") or {}).get("total_mb") or 8192)
    except (TypeError, ValueError):
        cpus, total_mb = 4, 8192
    flags.append(f"-cores {max(2, min(4, cpus - 1))}")
    flags.append(f"-memory {6144 if total_mb >= 32768 else 4096}")
    if software:
        flags.append("-no-snapshot")  # Vulkan + snapshots never mix
    return flags


# --------------------------------------------------------------------------
# Intel hybrid CPUs (P/E cores) — the headless-throttling trap (37.3.x)
# --------------------------------------------------------------------------
#: E-core throttling of headless emulators was fixed in 37.3.6 canary
#: (issue #572278410: QEMU power-throttled onto E-cores on Intel hybrid
#: CPUs — cold boots 2–3x slower, ANR storms). Older engines still hit it,
#: so NovaDroid detects hybrid silicon and can pin qemu to the P-cores.
_HYBRID_CACHE: dict | None = None
_HYBRID_LOCK = threading.Lock()


def detect_hybrid() -> dict:
    """Detect an Intel hybrid (P+E core) CPU and compute a P-core mask.

    Windows method: `HKLM\\HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\N`
    exposes per-logical-processor "~MHz" (max clock). P-cores clock
    materially higher than E-cores, so two distinct clock groups with a
    >=1.5x spread = hybrid. Cross-checked against the marketing name
    ("12th/13th/14th Gen", "Core Ultra"). Cached — registry reads are cheap
    but not free, and the answer cannot change without a reboot.
    """
    global _HYBRID_CACHE
    with _HYBRID_LOCK:
        if _HYBRID_CACHE is not None:
            return dict(_HYBRID_CACHE)
    out: dict = {"hybrid": False, "logical_cpus": 0, "pcore_count": 0,
                 "pcore_mask": 0, "ecore_count": 0, "name": "",
                 "detail": "", "platform": sys.platform}
    try:
        if sys.platform == "win32":
            import winreg
            name = ""
            mhz: list[int] = []
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor") as root:
                i = 0
                while True:
                    try:
                        key_name = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(root, key_name) as k:
                            mhz.append(int(winreg.QueryValueEx(k, "~MHz")[0]))
                            if not name:
                                name = str(winreg.QueryValueEx(
                                    k, "ProcessorNameString")[0])
                    except OSError:
                        continue
            out["logical_cpus"] = len(mhz)
            out["name"] = name[:120]
            if len(mhz) >= 4 and mhz:
                hi = max(mhz)
                lo = min(mhz)
                pcores = [j for j, m in enumerate(mhz) if m >= (hi + lo) / 2]
                ecores = len(mhz) - len(pcores)
                spread = hi / lo if lo else 0
                name_hit = any(t in name for t in
                               ("12th Gen", "13th Gen", "14th Gen", "Core Ultra"))
                if len(pcores) < len(mhz) and spread >= 1.5:
                    out.update(hybrid=True, pcore_count=len(pcores),
                               ecore_count=ecores,
                               pcore_mask=sum(1 << j for j in pcores),
                               detail=f"two clock groups ({lo}/{hi} MHz)")
                elif name_hit:
                    # Alder/Raptor/Meteor Lake naming, but the registry clock
                    # split cannot separate the cores (overlapping turbo bins,
                    # or every ~MHz reads equal). Honesty rules: report the
                    # hybrid CPU, but mask 0 = "cannot pin safely" — pinning
                    # to EVERY logical CPU would silently do nothing.
                    sep = len(pcores) < len(mhz)
                    out.update(hybrid=True,
                               pcore_count=len(pcores) if sep else 0,
                               ecore_count=ecores if sep else 0,
                               pcore_mask=sum(1 << j for j in pcores) if sep else 0,
                               detail=("hybrid CPU name; clock split could not "
                                       "separate P/E cores — update the "
                                       "emulator to 37.3.6+ for the in-engine "
                                       "fix"))
        else:
            out["detail"] = "hybrid detection is Windows-only"
    except Exception as e:                      # never break a dashboard refresh
        out["detail"] = f"probe failed: {type(e).__name__}"
    with _HYBRID_LOCK:
        _HYBRID_CACHE = dict(out)
        return dict(_HYBRID_CACHE)


def hybrid_note(paths: ToolPaths) -> dict | None:
    """Engine-card note when this machine needs the P-core pin. None = quiet."""
    if paths.emulator:
        from . import engine as ENGINE
        ver = ENGINE.version_tuple(ENGINE.emulator_version(paths))
        if ver and ver >= (37, 3, 6):
            return None          # engine already opts itself out of throttling
    h = detect_hybrid()
    if not h.get("hybrid"):
        return None
    mask = h.get("pcore_mask") or 0
    fix = ("Fix: update the emulator to 37.3.6+ (in-engine opt-out), ")
    if mask:
        fix += (f"or Raw-boot with P-core affinity (mask 0x{mask:x}, "
                f"{h.get('pcore_count')} P-cores of {h.get('logical_cpus')}).")
    else:
        fix += ("or run instances with a visible window (only headless ones "
                "get throttled). This machine's P/E cores could not be "
                "separated by clock, so NovaDroid offers no affinity mask.")
    return {
        "level": "warn",
        "title": "Hybrid CPU: headless instances may be E-core throttled",
        "detail": ("Intel hybrid CPU detected. On engines older than "
                   "37.3.6 canary, headless emulator instances get "
                   "power-throttled onto E-cores — cold boots 2–3x slower "
                   "with ANR storms. ") + fix,
        "pcore_mask": mask,
    }


def suggest_flags(paths: ToolPaths, serial: str = "") -> dict:
    """`/api/smoothness/flags` — actually wire `runtime_flags` up.

    Prefers a fresh sample from the selected device; falls back to the last
    history entry, then to host-only defaults. Always returns usable flags so
    the "Suggest raw flags" button is useful before any measurement exists.
    """
    sample: dict = {}
    basis = "host defaults"
    if serial and paths.adb and safe.valid_serial(serial):
        s = measure(paths, serial)
        if s.get("ok"):
            sample, basis = s, "live sample"
    if not sample:
        items = history(1).get("samples") or []
        if items:
            sample, basis = items[-1], "last measurement"
    snap = HOST.snapshot()
    flags = runtime_flags(sample, snap)
    note = (f"Based on {basis}. These are ADVANCED raw-boot flags: they "
            "override this AVD's config.ini for that launch only. "
            "Never use Vulkan together with a snapshot.")
    return {"ok": True, "flags": flags, "note": note, "basis": basis,
            "sample": {"renderer": sample.get("renderer"),
                       "score": sample.get("score")}}


def autotune(paths: ToolPaths, name: str, apply: bool = False) -> dict:
    """Recommend (and optionally apply) the best config for an AVD.

    AVD should be STOPPED — the emulator rewrites config.ini on exit and will
    clobber the change if it is running.
    """
    from . import avd as AVD
    if not name or not re.fullmatch(r"[\w.\-]+", name):
        return {"ok": False, "err": "invalid AVD name"}
    cfg = AVD.avd_dir(name) / "config.ini"
    if not cfg.exists():
        return {"ok": False, "err": f"AVD not found: {name}"}
    current = inifile.parse(cfg)
    snap = HOST.snapshot()
    recs = recommend(snap, current)
    if not apply:
        return {"ok": True, "avd": name, "dry_run": True,
                "recommendations": recs, "count": len(recs),
                "note": ("Nothing written. Pass apply:true to write config.ini "
                         "(stop the AVD first).")}
    if not recs:
        return {"ok": True, "avd": name, "applied": [],
                "note": "Already optimal — nothing to change."}
    try:
        inifile.upsert(cfg, {r["key"]: r["suggested"] for r in recs})
    except OSError:
        return {"ok": False, "err": "could not write config.ini (check permissions)"}
    return {"ok": True, "avd": name, "dry_run": False, "applied": recs,
            "count": len(recs),
            "note": "Wrote config.ini — stop the AVD and boot again to feel it."}
