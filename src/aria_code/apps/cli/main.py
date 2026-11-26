"""Thin native launcher for Aria Code (`aria` / `aria-code`).

Delegates execution directly to the native Rust binary (`aria-native`).
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Optional

from aria_code._version import __version__

_EXE = "aria-native.exe" if sys.platform == "win32" else "aria-native"


def _is_executable(path: Path) -> bool:
    return path.is_file() and (sys.platform == "win32" or os.access(path, os.X_OK))


def find_native_binary() -> Optional[str]:
    """Locate ``aria-native`` in env, packaged ``_bin/``, repo target, or ``PATH``."""
    configured = os.environ.get("ARIA_NATIVE_BINARY", "").strip()
    if configured:
        cand = Path(configured)
        return str(cand) if _is_executable(cand) else None

    here = Path(__file__).resolve()
    pkg_root = here.parents[2]  # src/aria_code
    exe_dir = Path(sys.executable).resolve().parent
    candidates = [
        exe_dir / "_internal" / _EXE,
        exe_dir / _EXE,
        pkg_root / "_bin" / _EXE,
        pkg_root.parent.parent / "rust" / "target" / "release" / _EXE,
        pkg_root.parent.parent / "rust" / "target" / "debug" / _EXE,
    ]
    for cand in candidates:
        if _is_executable(cand):
            return str(cand)
    return shutil.which("aria-native")


_PRODUCT_HELP = """Aria Code — AI coding and quantitative engineering CLI

Usage:
  aria [OPTIONS]
  aria-code [OPTIONS]

Options:
  -C, --cd DIR                     Set workspace directory
  --model MODEL                    Select model
  --url URL                        Override provider base URL
  --session ID                     Resume a saved session
  --resume                         Resume the most recent session
  --thinking                       Enable extended thinking
  --local                          Prefer local providers
  --no-banner                      Hide startup banner
  --banner STYLE                   Select startup banner style
  --allow-tools TOOLS              Allow specific tools
  --add-dir DIR                    Grant access to additional directory
  --read-dir DIR                   Grant read-only access to directory
  --dangerously-skip-permissions   Skip permission prompts
  -V, --version                    Show version and exit
  -h, --help                       Show this help message and exit
"""


def main() -> None:
    """Synchronous entry point for the ``aria`` and ``aria-code`` commands."""
    argv = sys.argv[1:]
    if len(argv) >= 2 and argv[0] == "--aria-worker" and argv[1] == "cli":
        argv = argv[2:]
    if argv in (["--version"], ["-V"]):
        print(f"aria-code {__version__}")
        return
    if argv in (["--help"], ["-h"]):
        print(_PRODUCT_HELP, end="")
        return

    binary = find_native_binary()
    if binary is None:
        raise SystemExit(
            "aria-native binary not found. Install the native release or set ARIA_NATIVE_BINARY."
        )

    args = [binary, *argv]
    if os.name != "nt":
        os.execv(binary, args)
    raise SystemExit(subprocess.run(args, check=False).returncode)


__all__ = ["find_native_binary", "main"]


if __name__ == "__main__":
    main()
