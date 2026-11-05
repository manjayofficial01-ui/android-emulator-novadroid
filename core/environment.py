"""AVD camera environment — the emulator 36.6.11 "environment" camera mode.

Official release note (36.6.11 Stable, Jun 2 2026):

    Added "environment" camera mode to the Emulator for configurable virtual
    scene backgrounds. These scenes are defined in the AVD's
    environment.ini file and can be dynamically reloaded at runtime.

The emulator command-line reference confirms `-camera-back environment`
means "use the virtual scene defined in the AVD's environment configuration
file", and that this flag overrides the AVD camera setting. In `config.ini`
the equivalent property is `hw.camera.back`.

What this module deliberately does NOT do: invent an `environment.ini`
schema. Google has not published the scene key names, so we never fabricate
scene definitions. We (a) set the camera mode the emulator documents, and
(b) make sure `environment.ini` exists as a comment-only placeholder the
operator can fill in. Unknown/comment lines are ignored by ini readers, so
this is safe on every build. Existing user content is never overwritten.

Requires emulator 36.6.11+ for the `environment` value; older builds fall
back to `virtualscene`, which every Play x86_64 image supports.
"""
from __future__ import annotations

from . import inifile, safe

INI_NAME = "environment.ini"
CONFIG_NAME = "config.ini"
CAMERA_KEY = "hw.camera.back"

#: UI value -> config.ini value. `virtualscene` is the pre-36.6.11 name.
MODE_TO_CONFIG = {
    "environment": "environment",
    "virtual": "virtualscene",
    "none": "none",
}
CONFIG_TO_MODE = {v: k for k, v in MODE_TO_CONFIG.items()}
MIN_ENVIRONMENT_VERSION = (36, 6, 11)

HEADER = (
    "# NovaDroid camera environment\n"
    "#\n"
    "# Emulator 36.6.11+ reads virtual-scene backgrounds from this file when\n"
    "# `hw.camera.back=environment` (or -camera-back environment) is used.\n"
    "# Scenes can be reloaded at runtime. Add your own scene definitions\n"
    "# below; NovaDroid only manages the camera MODE in config.ini and will\n"
    "# never overwrite lines you add here.\n"
)


def _files(name: str) -> tuple:
    """(environment.ini, config.ini) for a VALID AVD name. Raises ValueError."""
    if not safe.valid_avd_name(name):
        raise ValueError("invalid AVD name")
    from . import avd as AVD
    d = AVD.avd_dir(name)
    return d / INI_NAME, d / CONFIG_NAME


def get(name: str) -> dict:
    """Report the current camera mode and whether an environment file exists."""
    try:
        env_ini, cfg = _files(name)
    except ValueError as ex:
        return {"ok": False, "err": str(ex)}
    if not cfg.is_file():
        return {"ok": False, "err": f"AVD not found: {name}"}
    config = inifile.parse(cfg)
    raw = config.get(CAMERA_KEY, "")
    mode = CONFIG_TO_MODE.get(raw, "")
    exists = env_ini.is_file()
    hint = ""
    if raw == "environment":
        hint = ("environment mode needs emulator 36.6.11+; edit "
                "environment.ini to define scenes (reloads at runtime)")
    elif raw == "virtualscene":
        hint = "legacy virtual-scene camera; switch to environment on 36.6.11+"
    elif not raw:
        hint = "no camera mode set in config.ini"
    if not exists:
        hint = (hint + " · " if hint else "") + "no environment.ini yet"
    keys: dict = {}
    if exists:
        try:
            text = env_ini.read_text(encoding="utf-8", errors="ignore")
            if len(text.encode("utf-8", "ignore")) <= safe.MAX_INI_BYTES:
                keys = inifile.parse_text(text)
        except OSError:
            keys = {}
    return {"ok": True, "avd": name, "exists": exists, "mode": mode,
            "camera": raw, "keys": keys, "hint": hint,
            "min_version": ".".join(str(n) for n in MIN_ENVIRONMENT_VERSION)}


def set_mode(name: str, mode: str) -> dict:
    """Set the AVD camera mode, creating an `environment.ini` placeholder.

    Returns a hint telling the operator to reboot the AVD (config.ini is only
    read at boot) — the emulator can hot-reload SCENES, not the camera mode.
    """
    try:
        env_ini, cfg = _files(name)
    except ValueError as ex:
        return {"ok": False, "err": str(ex)}
    mode = str(mode or "").strip().lower()
    if mode not in MODE_TO_CONFIG:
        return {"ok": False,
                "err": f"mode must be one of {sorted(MODE_TO_CONFIG)}"}
    if not cfg.is_file():
        return {"ok": False, "err": f"AVD not found: {name}"}
    try:
        if mode == "environment" and not env_ini.exists():
            env_ini.write_text(HEADER, encoding="utf-8")
        inifile.upsert(cfg, {CAMERA_KEY: MODE_TO_CONFIG[mode]})
    except OSError:
        return {"ok": False,
                "err": "could not write the AVD config (is it read-only?)"}
    hint = ("Stop and restart the AVD to apply the camera mode. "
            if mode != "none" else "")
    if mode == "environment":
        hint += ("Requires emulator 36.6.11+; define scenes in "
                 "environment.ini — they reload at runtime.")
    return {"ok": True, "avd": name, "mode": mode,
            "camera": MODE_TO_CONFIG[mode],
            "exists": env_ini.is_file(), "hint": hint}
