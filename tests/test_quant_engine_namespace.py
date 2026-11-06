"""Bundled quant modules are deterministic; private extensions require opt-in."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("configured", [False, True])
def test_private_backend_never_changes_imports_without_explicit_configuration(tmp_path, configured):
    backend = tmp_path / "Arthera"
    extension = backend / "packages/quant_engine/backtest"
    extension.mkdir(parents=True)
    (extension / "team_only.py").write_text("VALUE = 'explicit backend'\n")
    env = dict(os.environ)
    env.pop("ARTHERA_ROOT", None)
    if configured:
        env["ARTHERA_ROOT"] = str(backend)
    code = r"""
import importlib
from pathlib import Path
import sys
source = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(source))
import aria_code
for name in ('packages', 'packages.quant_engine', 'packages.quant_engine.backtest',
             'packages.quant_engine.portfolio', 'packages.quant_engine.sports',
             'packages.quant_engine.stochastic'):
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve().is_relative_to(source), name
    assert Path(next(iter(module.__path__))).resolve().is_relative_to(source), name
    if sys.argv[2] == 'False':
        assert all(Path(p).resolve().is_relative_to(source) for p in module.__path__), name
engine = importlib.import_module('packages.quant_engine.backtest.engine')
assert Path(engine.__file__).resolve().is_relative_to(source)
try:
    plugin = importlib.import_module('packages.quant_engine.backtest.team_only')
except ModuleNotFoundError:
    assert sys.argv[2] == 'False'
else:
    assert sys.argv[2] == 'True'
    assert plugin.VALUE == 'explicit backend'
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(ROOT / "src"), str(configured)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
