"""Subprocess helpers + wrappers around the `android` CLI and SDK tools.

All calls are non-blocking-safe with timeouts, hardened for Windows paths
with spaces, and return structured dicts (never raise on tool failure).
"""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Callable


def _label(cmd: list[str]) -> str:
    """Just the executable name — never echo host paths or argv to a client.

    `run()` results are forwarded into API error strings in several places. A
    timeout used to include the full command line, which leaked the operator's
    SDK path, AVD paths and APK locations across the tunnel.
    """
    try:
        return Path(str(cmd[0])).name[:80]
    except (IndexError, TypeError, ValueError):
        return "tool"


def run(cmd: list[str], timeout: int = 60) -> dict:
    """Run a command, capture output as text. Returns {ok, code, out, err}.

    Encoding is explicit: on Windows the default locale codec (cp1252) turned
    any non-ASCII tool output — an AVD name, a GPU driver string — into mojibake
    or raised UnicodeDecodeError, which the old bare `except` reported as a
    generic failure. `errors="replace"` keeps the exit code and the readable
    part of the output intact.
    """
    if not cmd or any(c is None or c == "" for c in cmd):
        return {"ok": False, "code": 1, "out": "",
                "err": "refusing to run a command with empty arguments"}
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           shell=False, encoding="utf-8", errors="replace")
        return {"ok": p.returncode == 0, "code": p.returncode,
                "out": (p.stdout or "").strip(),
                "err": (p.stderr or "").strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": 124, "out": "",
                "err": f"timeout after {timeout}s running {_label(cmd)}"}
    except FileNotFoundError:
        return {"ok": False, "code": 127, "out": "",
                "err": f"executable not found: {_label(cmd)}"}
    except Exception as e:  # defensive: never crash the server
        print(f"[runner] {_label(cmd)} failed ({type(e).__name__})")
        return {"ok": False, "code": 1, "out": "",
                "err": f"command failed ({type(e).__name__})"}


def run_bytes(cmd: list[str], timeout: int = 60) -> dict:
    """Run a command and capture RAW BYTES on stdout.

    `run()` decodes as UTF-8, which silently corrupts binary payloads. This
    is the path for `adb exec-out screencap -p` and friends: no temp files,
    no decode, one process instead of three.
    """
    if not cmd or any(c is None or c == "" for c in cmd):
        return {"ok": False, "code": 1, "out": b"", "err": "empty command"}
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, shell=False)
        return {"ok": p.returncode == 0, "code": p.returncode,
                "out": p.stdout or b"",
                "err": (p.stderr or b"").decode("utf-8", "replace").strip()}
    except subprocess.TimeoutExpired:
        return _err_bytes(cmd, f"timeout after {timeout}s")
    except FileNotFoundError:
        return _err_bytes(cmd, "executable not found")
    except Exception as e:  # defensive: never crash the server
        print(f"[runner] {_label(cmd)} failed ({type(e).__name__})")
        return {"ok": False, "code": 1, "out": b"",
                "err": f"command failed ({type(e).__name__})"}


def _err_bytes(cmd: list[str], message: str) -> dict:
    return {"ok": False, "code": 1, "out": b"",
            "err": f"{message}: {_label(cmd)}"}


def run_in_background(cmd: list[str],
                      on_done: Callable[[dict], None] | None = None,
                      timeout: int = 900) -> threading.Thread:
    """Fire-and-forget runner for `android emulator start` (blocks till boot).

    Default 900s: a first boot of a Play image provisions the store and can
    legitimately take 10+ minutes on a cold cache, which the old fixed 600s
    ceiling would kill half-way through.
    """
    def _t():
        res = run(cmd, timeout=timeout)
        if on_done:
            try:
                on_done(res)
            except Exception as ex:
                # v4 swallowed this silently: a boot job then sat at
                # "starting" forever with no log line anywhere. Report it, and
                # let the caller mark the job failed via `finally` in its own
                # callback where possible.
                print(f"[background] callback failed ({type(ex).__name__}): {ex}")
    t = threading.Thread(target=_t, daemon=True)
    t.start()
    return t


def try_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None
