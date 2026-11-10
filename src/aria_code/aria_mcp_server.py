#!/usr/bin/env python3
"""Entry point for the aria-code MCP server.

``aria-code-mcp`` serves Aria's tools with the Go engine (``aria-runtime
mcp``) when one is installed beside it: in a platform wheel
(``aria_code/_bin/``), next to a frozen native build, or on ``PATH``. The Go
server has the core, media, artifact and transcription tools. Finance tools
live in the Arthera financial-service and broker tools in arthera-trading,
both MCP servers of their own.

``ARIA_MCP_ENGINE=python`` keeps the previous Python server
(packages/aria_mcp/server.py), as does an install without the Go engine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

_NAME = "aria-runtime.exe" if sys.platform == "win32" else "aria-runtime"


def go_engine() -> Optional[str]:
    """The aria-runtime to serve MCP with, or None for the Python server."""
    if os.environ.get("ARIA_MCP_ENGINE", "").strip().lower() == "python":
        return None
    configured = os.environ.get("ARIA_RUNTIME", "").strip()
    candidates = []
    if configured and Path(configured).stem == "aria-runtime":
        candidates.append(Path(configured))
    if getattr(sys, "frozen", False):
        # go/buildtools `runtime` puts it next to the frozen MCP executable.
        here = Path(sys.executable).resolve().parent
        candidates += [here / _NAME, here.parent / _NAME]
    candidates.append(Path(__file__).resolve().parent / "_bin" / _NAME)
    for candidate in candidates:
        if candidate.is_file() and (sys.platform == "win32" or os.access(candidate, os.X_OK)):
            return str(candidate)
    found = shutil.which("aria-runtime")
    return found


def main() -> None:
    engine = go_engine()
    if engine is None:
        from aria_code.packages.aria_mcp.server import main as python_main

        python_main()
        return
    args = [engine, "mcp", *sys.argv[1:]]
    if os.name != "nt":
        os.execv(engine, args)
    raise SystemExit(subprocess.run(args, check=False).returncode)


if __name__ == "__main__":
    main()
