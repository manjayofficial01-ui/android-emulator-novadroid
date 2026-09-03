"""Tablet & display control for NovaDroid.

Two layers (industry practice, same as Genymotion device recipes):
 1. LIVE (no reboot): `adb shell wm size` / `wm density` + rotation settings.
    Applies instantly to a running device — perfect for trying tablet layouts.
 2. PERSISTED (needs stopped AVD): writes hw.lcd.width/height/density into the
    AVD config.ini + optional device skin frame (pixel_tablet, nexus_9...).

Presets mirror real best-selling tablets so UI/layout QA matches hardware.
"""
from __future__ import annotations

from pathlib import Path

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


def get_display(paths: ToolPaths, serial: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    size = run([paths.adb, "-s", serial, "shell", "wm", "size"], timeout=15)
    dens = run([paths.adb, "-s", serial, "shell", "wm", "density"], timeout=15)
    rot = run([paths.adb, "-s", serial, "shell",
               "settings", "get", "system", "user_rotation"], timeout=10)
    return {"ok": True, "size": size["out"][:200],
            "density": dens["out"][:200], "rotation": rot["out"][:50]}


def _validate(w: int, h: int, dpi: int) -> str | None:
    if not (320 <= w <= 7680 and 320 <= h <= 7680):
        return "width/height must each be 320–7680px"
    if not (120 <= dpi <= 960):
        return "density must be 120–960 dpi"
    return None


def apply_display(paths: ToolPaths, serial: str, w: int, h: int, dpi: int) -> dict:
    """Live resize — no reboot. Mirrors Genymotion's runtime resolution switch."""
    if (e := _check(paths, serial)):
        return e
    try:
        w, h, dpi = int(w), int(h), int(dpi)
    except (TypeError, ValueError):
        return {"ok": False, "err": "w/h/dpi must be integers"}
    if (err := _validate(w, h, dpi)):
        return {"ok": False, "err": err}
    r1 = run([paths.adb, "-s", serial, "shell", "wm", "size", f"{w}x{h}"], timeout=15)
    r2 = run([paths.adb, "-s", serial, "shell", "wm", "density", str(dpi)], timeout=15)
    ok = r1["ok"] and r2["ok"]
    return {"ok": ok, "size": r1["out"][:200] or r1["err"][-200:],
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


def save_to_avd(name: str, w: int, h: int, dpi: int) -> dict:
    """Persist resolution into AVD config.ini. AVD must be STOPPED."""
    try:
        w, h, dpi = int(w), int(h), int(dpi)
    except (TypeError, ValueError):
        return {"ok": False, "err": "w/h/dpi must be integers"}
    if (err := _validate(w, h, dpi)):
        return {"ok": False, "err": err}
    cfg = Path.home() / ".android" / "avd" / f"{name}.avd" / "config.ini"
    if not cfg.exists():
        return {"ok": False, "err": f"AVD not found: {name}"}
    kv = {"hw.lcd.width": str(w), "hw.lcd.height": str(h), "hw.lcd.density": str(dpi)}
    lines, seen = [], set()
    for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" in line and line.split("=", 1)[0].strip() in kv:
            k = line.split("=", 1)[0].strip()
            lines.append(f"{k}={kv[k]}")
            seen.add(k)
        else:
            lines.append(line)
    for k, v in kv.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
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
    safe = "".join(c for c in skin if c.isalnum() or c in "_-")
    if not paths.sdk or not (paths.sdk / "skins" / safe).exists():
        return {"ok": False, "err": f"skin not shipped in SDK: {skin}"}
    cfg = Path.home() / ".android" / "avd" / f"{name}.avd" / "config.ini"
    if not cfg.exists():
        return {"ok": False, "err": f"AVD not found: {name}"}
    kv = {"skin.name": safe, "skin.path": str(paths.sdk / "skins" / safe),
          "skin.dynamic": "yes"}
    lines, seen = [], set()
    for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" in line and line.split("=", 1)[0].strip() in kv:
            k = line.split("=", 1)[0].strip()
            lines.append(f"{k}={kv[k]}")
            seen.add(k)
        else:
            lines.append(line)
    for k, v in kv.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True, "avd": name, "skin": safe}
