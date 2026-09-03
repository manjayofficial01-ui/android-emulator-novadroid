"""Tablet & display control for NovaDroid.

Two layers (industry practice, same as Genymotion device recipes):
 1. LIVE (no reboot): `adb shell wm size` / `wm density` + rotation settings.
    Applies instantly to a running device — perfect for trying tablet layouts.
 2. PERSISTED (needs stopped AVD): writes hw.lcd.width/height/density into the
    AVD config.ini + optional device skin frame (pixel_tablet, nexus_9...).

Presets mirror real best-selling tablets so UI/layout QA matches hardware.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import inifile
from .config import ToolPaths
from .runner import run

# width x height x density — landscape-first, like real tablets held wide.
TABLET_PRESETS = [
    {"id": "pixel_tablet_11", "label": "Pixel Tablet 11\"",
     "w": 2560, "h": 1600, "dpi": 276, "note": "Google, 16:10"},
    {"id": "tab_s9_11", "label": "Galaxy Tab S9 11\"",
     "w": 2560, "h": 1600, "dpi": 274, "note": "Samsung AMOLED, 16:10"},
    {"id": "tab_s7plus_12", "label": "Galaxy Tab 12.4\"",
     "w": 2800, "h": 1752, "dpi": 266, "note": "Large canvas, 16:10"},
    {"id": "nexus9_89", "label": "Nexus 9 8.9\"",
     "w": 2048, "h": 1536, "dpi": 288, "note": "Classic 4:3"},
    {"id": "tab_m8_8", "label": "8\" HD tablet",
     "w": 1280, "h": 800, "dpi": 213, "note": "Budget / kids tablet"},
    {"id": "wuxga_105", "label": "10.5\" WUXGA",
     "w": 1920, "h": 1200, "dpi": 240, "note": "Most common mid-range"},
]

PHONE_PRESETS = [
    {"id": "pixel_9_63", "label": "Pixel 9 6.3\"",
     "w": 1080, "h": 2424, "dpi": 420, "note": "Baseline phone"},
    {"id": "pixel_9pro_xl_68", "label": "Pixel 9 Pro XL 6.8\"",
     "w": 1344, "h": 2992, "dpi": 490, "note": "Tall flagship"},
    {"id": "pixel_fold_open", "label": "Pixel Fold (open)",
     "w": 2208, "h": 1840, "dpi": 380, "note": "Foldable inner screen"},
]

# rotation: 0 portrait, 1 landscape, 2 reverse-portrait, 3 seascape
ORIENTATIONS = {"portrait": 0, "landscape": 1,
                "reverse_portrait": 2, "seascape": 3, "auto": -1}


def presets() -> dict:
    return {"ok": True, "tablets": TABLET_PRESETS,
            "phones": PHONE_PRESETS, "orientations": sorted(ORIENTATIONS)}


def _check(paths: ToolPaths, serial: str) -> dict | None:
    if not paths.adb:
        return {"ok": False, "err": "adb not found"}
    if not serial:
        return {"ok": False, "err": "no device selected — Quickboot an AVD first"}
    return None


def _validate(w: int, h: int, dpi: int) -> str | None:
    if not (320 <= w <= 7680 and 320 <= h <= 7680):
        return "width/height must each be 320–7680px"
    if not (120 <= dpi <= 960):
        return "density must be 120–960 dpi"
    return None


def _ints(w, h, dpi) -> tuple[int, int, int] | str:
    try:
        return int(w), int(h), int(dpi)
    except (TypeError, ValueError):
        return "w/h/dpi must be integers"


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------
def get_display(paths: ToolPaths, serial: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    # One round-trip instead of three: each `adb shell` costs ~150ms, which
    # the dashboard pays on every interactive-tap coordinate mapping.
    r = run([paths.adb, "-s", serial, "shell",
             "wm size; echo ---; wm density; echo ---; "
             "settings get system user_rotation; echo ---; "
             "settings get system accelerometer_rotation"], timeout=20)
    parts = r["out"].split("---")
    size = parts[0].strip() if parts else ""
    dens = parts[1].strip() if len(parts) > 1 else ""
    rot = parts[2].strip() if len(parts) > 2 else ""
    auto = parts[3].strip() if len(parts) > 3 else ""
    w, h = parse_wm_size(size)
    return {"ok": True, "size": size[:200], "density": dens[:200],
            "rotation": rot[:50], "auto_rotate": auto == "1",
            "w": w, "h": h, "dpi": parse_wm_density(dens)}


def parse_wm_size(text: str) -> tuple[int, int]:
    """Extract the *effective* size from `wm size` output.

    `wm size` prints both, and the override is what apps actually see:
        Physical size: 1080x2424
        Override size: 2560x1600
    """
    override = re.search(r"Override size:\s*(\d+)x(\d+)", text or "")
    if override:
        return int(override.group(1)), int(override.group(2))
    physical = re.search(r"Physical size:\s*(\d+)x(\d+)", text or "")
    if physical:
        return int(physical.group(1)), int(physical.group(2))
    bare = re.search(r"(\d{3,5})x(\d{3,5})", text or "")
    return (int(bare.group(1)), int(bare.group(2))) if bare else (0, 0)


def parse_wm_density(text: str) -> int:
    m = re.search(r"Override density:\s*(\d+)", text or "")
    if m:
        return int(m.group(1))
    m = re.search(r"Physical density:\s*(\d+)", text or "")
    return int(m.group(1)) if m else 0


def size_tuple(paths: ToolPaths, serial: str) -> tuple[int, int]:
    """(w, h) the device is currently rendering at."""
    if (e := _check(paths, serial)):
        return (0, 0)
    r = run([paths.adb, "-s", serial, "shell", "wm size"], timeout=15)
    return parse_wm_size(r["out"])


# --------------------------------------------------------------------------
# Live (no reboot)
# --------------------------------------------------------------------------
def apply_display(paths: ToolPaths, serial: str, w: int, h: int, dpi: int) -> dict:
    """Live resize — no reboot. Mirrors Genymotion's runtime resolution switch."""
    if (e := _check(paths, serial)):
        return e
    parsed = _ints(w, h, dpi)
    if isinstance(parsed, str):
        return {"ok": False, "err": parsed}
    w, h, dpi = parsed
    if (err := _validate(w, h, dpi)):
        return {"ok": False, "err": err}
    r1 = run([paths.adb, "-s", serial, "shell", "wm", "size", f"{w}x{h}"], timeout=15)
    r2 = run([paths.adb, "-s", serial, "shell", "wm", "density", str(dpi)], timeout=15)
    ok = r1["ok"] and r2["ok"]
    return {"ok": ok, "w": w, "h": h, "dpi": dpi,
            "size": r1["out"][:200] or r1["err"][-200:],
            "density": r2["out"][:200] or r2["err"][-200:],
            "note": "Live only — use Save-to-AVD to survive reboot."}


def reset_display(paths: ToolPaths, serial: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    r1 = run([paths.adb, "-s", serial, "shell", "wm", "size", "reset"], timeout=15)
    r2 = run([paths.adb, "-s", serial, "shell", "wm", "density", "reset"], timeout=15)
    return {"ok": r1["ok"] and r2["ok"],
            "log": ((r1["out"] + r2["out"]) or (r1["err"] + r2["err"]))[:400]}


def set_orientation(paths: ToolPaths, serial: str, mode: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    mode = (mode or "").lower()
    if mode not in ORIENTATIONS:
        return {"ok": False, "err": f"mode must be one of {sorted(ORIENTATIONS)}"}
    if mode == "auto":
        r = run([paths.adb, "-s", serial, "shell", "settings", "put",
                 "system", "accelerometer_rotation", "1"], timeout=10)
        return {"ok": r["ok"], "mode": "auto"}
    run([paths.adb, "-s", serial, "shell", "settings", "put",
         "system", "accelerometer_rotation", "0"], timeout=10)
    r = run([paths.adb, "-s", serial, "shell", "settings", "put",
             "system", "user_rotation", str(ORIENTATIONS[mode])], timeout=10)
    return {"ok": r["ok"], "mode": mode,
            "note": "If the app forces orientation, this only affects launcher/system UI."}


# --------------------------------------------------------------------------
# Persisted (AVD must be stopped)
# --------------------------------------------------------------------------
def save_to_avd(name: str, w: int, h: int, dpi: int) -> dict:
    """Persist resolution into AVD config.ini. AVD must be STOPPED."""
    parsed = _ints(w, h, dpi)
    if isinstance(parsed, str):
        return {"ok": False, "err": parsed}
    w, h, dpi = parsed
    if (err := _validate(w, h, dpi)):
        return {"ok": False, "err": err}
    if not re.fullmatch(r"[\w.\-]+", name or ""):
        return {"ok": False, "err": "invalid AVD name"}
    cfg = (Path.home() / ".android" / "avd" / f"{name}.avd" / "config.ini")
    if not cfg.exists():
        return {"ok": False, "err": f"AVD not found: {name}"}
    try:
        inifile.upsert(cfg, {"hw.lcd.width": str(w), "hw.lcd.height": str(h),
                             "hw.lcd.density": str(dpi)})
    except OSError as e:
        return {"ok": False, "err": f"could not write config.ini: {e}"}
    return {"ok": True, "avd": name, "display": f"{w}x{h} @ {dpi}dpi",
            "note": "Takes effect on next (cold or quick) boot. Stop the AVD first."}


def list_skins(paths: ToolPaths) -> dict:
    """Device frames shipped with the SDK (pixel_tablet, nexus_9, folds...)."""
    if not paths.sdk:
        return {"ok": False, "skins": [], "err": "SDK not found"}
    d = paths.sdk / "skins"
    skins = sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []
    tabletish = [s for s in skins if any(k in s for k in
                 ("tablet", "tab", "nexus_9", "nexus_10", "fold", "pixel_c"))]
    return {"ok": True, "skins": skins, "tablet_skins": tabletish}


def set_skin(paths: ToolPaths, name: str, skin: str) -> dict:
    """Apply a device frame to a STOPPED AVD."""
    safe = "".join(c for c in (skin or "") if c.isalnum() or c in "_-")
    if not paths.sdk or not (paths.sdk / "skins" / safe).exists():
        return {"ok": False, "err": f"skin not shipped in SDK: {skin}"}
    if not re.fullmatch(r"[\w.\-]+", name or ""):
        return {"ok": False, "err": "invalid AVD name"}
    cfg = Path.home() / ".android" / "avd" / f"{name}.avd" / "config.ini"
    if not cfg.exists():
        return {"ok": False, "err": f"AVD not found: {name}"}
    try:
        inifile.upsert(cfg, {"skin.name": safe,
                             "skin.path": str(paths.sdk / "skins" / safe),
                             "skin.dynamic": "yes"})
    except OSError as e:
        return {"ok": False, "err": f"could not write config.ini: {e}"}
    return {"ok": True, "avd": name, "skin": safe}
