#!/usr/bin/env python3
"""Measure entry-point overhead; this is not a model or full TUI benchmark."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time


def measure(command: list[str], samples: int, env: dict) -> dict:
    elapsed = []
    for _ in range(samples):
        started = time.perf_counter()
        result = subprocess.run(command, capture_output=True, env=env, timeout=30)
        elapsed.append((time.perf_counter() - started) * 1000)
        if result.returncode:
            raise RuntimeError(f"Benchmark failed ({result.returncode}): {result.stderr.decode(errors='replace')[:500]}")
    warm = elapsed[1:] or elapsed
    return {"first_ms": round(elapsed[0], 3), "warm_median_ms": round(statistics.median(warm), 3),
            "warm_max_ms": round(max(warm), 3), "samples": samples}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--samples", type=int, default=8)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("samples must be at least 2")
    binary = args.binary.resolve(strict=True)
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="aria-native-benchmark-") as temporary:
        env = {**os.environ, "PYTHONPATH": str(root / "src"), "ARIA_HOME": temporary,
               "ARIA_USER_OUTPUT_ROOT": str(Path(temporary) / "outputs")}
        result = {"platform": platform.platform(), "machine": platform.machine(),
                  "python": platform.python_version(), "binary_bytes": binary.stat().st_size,
                  "scope": "command entry points only; full TUI and cloud inference are not measured",
                  "native_help": measure([str(binary), "--help"], args.samples, env),
                  "native_version": measure([str(binary), "--version"], args.samples, env),
                  "python_version": measure([sys.executable, "-c", "from aria_code.apps.cli.main import main; main()", "--version"], args.samples, env),
                  "delegated_python_version": measure([str(binary), "--python", sys.executable, "run", "--", "--version"], args.samples, env),
                  "python_tool_bridge": measure([str(binary), "--python", sys.executable, "-C", str(root),
                                                 "tool", "read_file", '{"path":"rust/Cargo.toml"}'], args.samples, env)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

