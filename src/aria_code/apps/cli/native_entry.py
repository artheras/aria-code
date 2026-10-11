"""Lightweight native frontend bootstrap and frozen worker entrypoints.

Rust owns the terminal; Python remains the provider/tool application service.
Private worker modes are dispatched before importing the legacy CLI. Setting
ARIA_FRONTEND=python explicitly keeps the original interface available.
"""
from __future__ import annotations

import os
import subprocess
import sys


def dispatch_worker() -> None:
    if sys.argv[1:2] != ["--aria-worker"]:
        return
    if len(sys.argv) < 3:
        raise SystemExit("Missing worker mode")
    mode = sys.argv[2]
    del sys.argv[1:3]
    os.environ["ARIA_FRONTEND"] = "python"
    # The host assigns process containment before acknowledging startup.
    if mode in ("app", "stream") and sys.stdin.buffer.read(1) != b"\n":
        raise SystemExit(2)
    if mode == "app":
        from aria_code.apps.cli.app_server import main
    elif mode == "bridge":
        from aria_code.apps.cli.native_bridge import main
    elif mode in ("cli", "stream"):
        from aria_code.apps.cli.main import main
    else:
        raise SystemExit("Unknown worker mode")
    main()
    raise SystemExit(0)


def maybe_native() -> None:
    if os.environ.get("ARIA_FRONTEND", "").lower() == "python":
        return
    # Never change pipe/headless output or load a UI in a non-terminal.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    from aria_code.runtime.native_index import native_binary
    binary = native_binary()
    if not binary:
        return  # Pure Python wheels remain usable without a Rust installation.
    try:
        capability = subprocess.run([binary, "--entry-protocol"], capture_output=True,
                                    timeout=3, check=False)
        if capability.returncode or capability.stdout.strip() != b"1":
            return  # Old opt-in prototypes do not implement the formal entry.
    except (OSError, subprocess.SubprocessError):
        return
    env = dict(os.environ, ARIA_PYTHON=sys.executable)
    if os.name != "nt":
        os.execve(binary, [binary, "--app", *sys.argv[1:]], env)
    result = subprocess.run([binary, "--app", *sys.argv[1:]], env=env, check=False)
    raise SystemExit(result.returncode)
