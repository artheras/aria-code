"""Candidates for an `@` file reference, the way Claude Code and Codex offer them.

Typing `@` in a prompt names a file or folder. The old completion listed one
directory at a time, like a shell, which makes a file three levels down take
three Tabs. This searches the whole workspace instead: `@auth` finds
`src/api/auth.py`, `@rdme` finds `README.md`, and `@src/` lists what is in
`src/`. Files the project ignores (`.gitignore`) and build clutter are left out.

The result is a list of whole replacement texts: the prompt as typed, with the
`@token` replaced by one candidate. That is the shape the Go runtime has always
answered with, so the frontend has one way to read both.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: How many candidates a request returns.
LIMIT = 8

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", ".cache", "site-packages",
}
_MAX_FILES = 20000
_TTL_SECONDS = 10.0
_cache: Dict[str, Tuple[float, List[str]]] = {}


def _git_files(root: Path) -> Optional[List[str]]:
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True, timeout=3, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [p for p in out.decode("utf-8", "replace").split("\0") if p][:_MAX_FILES]


def _walk_files(root: Path) -> List[str]:
    found: List[str] = []
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        base = Path(current).relative_to(root)
        for name in sorted(names):
            if name.startswith(".") and name not in (".gitignore", ".env.example"):
                continue
            found.append((base / name).as_posix())
            if len(found) >= _MAX_FILES:
                return found
    return found


def workspace_files(root: Path, now: Optional[float] = None) -> List[str]:
    """Every file in the workspace (relative, `/`-separated), cached for a few seconds."""
    key = str(root)
    stamp = time.monotonic() if now is None else now
    hit = _cache.get(key)
    if hit and stamp - hit[0] < _TTL_SECONDS:
        return hit[1]
    files = _git_files(root)
    if files is None:
        files = _walk_files(root)
    _cache[key] = (stamp, files)
    return files


def _token(text: str) -> Optional[Tuple[int, str]]:
    """Where the `@token` starts and what has been typed of it, or None."""
    start = text.rfind("@")
    if start < 0 or (start > 0 and text[start - 1] not in " \t\n"):
        return None
    token = text[start + 1:]
    if token.startswith("file:"):
        token = token[5:]
    token = token.lstrip('"')
    if any(c in token for c in "\n\r\t"):
        return None
    return start, token


def _is_subsequence(needle: str, hay: str) -> bool:
    it = iter(hay)
    return all(c in it for c in needle)


def _children(files: List[str], prefix: str) -> List[str]:
    """What sits directly inside `prefix` ('' for the top level): folders end in `/`."""
    seen: Dict[str, None] = {}
    for path in files:
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        if not rest:
            continue
        head, sep, _ = rest.partition("/")
        seen[prefix + head + ("/" if sep else "")] = None
    entries = list(seen)
    return sorted(entries, key=lambda e: (not e.endswith("/"), e.lower()))


def _search(files: List[str], query: str) -> List[str]:
    q = query.lower()
    dirs: Dict[str, None] = {}
    for path in files:
        parts = path.split("/")
        for i in range(1, len(parts)):
            dirs["/".join(parts[:i]) + "/"] = None
    scored: List[Tuple[int, int, str]] = []
    for path in [*files, *dirs]:
        low = path.lower()
        bare = low.rstrip("/")
        base = bare.rsplit("/", 1)[-1]
        if base.startswith(q):
            tier = 0
        elif low.startswith(q):
            tier = 1
        elif q in base:
            tier = 2
        elif q in low:
            tier = 3
        elif _is_subsequence(q, base):
            tier = 4
        elif _is_subsequence(q, low):
            tier = 5
        else:
            continue
        scored.append((tier, len(path), path))
    scored.sort()
    return [path for _, _, path in scored]


def reference_candidates(text: str, root: Path, limit: int = LIMIT) -> List[str]:
    """Replacement prompts for the `@token` at the end of `text`, best first."""
    found = _token(text)
    if found is None:
        return []
    start, token = found
    files = workspace_files(root)
    if token == "" or token.endswith("/"):
        paths = _children(files, token)
    else:
        paths = _search(files, token)
    out: List[str] = []
    for path in paths[:limit]:
        shown = f'"{path}"' if " " in path else path
        out.append(f"{text[:start]}@{shown}")
    return out
