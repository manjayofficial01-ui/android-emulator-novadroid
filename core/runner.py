"""Subprocess helpers + wrappers around the `android` CLI and SDK tools.

All calls are non-blocking-safe with timeouts, hardened for Windows paths
with spaces, and return structured dicts (never raise on tool failure).
"""
from __future__ import annotations

import json
import subprocess
import threading
from typing import Callable


def _err(cmd: list[str], message: str, code: int) -> dict:
    return {"ok": False, "code": code, "out": "",
            "err": f"{message}: {' '.join(str(c) for c in cmd)}"}


def run(cmd: list[str], timeout: int = 60) -> dict:
    """Run a command, capture output as text. Returns {ok, code, out, err}."""
    if not cmd or any(c is None or c == "" for c in cmd):
        return {"ok": False, "code": 1, "out": "",
                "err": "refusing to run a command with empty arguments"}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, shell=False)
        return {"ok": p.returncode == 0, "code": p.returncode,
                "out": (p.stdout or "").strip(),
                "err": (p.stderr or "").strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": 124, "out": "",
                "err": f"timeout after {timeout}s: {' '.join(cmd)}"}
    except FileNotFoundError:
        return {"ok": False, "code": 127, "out": "",
                "err": f"executable not found: {cmd[0]}"}
    except Exception as e:  # defensive: never crash the server
        return {"ok": False, "code": 1, "out": "", "err": str(e)}


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
        return {"ok": False, "code": 1, "out": b"", "err": str(e)}


def _err_bytes(cmd: list[str], message: str) -> dict:
    return {"ok": False, "code": 1, "out": b"",
            "err": f"{message}: {' '.join(str(c) for c in cmd)}"}


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
            except Exception:
                pass
    t = threading.Thread(target=_t, daemon=True)
    t.start()
    return t


def parse_kv_block(text: str) -> dict:
    d: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            d[k.strip()] = v.strip()
    return d


def try_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None
