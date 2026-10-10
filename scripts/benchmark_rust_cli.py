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
        session_dir = Path(temporary) / "sessions"
        session_dir.mkdir()
        for index in range(100):
            (session_dir / f"session_{index:03d}.json").write_text(json.dumps({
                "id": f"session_{index:03d}", "metadata": {"title": f"Project {index}"},
                "updated_at": "2026-10-10T00:00:00", "messages": [
                    {"role": "user", "content": "Investigate UTF-8 support 你好" * 20},
                    {"role": "assistant", "content": "Verified a project change." * 20},
                ],
            }), encoding="utf-8")
        (Path(temporary) / "config.json").write_text(json.dumps({
            "model": "google/gemini-3.5-flash", "local_provider": "google", "ui_lang": "zh",
        }), encoding="utf-8")
        (Path(temporary) / "native_update_check-native.json").write_text(json.dumps({
            "schema": 1, "source": "https://api.github.com/repos/artheras/aria-code/releases/latest",
            "checked_at": time.time() - 1, "latest": "0.126.0",
        }), encoding="utf-8")
        result = {"platform": platform.platform(), "machine": platform.machine(),
                  "python": platform.python_version(), "binary_bytes": binary.stat().st_size,
                  "session_fixture": {"stored_sessions": 100, "listed_sessions": 20, "messages_per_session": 2},
                  "scope": "command entry points only; full TUI and cloud inference are not measured",
                  "native_help": measure([str(binary), "--help"], args.samples, env),
                  "native_version": measure([str(binary), "--version"], args.samples, env),
                  "native_config_paths": measure([str(binary), "config", "paths"], args.samples, env),
                  "native_config_show": measure([str(binary), "config", "show"], args.samples, env),
                  "native_session_list": measure([str(binary), "sessions", "list"], args.samples, env),
                  "python_session_list": measure([sys.executable, "-c", "import json; from aria_code.apps.cli.session_store import SessionManager; print(json.dumps(SessionManager().list_sessions()))"], args.samples, env),
                  "native_cached_update": measure([str(binary), "update", "check", "--current", "0.126.0", "--offline"], args.samples, env),
                  "python_version": measure([sys.executable, "-c", "from aria_code.apps.cli.main import main; main()", "--version"], args.samples, env),
                  "delegated_python_version": measure([str(binary), "--python", sys.executable, "run", "--", "--version"], args.samples, env),
                  "python_tool_bridge": measure([str(binary), "--python", sys.executable, "-C", str(root),
                                                 "tool", "read_file", '{"path":"rust/Cargo.toml"}'], args.samples, env)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
