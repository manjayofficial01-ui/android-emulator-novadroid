"""Subprocess helpers + wrappers around the `android` CLI and SDK tools.

All calls are non-blocking-safe with timeouts, hardened for Windows paths
with spaces, and return structured dicts (never raise on tool failure).
"""
from __future__ import annotations

import json
import subprocess
import threading
from typing import Callable


def run(cmd: list[str], timeout: int = 60) -> dict:
    """Run a command, capture output. Returns {ok, code, out, err}."""
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


def run_in_background(cmd: list[str],
                      on_done: Callable[[dict], None] | None = None) -> threading.Thread:
    """Fire-and-forget runner for `android emulator start` (blocks till boot)."""
    def _t():
        res = run(cmd, timeout=600)
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
