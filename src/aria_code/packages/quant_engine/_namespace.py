"""Explicit legacy backend opt-in; never search a developer's filesystem.

Standalone financial-service over MCP is the normal integration. Existing
private integrations may deliberately set ARTHERA_ROOT until their public API
cutover. Bundled modules keep precedence over that configured source.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List


def extend(package_path: List[str], subpackage: str) -> None:
    configured = os.getenv("ARTHERA_ROOT", "").strip()
    if not configured:
        return
    candidate = Path(configured).expanduser() / "packages" / "quant_engine" / subpackage
    try:
        if candidate.is_dir() and str(candidate) not in package_path:
            package_path.append(str(candidate))
    except OSError:
        return
