"""One place for every input-validation rule NovaDroid relies on.

The v4 code re-implemented "is this safe?" in six modules with six slightly
different answers. That is how a dashboard that can stop VMs, install APKs and
write `.ini` files ended up with a hand-rolled shell blacklist as its security
boundary.

Rules here are deliberately conservative and *pure* (no subprocesses, no
device needed), so every one of them is unit-testable:

* `sh_quote()`  — POSIX single-quote a value before it goes into an
  `adb shell` script. `adb` joins argv with spaces and hands the result to
  `/system/bin/sh -c`, so ANY unquoted user text is remote shell code.
* `valid_*()`   — strict allowlists for names that become file paths, argv
  elements or adb serials.
* `apk_path()`  — confine user-supplied host paths to a small set of roots
  and to archive extensions, so the install/analyze endpoints cannot be used
  to probe arbitrary files on the host or echo absolute paths back.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

#: Names that become directory components under ~/.android/avd.
_AVD_RE = re.compile(r"[\w.\-]+")
#: adb serials: emulator-5554, USB serials, and host:port for TCP devices.
_SERIAL_RE = re.compile(r"[A-Za-z0-9._:\-]+")
#: Android application IDs (at least two segments, Java-ish identifiers).
_PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")
#: ALL C0 controls + DEL. The v5 first draft left \t, \n and \r out of the
#: class, which meant `sh_quote()` did not strip newlines even though its own
#: docstring promised it did. Every identifier/path/command is checked here.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
#: Free-text fields (clipboard) may legitimately contain tab/LF/CR; only the
#: remaining controls are refused there.
_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Only archive-shaped files may be handed to `adb install` / `aapt2`.
APK_EXTS = frozenset({".apk", ".apks", ".xapk", ".apkm", ".zip"})
#: Maximum size of an `environment.ini` we are willing to read or write.
MAX_INI_BYTES = 64 * 1024


def has_control(text: str) -> bool:
    """True if `text` contains NUL or any other control character."""
    return bool(_CONTROL_RE.search(str(text or "")))


def has_text_control(text: str) -> bool:
    """Control check for free text: tab/LF/CR allowed, NUL and friends not.

    Pasting a multi-line snippet into the device clipboard is a normal thing
    to do; a NUL byte is never. `_dq()` already escapes every character that
    could break out of a double-quoted remote shell word.
    """
    return bool(_TEXT_CONTROL_RE.search(str(text or "")))


def sh_quote(value) -> str:
    """Quote one value so `/system/bin/sh` sees it as a single literal word.

    Single quotes suppress every expansion (`$`, backticks, `\\`, globs,
    `;`, `|`, `&`, redirects). An embedded apostrophe ends the quoted run,
    emits an escaped apostrophe and starts a new one: `'a'\\''b'`.

    Control characters are stripped first — a newline inside a quoted word is
    legal POSIX but adb's argument joining makes it needlessly fragile.
    """
    s = _CONTROL_RE.sub("", str(value if value is not None else ""))
    return "'" + s.replace("'", "'\\''") + "'"


def valid_avd_name(name) -> bool:
    return bool(name) and not has_control(name) and bool(_AVD_RE.fullmatch(str(name)))


def valid_serial(serial) -> bool:
    return (bool(serial) and len(str(serial)) <= 128
            and not has_control(serial)
            and bool(_SERIAL_RE.fullmatch(str(serial))))


def valid_package(package) -> bool:
    return (bool(package) and len(str(package)) <= 255
            and not has_control(package)
            and bool(_PACKAGE_RE.fullmatch(str(package))))


def safe_filename(name) -> str:
    """Basename only, with path separators, control chars and `.`/`..` gone."""
    clean = _CONTROL_RE.sub("", str(name or "")).replace("\\", "/").split("/")[-1]
    return "" if clean in ("", ".", "..") else clean


def confine(base: Path, relative: str) -> Path | None:
    """Resolve `relative` under `base`, refusing anything that escapes it.

    Callers MUST use the returned path, not the original string. Symlinks are
    followed by `resolve()`, so a link inside `base` pointing outside is
    refused as well.
    """
    if not relative or has_control(relative):
        return None
    rel = str(relative).replace("\\", "/")
    if rel.startswith("/") or ":" in rel.split("/")[0] or ".." in rel.split("/"):
        return None
    try:
        root = Path(base).resolve()
        target = (root / rel).resolve()
        if target == root or root not in target.parents:
            return None
        return target
    except (OSError, RuntimeError, ValueError):
        return None


def allowed_roots() -> list[Path]:
    """Host directories an APK may be read from.

    By default: the user's home (which covers Downloads/Desktop), the shared
    folder, the OS temp dir and the working directory. Extra roots can be
    added with the NOVADROID_APK_DIRS environment variable (os.pathsep
    separated) for SDKs or build trees on another drive.
    """
    roots = [Path.home(), Path.home() / "NovaDroid-Shared",
             Path(tempfile.gettempdir()), Path.cwd()]
    extra = os.environ.get("NOVADROID_APK_DIRS", "")
    roots += [Path(p) for p in extra.split(os.pathsep) if p.strip()]
    out: list[Path] = []
    for p in roots:
        try:
            r = p.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if r not in out:
            out.append(r)
    return out


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def apk_path(raw) -> tuple[Path | None, str]:
    """Validate a user-supplied APK host path.

    Returns (path, "") on success, or (None, short_error) on failure. Errors
    deliberately contain only the basename — never echo the caller's full
    path back, and never leak whether some unrelated host file exists.
    """
    if not raw or not isinstance(raw, str):
        return None, "no APK path given"
    if has_control(raw):
        return None, "invalid APK path"
    try:
        p = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None, "invalid APK path"
    label = p.name[:80]
    if p.suffix.lower() not in APK_EXTS:
        return None, f"only {'/'.join(sorted(APK_EXTS))} files are accepted"
    try:
        if not p.is_file():
            return None, f"APK not found: {label}"
    except OSError:
        return None, f"APK not found: {label}"
    if not any(_under(p, root) for root in allowed_roots()):
        return None, ("APK must be in your user folder, the NovaDroid shared "
                      "folder, or a directory listed in NOVADROID_APK_DIRS")
    return p, ""


def memory_mb(value) -> int:
    """Parse AVD RAM/heap config values, including 37.1.11 unit suffixes.

    `6G` -> 6144, `4096M` -> 4096, `4194304K` -> 4096, `4096` -> 4096.
    Unparseable values return 0 so callers can treat them as "unknown".
    """
    s = str(value or "").strip().upper()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]?)B?", s)
    if not m:
        return 0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0
    # Bare numbers in config.ini are already megabytes.
    factor = {"": 1, "K": 1 / 1024, "M": 1,
              "G": 1024, "T": 1024 * 1024}[m.group(2)]
    return int(n * factor)
