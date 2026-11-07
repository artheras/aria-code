"""Aria Code package facade layer.

This namespace is intentionally thin. It exposes stable package boundaries
without forcing the existing single-file CLI to move all implementation code at
once.
"""

# Installed modules are authoritative. A legacy private source bridge requires
# an explicit opt-in; nearby checkouts must never change import resolution.
import os as _os
from pathlib import Path as _Path

_configured_root = _os.getenv("ARTHERA_ROOT", "").strip()
if _configured_root:
    _external = _Path(_configured_root).expanduser() / "packages"
    if _external.is_dir() and str(_external) not in __path__:
        __path__.append(str(_external))

__all__ = [
    "aria_agents",
    "aria_core",
    "aria_infra",
    "aria_mcp",
    "aria_skills",
    "aria_tools",
]
