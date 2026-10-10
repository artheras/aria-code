"""Project-graph imports from the native ``aria-native index imports``.

Parsing every Python file's imports is most of the time a graph build takes
(``ast.parse`` and ``ast.walk`` per file). The Rust ``aria-graph`` crate does
the same resolution in parallel; ``project_graph`` asks it first and keeps the
Python implementation for everything it does not answer:

* no binary — ``ARIA_NATIVE_BINARY`` or ``aria-native`` on ``PATH`` — or
  ``ARIA_NATIVE_GRAPH=off``;
* too few files to be worth a process (``_MIN_FILES``);
* the binary failing, timing out or answering out of shape;
* a file its parser rejects (``fallback``), which Python then parses itself.

The two implementations must agree; ``tests/test_native_index.py`` holds them
to it on this repository and on edge cases.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Sequence

_MIN_FILES = 32
_TIMEOUT = 120


def native_binary() -> Optional[str]:
    configured = os.environ.get("ARIA_NATIVE_BINARY", "").strip()
    if configured:
        return configured if Path(configured).is_file() else None
    return shutil.which("aria-native")


def enabled() -> bool:
    return os.environ.get("ARIA_NATIVE_GRAPH", "").strip().lower() not in ("0", "off", "false", "no")


def resolve_imports(root: Path, files: Sequence[tuple[str, str]], parse: Sequence[str],
                    *, minimum: Optional[int] = None) -> Optional[tuple[dict[str, list[str]], list[str]]]:
    """``(imports by path, paths to parse in Python)``, or None to do it all in Python."""
    if len(parse) < (_MIN_FILES if minimum is None else minimum) or not enabled():
        return None
    binary = native_binary()
    if not binary:
        return None
    request = json.dumps({"root": str(root), "files": [list(f) for f in files], "parse": list(parse)})
    try:
        done = subprocess.run([binary, "index", "imports"], input=request, capture_output=True,
                              text=True, encoding="utf-8", timeout=_TIMEOUT, check=False)
        if done.returncode != 0:
            return None
        data = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    imports, fallback = data.get("imports"), data.get("fallback")
    if not isinstance(imports, dict) or not isinstance(fallback, list):
        return None
    wanted = set(parse)
    if not set(imports) | set(fallback) >= wanted:   # every file answered one way or the other
        return None
    return ({path: list(found) for path, found in imports.items() if path in wanted},
            [path for path in fallback if path in wanted])
