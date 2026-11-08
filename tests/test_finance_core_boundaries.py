"""Shared simulation stays pure; legacy callers retain the same objects."""

from __future__ import annotations

import importlib
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_backtest_and_statistics_paths_share_the_managed_objects():
    from aria_code.domain import backtest_engine as host, performance_stats
    from aria_code.domain._finance_core import backtest_engine as core

    assert importlib.import_module("backtest_engine") is host
    assert host.Bar is core.Bar
    assert host.BacktestEngine is core.BacktestEngine
    assert host.Strategy is core.Strategy
    assert host._STRATEGIES is core._STRATEGIES
    assert host.stats is performance_stats is core.stats
    assert host.load_bars.__module__ == host.__name__
    assert host.strategy_order_to_intent.__module__ == host.__name__


def test_pure_core_imports_and_runs_without_host_services_or_finance_extras(tmp_path):
    code = r"""
import sys
from importlib.abc import MetaPathFinder

class NoHostServices(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = ("aria_code.brokers", "aria_code.data_service", "brokers", "data_service",
                   "arthera_sdk", "arthera_finance", "numpy", "pandas")
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise ImportError("Host dependency reached: " + fullname)

sys.meta_path.insert(0, NoHostServices())
sys.path.insert(0, sys.argv[1])
from aria_code.domain._finance_core.backtest_engine import Bar, BacktestEngine, BuyHoldStrategy
data = {"AAA": [Bar("2026-01-01", 10, 10, 10, 10),
                Bar("2026-01-02", 10, 12, 10, 12)]}
result = BacktestEngine(starting_cash=1000, commission=0, slippage=0).run(data, BuyHoldStrategy())
assert result.equity_curve[-1]["equity"] == 1200
assert result.trades[0]["date"] == "2026-01-02"
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(ROOT / "src")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
