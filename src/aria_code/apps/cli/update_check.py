"""Background version checker for the active Aria Code install channel.

Checks GitHub releases or scoped npm once per 24 hours in a daemon thread so
startup is never blocked. The result is cached in the Aria home directory and read
at banner render time.

Public API
----------
    start_update_check(current_version: str) -> None
        Call once, early in startup. Spawns daemon thread; returns immediately.

    get_update_notice() -> str | None
        Call at banner render time. Returns a Rich-markup string if a newer
        version is available, otherwise None.  Thread-safe — safe to call
        before the background thread finishes (returns cached result then).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from aria_code.packages.aria_core.paths import aria_home

_RELEASE_URL   = "https://api.github.com/repos/artheras/aria-code/releases/latest"
_NPM_URL       = "https://registry.npmjs.org/@artheras%2Faria-code/latest"
_CACHE_FILE    = aria_home() / "update_check.json"
_CACHE_TTL_S   = 86_400      # 24 hours
_FETCH_TIMEOUT = 4           # seconds — fail cleanly on slow networks

# These are the project's historical PyPI releases (June–August 2026),
# published before the current 0.x line. Do not treat arbitrary future 4.x
# versions as legacy, or recommend these old artifacts to a current install.
_LEGACY_VERSIONS = frozenset({
    (4, 1, 3), (4, 1, 4), (4, 1, 7), (4, 2, 0),
    (4, 3, 0), (4, 4, 0), (4, 4, 1), (4, 4, 2),
})

_notice: Optional[str] = None
_lock   = threading.Lock()


# ── Version comparison ────────────────────────────────────────────────────────

def _parse(v: str) -> tuple[int, int, int] | None:
    """Accept only stable project release tags, never another package's version."""
    import re
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", v.strip())
    return tuple(map(int, match.groups())) if match else None


def _newer(latest: str, current: str) -> bool:
    parsed_latest, parsed_current = _parse(latest), _parse(current)
    if parsed_latest is None or parsed_current is None:
        return False
    if parsed_current in _LEGACY_VERSIONS and parsed_latest[0] == 0:
        return True
    if parsed_latest in _LEGACY_VERSIONS and parsed_current[0] == 0:
        return False
    return parsed_latest > parsed_current


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _read_cache() -> dict:
    try:
        return json.loads(_CACHE_FILE.read_text())
    except Exception:
        return {}


def _write_cache(data: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(data))
    except Exception:
        pass


# ── Notice builder ────────────────────────────────────────────────────────────

def _install_channel() -> str:
    """Infer which update channel owns the running executable."""
    override = os.environ.get("ARIA_CODE_INSTALL_CHANNEL", "").lower()
    if override in ("npm", "native", "pip", "source"):
        return override
    executable = str(getattr(sys, "executable", "") or "").lower()
    if "node_modules" in executable and "aria" in executable:
        return "npm"
    if getattr(sys, "frozen", False):
        return "native"
    root = Path(__file__).resolve().parents[4]
    if (root / ".git").exists() and (root / "pyproject.toml").exists():
        return "source"
    return "pip"


def _update_command(channel: str, latest: str = "") -> str:
    if channel == "npm":
        return "npm install -g @artheras/aria-code@latest"
    if channel == "source":
        return "git pull && python3 -m pip install -e ."
    if channel == "pip":
        # Pinned: PyPI still lists an older 4.x numbering, so an unpinned
        # --upgrade "updates" 0.73.0 to 4.4.2, which is older code.
        return f'python3 -m pip install --upgrade "aria-code=={latest}"' if latest else \
            'python3 -m pip install --upgrade "aria-code<4"'
    if sys.platform == "win32":
        return "irm https://raw.githubusercontent.com/artheras/aria-code/main/scripts/install.ps1 | iex"
    return "curl -fsSL https://raw.githubusercontent.com/artheras/aria-code/main/scripts/install.sh | sh"


def _build_notice(latest: str, current: str, lang: str, channel: str = "native") -> str:
    latest = latest.removeprefix("v")
    current = current.removeprefix("v")
    cmd = _update_command(channel, latest) if channel == "source" or (channel == "native" and sys.platform == "win32") else "aria update"
    if lang == "zh":
        return (
            f"[yellow]⬆  新版本可用[/yellow] "
            f"[dim]v{current}[/dim] [dim]→[/dim] [bold]v{latest}[/bold]"
            f"  [dim]{cmd}[/dim]"
        )
    return (
        f"[yellow]⬆  Update available[/yellow] "
        f"[dim]v{current}[/dim] [dim]→[/dim] [bold]v{latest}[/bold]"
        f"  [dim]{cmd}[/dim]"
    )


# ── Background worker ─────────────────────────────────────────────────────────

def _worker(current: str, lang: str, channel: str = "native") -> None:
    global _notice
    # GitHub releases are the version of record for every channel but npm.
    # PyPI's own "latest" is not: it still points at the old 4.x line.
    source_url, version_field = (_NPM_URL, "version") if channel == "npm" else (_RELEASE_URL, "tag_name")

    # 1. Serve from cache if still fresh
    cache = _read_cache()
    now   = time.time()
    if cache.get("source") == source_url and cache.get("checked_at", 0) + _CACHE_TTL_S > now:
        latest = cache.get("latest", "")
        if latest and _newer(latest, current):
            with _lock:
                _notice = _build_notice(latest, current, lang, channel)
        return

    # 2. Fetch metadata for this installation channel, never a similarly named package.
    try:
        import urllib.request
        req = urllib.request.Request(
            source_url,
            headers={"Accept": "application/json", "User-Agent": "aria-code-update-check"},
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            data   = json.loads(resp.read())
            latest = data[version_field]
    except Exception:
        return   # background checks never interrupt the user's work

    # 3. Persist to cache
    if _parse(latest) is None:
        return
    _write_cache({"source": source_url, "checked_at": now, "latest": latest})

    # 4. Set notice
    if _newer(latest, current):
        with _lock:
            _notice = _build_notice(latest, current, lang, channel)


# ── Public API ────────────────────────────────────────────────────────────────

def start_update_check(current_version: str, lang: str = "en", enabled: bool = True) -> None:
    """Start background version check. Call once, early in startup."""
    global _notice
    with _lock:
        _notice = None
    if not enabled:
        return
    channel = _install_channel()
    source = _NPM_URL if channel == "npm" else _RELEASE_URL
    cache = _read_cache()
    if cache.get("source") == source and _newer(cache.get("latest", ""), current_version):
        with _lock:
            _notice = _build_notice(cache["latest"], current_version, lang, channel)
    t = threading.Thread(
        target=_worker,
        args=(current_version, lang, channel),
        daemon=True,
        name="aria-update-check",
    )
    t.start()


def get_update_notice(wait_ms: int = 0) -> Optional[str]:
    """Return Rich-markup update notice, or None if up to date / not yet known.

    Read the cached notice immediately. An explicit wait is supported for
    callers outside startup, but startup never waits for a network request.
    """
    deadline = time.monotonic() + wait_ms / 1000
    while time.monotonic() < deadline:
        with _lock:
            if _notice is not None:
                return _notice
        alive = any(t.name == "aria-update-check" for t in threading.enumerate())
        if not alive:
            break
        time.sleep(0.05)
    with _lock:
        return _notice
