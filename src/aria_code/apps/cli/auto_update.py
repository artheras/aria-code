"""Install a verified update in the background, the way Claude Code does.

The session keeps running on the version it started with. When a newer native
release is out, it is downloaded, checked and switched in behind the scenes,
and the header says "restart to use it". Nothing is interrupted: the new
version only takes over the next time Aria starts.

Only managed native installs update themselves. pip, npm and source checkouts
belong to their own tools, so they keep the plain "update available" notice.
Turn it off with `auto_update: false` in the config or `ARIA_AUTO_UPDATE=0`.
A version that failed to install is not retried for a day, and two sessions
never install at once.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

RETRY_AFTER_FAILURE_S = 86_400
LOCK_STALE_S = 900


def enabled(config: dict) -> bool:
    if os.environ.get("ARIA_AUTO_UPDATE", "").lower() in {"0", "false", "no", "off"}:
        return False
    if os.environ.get("ARIA_OFFLINE", "").lower() in {"1", "true", "yes", "on"}:
        return False
    return bool(config.get("auto_update", True))


def _state_path(app_root: Path) -> Path:
    return app_root / "auto-update.json"


def _recently_failed(app_root: Path, version: str, now: float) -> bool:
    try:
        data = json.loads(_state_path(app_root).read_text())
    except (OSError, ValueError):
        return False
    return data.get("failed") == version and now - float(data.get("at", 0)) < RETRY_AFTER_FAILURE_S


def _remember_failure(app_root: Path, version: str, now: float) -> None:
    try:
        _state_path(app_root).write_text(json.dumps({"failed": version, "at": now}))
    except OSError:
        pass


def _take_lock(app_root: Path, now: float) -> Optional[Path]:
    lock = app_root / "auto-update.lock"
    try:
        if lock.exists() and now - lock.stat().st_mtime > LOCK_STALE_S:
            lock.unlink()
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return None
    os.close(fd)
    return lock


def install_if_newer(
    current: str,
    channel: str,
    config: dict,
    *,
    check: Callable[..., tuple],
    install: Callable[[str, dict], Path],
    newer: Callable[[str, str], bool],
    app_root: Path,
    now: Optional[float] = None,
) -> Optional[str]:
    """Install the latest release when there is one; return its version, else None.

    Blocking: run it off the event loop. Any failure leaves the installed
    version untouched and returns None.
    """
    if channel != "native" or not enabled(config):
        return None
    stamp = time.time() if now is None else now
    try:
        app_root.mkdir(parents=True, exist_ok=True)
        latest, release = check("native")
        if not newer(latest, current) or _recently_failed(app_root, latest, stamp):
            return None
        lock = _take_lock(app_root, stamp)
        if lock is None:
            return None
        try:
            install(latest, release)
        except Exception:
            _remember_failure(app_root, latest, stamp)
            return None
        finally:
            lock.unlink(missing_ok=True)
        return latest
    except Exception:
        return None
