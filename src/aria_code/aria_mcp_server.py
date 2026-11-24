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
_RUST_NAME = "aria-native.exe" if sys.platform == "win32" else "aria-native"


def _is_executable(path: Path) -> bool:
    return path.is_file() and (sys.platform == "win32" or os.access(path, os.X_OK))


def go_engine() -> Optional[str]:
    """The aria-runtime to serve MCP with, or None for the Python server."""
    mode = os.environ.get("ARIA_MCP_ENGINE", "").strip().lower()
    if mode in {"python", "rust"}:
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
        if _is_executable(candidate):
            return str(candidate)
    found = shutil.which("aria-runtime")
    return found


def rust_engine() -> Optional[str]:
    """The aria-native binary to serve MCP with, or None when unavailable."""
    mode = os.environ.get("ARIA_MCP_ENGINE", "").strip().lower()
    if mode in {"python", "go"}:
        return None
    configured = os.environ.get("ARIA_NATIVE_BINARY", "").strip()
    candidates = []
    if configured and Path(configured).stem == "aria-native":
        candidates.append(Path(configured))
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve().parent
        candidates += [here / _RUST_NAME, here.parent / _RUST_NAME]
    candidates.append(Path(__file__).resolve().parent / "_bin" / _RUST_NAME)
    for candidate in candidates:
        if _is_executable(candidate):
            return str(candidate)
    return shutil.which("aria-native")


def _exec_handover(args: list[str]) -> None:
    if os.name != "nt":
        os.execv(args[0], args)
    raise SystemExit(subprocess.run(args, check=False).returncode)


def main() -> None:
    mode = os.environ.get("ARIA_MCP_ENGINE", "").strip().lower()
    if mode == "rust":
        rust_bin = rust_engine()
        if rust_bin is not None:
            _exec_handover([rust_bin, "mcp-serve", *sys.argv[1:]])

    engine = go_engine()
    if engine is not None:
        _exec_handover([engine, "mcp", *sys.argv[1:]])

    rust_bin = rust_engine()
    if rust_bin is not None:
        _exec_handover([rust_bin, "mcp-serve", *sys.argv[1:]])

    from aria_code.packages.aria_mcp.server import main as python_main

    python_main()


if __name__ == "__main__":
    main()
