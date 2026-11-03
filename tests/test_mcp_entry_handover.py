"""aria-code-mcp hands over to the Go server (aria-runtime mcp) when one is installed."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX stand-in executables")


def fake_runtime(directory: Path) -> Path:
    path = directory / "aria-runtime"
    path.write_text('#!/bin/sh\necho "go engine: $*"\n')
    path.chmod(0o755)
    return path


def test_go_engine_lookup_order(tmp_path, monkeypatch):
    from aria_code import aria_mcp_server as entry

    package = tmp_path / "aria_code"
    (package / "_bin").mkdir(parents=True)
    module = package / "aria_mcp_server.py"
    module.write_text("")
    monkeypatch.setattr(entry, "__file__", str(module))
    monkeypatch.delenv("ARIA_RUNTIME", raising=False)
    monkeypatch.delenv("ARIA_MCP_ENGINE", raising=False)
    monkeypatch.setattr(entry.shutil, "which", lambda name: None)
    assert entry.go_engine() is None, "no Go engine: the Python server"

    packaged = fake_runtime(package / "_bin")
    assert entry.go_engine() == str(packaged.resolve())
    packaged.chmod(0o644)
    assert entry.go_engine() is None, "a copy that cannot run is not used"
    packaged.chmod(0o755)

    elsewhere = tmp_path / "custom"
    elsewhere.mkdir()
    configured = fake_runtime(elsewhere)
    monkeypatch.setenv("ARIA_RUNTIME", str(configured))
    assert entry.go_engine() == str(configured), "ARIA_RUNTIME wins"
    monkeypatch.setenv("ARIA_RUNTIME", str(tmp_path / "python3"))
    assert entry.go_engine() == str(packaged.resolve()), "ARIA_RUNTIME naming another program is ignored"

    monkeypatch.setenv("ARIA_MCP_ENGINE", "python")
    assert entry.go_engine() is None, "ARIA_MCP_ENGINE=python keeps the Python server"


def test_the_entry_point_execs_the_go_server_with_its_arguments(tmp_path):
    runtime = fake_runtime(tmp_path)
    env = {**os.environ, "ARIA_RUNTIME": str(runtime),
           "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")])}
    env.pop("ARIA_MCP_ENGINE", None)
    out = subprocess.run([sys.executable, "-m", "aria_code.aria_mcp_server", "--version"],
                         env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "go engine: mcp --version"
