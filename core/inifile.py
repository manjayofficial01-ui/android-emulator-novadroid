"""Single implementation of AVD `.ini` key/value editing.

`window.py`, `display.py` and `avd.py` each used to re-implement "upsert
`key = value` into an ini file" with subtly different semantics — three
copies of one ruleset, three places for the same bug to hide (and one of
them happily rewrote commented-out keys). Everything goes through here now.

Contract, tuned to how the Android emulator actually reads these files:
  * comments (`#` / `;`) and section headers are preserved verbatim and are
    NEVER treated as a match — a commented-out `window.y` must stay a comment
  * an existing key is rewritten in place, keeping its position in the file
  * a missing key is appended at the end
  * no key is ever duplicated, no matter how many times you upsert it
  * the file always ends with exactly one trailing newline
  * OSError propagates so callers can turn it into a JSON `err`
"""
from __future__ import annotations

from pathlib import Path


def lines_of(text: str) -> list[str]:
    return text.splitlines()


def _split(line: str) -> tuple[str, str] | None:
    """Return (key, value) for an assignment line, else None.

    Rejects comment lines and lines whose key is empty or whitespace-only.
    """
    stripped = line.strip()
    if not stripped or stripped[0] in "#;":
        return None
    if "=" not in line:
        return None
    k, v = line.split("=", 1)
    k = k.strip()
    if not k:
        return None
    return k, v.strip()


def parse(path: Path) -> dict[str, str]:
    """Read an ini into a dict. Missing file -> {}. Never raises."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        return out
    for line in text.splitlines():
        kv = _split(line)
        if kv:
            out[kv[0]] = kv[1]
    return out


def get(path: Path, key: str, default: str | None = None) -> str | None:
    return parse(path).get(key, default)


def upsert(path: Path, kv: dict[str, str]) -> str:
    """Upsert `kv` into `path`, preserving comments and key order.

    Returns the resulting file text (handy for assertions). Raises OSError
    if the file cannot be written — callers decide how to report that.
    """
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()

    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        kvline = _split(line)
        if kvline and kvline[0] in kv:
            out.append(f"{kvline[0]}={kv[kvline[0]]}")
            seen.add(kvline[0])
        else:
            out.append(line)

    for k, v in kv.items():
        if k not in seen:
            out.append(f"{k}={v}")

    text = "\n".join(out).rstrip("\n") + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def delete(path: Path, keys: list[str]) -> str:
    """Remove keys (and their comment-free assignment lines) from `path`."""
    if not path.exists():
        return ""
    wanted = set(keys)
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        kv = _split(line)
        if kv and kv[0] in wanted:
            continue
        out.append(line)
    text = "\n".join(out).rstrip("\n") + "\n"
    path.write_text(text, encoding="utf-8")
    return text
