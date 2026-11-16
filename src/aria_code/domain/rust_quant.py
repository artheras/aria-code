"""Thin bridge to the native Rust `aria-quant` binary with safe Python fallback."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def find_aria_quant_binary() -> Optional[str]:
    """Locate the native `aria-quant` Rust binary if available."""
    exe_name = "aria-quant.exe" if os.name == "nt" else "aria-quant"
    env_bin = os.getenv("ARIA_QUANT_BINARY", "").strip()
    if env_bin and Path(env_bin).is_file():
        return env_bin
    on_path = shutil.which("aria-quant")
    if on_path:
        return on_path
    exe_dir = Path(sys.executable).resolve().parent
    for cand in (exe_dir / exe_name, exe_dir / "_internal" / exe_name):
        if cand.is_file():
            return str(cand)
    for parent in Path(__file__).resolve().parents:
        cand = parent / "rust" / "target" / "release" / exe_name
        if cand.is_file():
            return str(cand)
        cand_dbg = parent / "rust" / "target" / "debug" / exe_name
        if cand_dbg.is_file():
            return str(cand_dbg)
    return None


def run_aria_quant(command: str, payload: Dict[str, Any], *, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """Invoke `aria-quant <command>` with JSON payload, returning parsed dict or None."""
    bin_path = find_aria_quant_binary()
    if not bin_path:
        return None
    try:
        proc = subprocess.run(
            [bin_path, command, json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        out = json.loads(proc.stdout)
        if isinstance(out, dict):
            return out
    except Exception:
        return None
    return None
