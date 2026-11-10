"""Project-graph imports and Python definitions from ``aria-native index``.

Parsing every Python file twice — ``repo_map`` for its definitions,
``project_graph`` for its imports, each an ``ast.parse`` and a walk — is most
of the time a graph build takes. The Rust ``aria-graph`` crate does both in
parallel; the callers ask it first and keep the Python implementation for
everything it does not answer:

* no binary — ``ARIA_NATIVE_BINARY`` or ``aria-native`` on ``PATH`` — or
  ``ARIA_NATIVE_GRAPH=off``;
* too few files to be worth a process (``_MIN_FILES``);
* the binary failing, timing out or answering out of shape;
* a file its parser rejects or cannot read (``fallback``), which Python then
  handles itself.

The two implementations must agree; ``tests/test_native_index.py`` holds them
to it on this repository and on edge cases.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

_MIN_FILES = 32
_TIMEOUT = 120


def native_binary() -> Optional[str]:
    """``ARIA_NATIVE_BINARY``, else the copy bundled with a native build or a
    platform wheel, else ``PATH``."""
    configured = os.environ.get("ARIA_NATIVE_BINARY", "").strip()
    if configured:
        return configured if Path(configured).is_file() else None
    name = "aria-native.exe" if sys.platform == "win32" else "aria-native"
    if getattr(sys, "frozen", False):
        # go/buildtools puts it in the PyInstaller contents
        # folder (_internal/), which is sys._MEIPASS at run time.
        bundled = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / name
        if bundled.is_file():
            return str(bundled)
    # A platform wheel (scripts/platform_wheels.py) carries it, with the Go
    # engine beside it, in aria_code/_bin/.
    packaged = Path(__file__).resolve().parents[1] / "_bin" / name
    if packaged.is_file() and (sys.platform == "win32" or os.access(packaged, os.X_OK)):
        return str(packaged)
    return shutil.which("aria-native")


def enabled() -> bool:
    return os.environ.get("ARIA_NATIVE_GRAPH", "").strip().lower() not in ("0", "off", "false", "no")


def _call(command: str, payload: dict, wanted: Sequence[str], key: str,
          minimum: Optional[int]) -> Optional[tuple[dict, list[str]]]:
    """``(answers by path, paths left to Python)`` from ``aria-native index <command>``, or None."""
    if len(wanted) < (_MIN_FILES if minimum is None else minimum) or not enabled():
        return None
    binary = native_binary()
    if not binary:
        return None
    try:
        done = subprocess.run([binary, "index", command], input=json.dumps(payload), capture_output=True,
                              text=True, encoding="utf-8", timeout=_TIMEOUT, check=False)
        if done.returncode != 0:
            return None
        data = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    answers, fallback = data.get(key), data.get("fallback")
    if not isinstance(answers, dict) or not isinstance(fallback, list):
        return None
    paths = set(wanted)
    if not set(answers) | set(fallback) >= paths:   # every file answered one way or the other
        return None
    return ({path: answer for path, answer in answers.items() if path in paths},
            [path for path in fallback if path in paths])


def resolve_imports(root: Path, files: Sequence[tuple[str, str]], parse: Sequence[str],
                    *, minimum: Optional[int] = None) -> Optional[tuple[dict[str, list[str]], list[str]]]:
    """``(imports by path, paths to parse in Python)``, or None to do it all in Python."""
    payload = {"root": str(root), "files": [list(f) for f in files], "parse": list(parse)}
    result = _call("imports", payload, parse, "imports", minimum)
    if result is None:
        return None
    imports, fallback = result
    return {path: list(found) for path, found in imports.items()}, fallback


def python_symbols(root: Path, parse: Sequence[str], *, minimum: Optional[int] = None,
                   ) -> Optional[tuple[dict[str, list[tuple[str, str, int, str]]], list[str]]]:
    """``(definitions by path as (name, kind, line, parent), paths for Python)``, or None."""
    result = _call("symbols", {"root": str(root), "parse": list(parse)}, parse, "symbols", minimum)
    if result is None:
        return None
    symbols, fallback = result
    try:
        return ({path: [(str(n), str(k), int(line), str(p)) for n, k, line, p in found]
                 for path, found in symbols.items()}, fallback)
    except (TypeError, ValueError):
        return None


UNAVAILABLE = object()


def _raw(command: str, payload: dict) -> Optional[dict]:
    """JSON from ``aria-native index <command>``, or None when it cannot answer."""
    if not enabled():
        return None
    binary = native_binary()
    if not binary:
        return None
    try:
        done = subprocess.run(
            [binary, "index", command], input=json.dumps(payload), capture_output=True,
            text=True, encoding="utf-8", timeout=_TIMEOUT, check=False,
        )
        if done.returncode != 0:
            return None
        data = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return data if isinstance(data, dict) else None


def reverse_apply(after: str, diff: str):
    """The text *diff* was made from, None when it does not fit, or UNAVAILABLE."""
    data = _raw("changes", {"op": "reverse", "after": after, "diff": diff})
    if data is None or "applied" not in data:
        return UNAVAILABLE
    if not data.get("applied"):
        return None
    text = data.get("text")
    return text if isinstance(text, str) else UNAVAILABLE


def symbol_changes(before: str, after: str, language: str) -> Optional[list[tuple[str, str, str]]]:
    """``(kind, qualified name, symbol kind)`` rows, or None to decide in Python."""
    data = _raw("changes", {"op": "changes", "before": before, "after": after, "language": language})
    rows = data.get("changes") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    try:
        return [(str(row["kind"]), str(row["name"]), str(row["symbol_kind"])) for row in rows]
    except (KeyError, TypeError):
        return None
