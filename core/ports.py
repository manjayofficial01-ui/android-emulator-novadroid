"""Port forwarding manager — `adb forward` / `adb reverse` with a UI-shaped API.

PC↔device port plumbing is the thing every mobile developer ends up doing by
hand (`adb forward tcp:8080 tcp:8080`, then forgets which rule is live).
Genymotion ships a Network widget; the gaming emulators ship nothing. Both
directions matter:

  forward : PC → device   (hit a server running INSIDE the guest from the host)
  reverse : device → PC   (a guest app reaches a dev server on THIS machine)

v5.0.1 correctness notes (verified against AOSP `client/commandline.cpp` and
`adb_listeners.cpp::format_listeners`):

  * `adb forward LOCAL REMOTE` — the FIRST spec is the PC-side listener.
  * `adb reverse REMOTE LOCAL` — the FIRST spec is the DEVICE-side listener,
    the second is the PC-side target. The old code passed both sides in the
    forward order for both verbs, silently building inverted tunnels.
  * `adb reverse --remove REMOTE` — removes by the DEVICE-side spec.
  * `adb reverse --list` prints `(reverse) <device-side> <pc-side>` — the
    literal string `(reverse)`, not the serial, in the first column.

Rules survive until the device disconnects, so they are listed from adb each
time rather than cached.
"""
from __future__ import annotations

import re

from . import safe
from .config import ToolPaths
from .runner import run


def _check(paths: ToolPaths, serial: str) -> dict | None:
    if not paths.adb:
        return {"ok": False, "err": "adb not found"}
    if not serial:
        return {"ok": False, "err": "no device selected — Quickboot an AVD first"}
    if not safe.valid_serial(serial):
        return {"ok": False, "err": "invalid device serial"}
    return None


def _port(value, default: int = 0) -> int:
    """Parse a port. Out-of-range values are INVALID (0), never clamped —
    silently mapping 70000 -> 65535 created rules nobody asked for."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return default
    return n if 0 <= n <= 65535 else 0


def parse_rules(text: str, direction: str = "forward",
                serial: str = "") -> list[dict]:
    """Parse `adb forward --list` / `adb reverse --list` output.

    forward — one rule per line: "<serial> <local> <remote>", e.g.
        emulator-5554 tcp:8080 tcp:8080
    reverse — the serial column is the literal "(reverse)"; the columns are
    DEVICE-side then PC-side, and they are normalised here to the same
    {local: PC-side, remote: device-side} shape `add()` speaks:
        (reverse) tcp:6000 tcp:5000
    Pure function — unit-tested without a device.
    """
    rules: list[dict] = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        if direction == "reverse":
            if parts[0] != "(reverse)":
                continue
            if not parts[1].startswith(("tcp:", "localabstract:", "jdwp:")):
                continue
            rules.append({"serial": serial or "(reverse)",
                          "local": parts[2], "remote": parts[1]})
        else:
            a, b, c = parts
            if not b.startswith(("tcp:", "localabstract:", "jdwp:")):
                continue
            rules.append({"serial": a, "local": b, "remote": c})
    return rules


def forwards(paths: ToolPaths, serial: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    r = run([paths.adb, "-s", serial, "forward", "--list"], timeout=15)
    rules = [x for x in parse_rules(r["out"], "forward") if x["serial"] == serial]
    return {"ok": True, "direction": "forward", "rules": rules,
            "count": len(rules)}


def reverses(paths: ToolPaths, serial: str) -> dict:
    if (e := _check(paths, serial)):
        return e
    r = run([paths.adb, "-s", serial, "reverse", "--list"], timeout=15)
    # The output carries no serial (just "(reverse)"), but the query was
    # scoped with -s, so every line belongs to this device.
    rules = parse_rules(r["out"], "reverse", serial)
    return {"ok": True, "direction": "reverse", "rules": rules,
            "count": len(rules)}


def add(paths: ToolPaths, serial: str, direction: str = "forward",
        local: int = 0, remote: int = 0, remote_abstract: str = "") -> dict:
    """Add a tcp rule. `remote_abstract` (forward only) targets a Unix socket
    like localabstract:webview_devtools_remote_… for Chrome DevTools.

    `local` is always the PC-side port, `remote` always the device-side port,
    regardless of direction — adb's argument order differs per verb and is
    handled here so callers never have to think about it.
    """
    if (e := _check(paths, serial)):
        return e
    direction = (direction or "forward").lower()
    if direction not in ("forward", "reverse"):
        return {"ok": False, "err": "direction must be forward or reverse"}
    lcl = _port(local)
    if not lcl:
        return {"ok": False, "err": "local port must be 1–65535"}
    if remote_abstract:
        if direction != "forward":
            return {"ok": False, "err": "abstract sockets are forward-only"}
        if not re.fullmatch(r"[\w.\-]+", remote_abstract):
            return {"ok": False, "err": "invalid abstract socket name"}
        rmt = f"localabstract:{remote_abstract}"
        rmt_port = 0
    else:
        rmt_port = _port(remote)
        if not rmt_port:
            return {"ok": False, "err": "remote port must be 1–65535"}
        rmt = f"tcp:{rmt_port}"
    if direction == "forward":
        # adb forward LOCAL REMOTE
        r = run([paths.adb, "-s", serial, "forward", f"tcp:{lcl}", rmt],
                timeout=15)
    else:
        # adb reverse REMOTE LOCAL — device-side listener first.
        r = run([paths.adb, "-s", serial, "reverse", f"tcp:{rmt_port}",
                 f"tcp:{lcl}"], timeout=15)
    ok = bool(r["ok"]) and "cannot bind" not in (r["out"] + r["err"]).lower()
    return {"ok": ok, "direction": direction, "local": f"tcp:{lcl}", "remote": rmt,
            "err": "" if ok else (r["out"] + r["err"])[-300:] or "port already in use"}


def remove(paths: ToolPaths, serial: str, direction: str = "forward",
           local: int = 0, all_rules: bool = False, remote: int = 0) -> dict:
    """Remove one rule or all. For reverse rules, pass the DEVICE-side port
    as `remote` (`adb reverse --remove` addresses the remote spec); pass the
    PC-side port as `local` for forwards."""
    if (e := _check(paths, serial)):
        return e
    direction = (direction or "forward").lower()
    if direction not in ("forward", "reverse"):
        return {"ok": False, "err": "direction must be forward or reverse"}
    verb = "forward" if direction == "forward" else "reverse"
    if all_rules:
        r = run([paths.adb, "-s", serial, verb, "--remove-all"], timeout=15)
        return {"ok": bool(r["ok"]), "removed": "all",
                "err": (r["out"] + r["err"])[-200:] if not r["ok"] else ""}
    if direction == "reverse":
        rmt = _port(remote)
        if not rmt:
            return {"ok": False, "err": "which remote (device-side) port? (or all=true)"}
        spec = f"tcp:{rmt}"
    else:
        lcl = _port(local)
        if not lcl:
            return {"ok": False, "err": "which local port? (or all=true)"}
        spec = f"tcp:{lcl}"
    r = run([paths.adb, "-s", serial, verb, "--remove", spec], timeout=15)
    ok = bool(r["ok"]) and "error" not in (r["out"] + r["err"]).lower()
    return {"ok": ok, "removed": spec,
            "err": "" if ok else (r["out"] + r["err"])[-200:] or "rule not found"}
