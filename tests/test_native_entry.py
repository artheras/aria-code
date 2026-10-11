"""Interpreter/frozen entry dispatch must not recurse or change pipe output."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("entry", ["console", "frozen-source"])
def test_worker_dispatch_precedes_cli_imports(entry, tmp_path):
    command = ([sys.executable, "-c", "from aria_code.apps.cli.main import main; main()"]
               if entry == "console" else [sys.executable, str(ROOT / "src/aria_code/aria_cli.py")])
    result = subprocess.run([*command, "--aria-worker", "cli", "--version"], cwd=tmp_path,
                            env=dict(os.environ, PYTHONPATH=str(ROOT / "src")), capture_output=True, timeout=10)
    from aria_code._version import __version__
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode().strip() == f"aria-code {__version__}"


def test_worker_rejects_missing_host_handshake(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "src/aria_code/aria_cli.py"),
                            "--aria-worker", "app"], input=b"X", capture_output=True, cwd=tmp_path,
                            env=dict(os.environ, PYTHONPATH=str(ROOT / "src")), timeout=10)
    assert result.returncode == 2
    assert result.stdout == b""


def test_updater_finds_command_symlink_when_running_in_frozen_worker(tmp_path, monkeypatch):
    from aria_code.apps.cli import updater
    root = tmp_path / "releases/version/aria-code-bin"
    root.mkdir(parents=True)
    worker = root / "aria-code-worker"
    worker.touch()
    frontend = root / "aria-code-bin"
    frontend.touch()
    bin_dir = tmp_path / ".local/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "aria-code").symlink_to(frontend)
    monkeypatch.delenv("ARIA_CODE_INSTALL_DIR", raising=False)
    monkeypatch.setattr(sys, "executable", str(worker))
    monkeypatch.setattr(sys, "argv", [str(worker), "update"])
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert updater._install_dir() == bin_dir


@pytest.mark.skipif(not os.environ.get("ARIA_NATIVE_BINARY"), reason="build Rust first")
def test_formal_version_and_python_rollback_keep_product_version(tmp_path):
    from aria_code._version import __version__
    for rollback in ("rust", "python"):
        env = dict(os.environ, ARIA_FRONTEND=rollback, ARIA_PYTHON=sys.executable,
                   ARIA_HOME=str(tmp_path), PYTHONPATH=str(ROOT / "src"))
        result = subprocess.run([os.environ["ARIA_NATIVE_BINARY"], "--app", "--version"],
                                cwd=tmp_path, env=env, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout.decode().strip() == f"aria-code {__version__}"
