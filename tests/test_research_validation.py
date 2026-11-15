"""Tests for independent Research & Backtest Validation Gate."""

from aria_code.domain.research_validation import (
    deflated_sharpe_ratio,
    minimum_backtest_length_years,
    validate_research_backtest,
)
from aria_code.domain.risk_policy import RiskPolicy


def test_deflated_sharpe_ratio_penalizes_multiple_trials():
    dsr_single = deflated_sharpe_ratio(1.8, n_obs=500, n_trials=1)
    dsr_many = deflated_sharpe_ratio(1.8, n_obs=500, n_trials=100)
    assert dsr_single > 0.99
    assert dsr_many < dsr_single


def test_minimum_backtest_length_increases_with_trials():
    btl_1 = minimum_backtest_length_years(1.5, n_trials=1)
    btl_50 = minimum_backtest_length_years(1.5, n_trials=50)
    assert btl_50 > btl_1 > 0


def test_validate_research_backtest_approved_and_rejected():
    policy = RiskPolicy(max_position_weight=0.25, max_drawdown_pct=0.20, approval_required_above_weight=0.15)

    ok = validate_research_backtest(
        is_sharpe=2.0,
        oos_sharpe=1.6,
        n_obs=750,
        n_trials=5,
        max_drawdown_pct=0.10,
        pit_passed=True,
        risk_policy=policy,
        target_weight=0.10,
    )
    assert ok.passed is True
    assert ok.verdict == "approved"
    assert ok.dsr >= 0.95

    overfit_and_leaked = validate_research_backtest(
        is_sharpe=3.0,
        oos_sharpe=0.3,
        n_obs=40,
        n_trials=200,
        max_drawdown_pct=0.25,
        pit_passed=False,
        risk_policy=policy,
        target_weight=0.30,
    )
    assert overfit_and_leaked.passed is False
    assert overfit_and_leaked.verdict == "rejected"
    assert any("Point-in-Time" in r for r in overfit_and_leaked.reasons)
    assert any("degradation" in r for r in overfit_and_leaked.reasons)
    assert any("Deflated Sharpe" in r for r in overfit_and_leaked.reasons)


def test_load_bars_enforces_point_in_time(monkeypatch):
    import aria_code.data_service as ds
    from aria_code.domain.backtest_engine import load_bars

    payload = ds.DataServiceResult(
        kind="history",
        symbol="AAPL",
        success=True,
        data={
            "data": [
                {"date": "2026-11-08", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
                {"date": "2026-11-12", "open": 102, "high": 103, "low": 101, "close": 102, "volume": 12},
            ]
        },
        quality={"available_at": "2026-11-10T00:00:00Z", "status": "ok"},
        timestamp="2026-11-10T00:00:00Z",
    )
    monkeypatch.setattr(ds.DataService, "history", lambda self, *a, **k: payload)

    bars = load_bars("AAPL", as_of="2026-11-10T23:59:59Z", enforce_pit=True)
    assert [b.date for b in bars] == ["2026-11-08"]

    import pytest
    with pytest.raises(ValueError, match="Point-in-Time"):
        load_bars("AAPL", as_of="2026-11-09T00:00:00Z", enforce_pit=True)


def test_strategy_vault_validate_and_save_backtest(tmp_path, monkeypatch):
    import aria_code.domain.strategy_vault as sv

    monkeypatch.setattr(sv, "_VAULT_DIR", tmp_path)
    monkeypatch.setattr(sv, "_VAULT_DB", tmp_path / "vault.db")
    vault = sv.StrategyVault()
    ver = vault.save("def on_bar(ctx): return []", name="demo_strat", message="init")

    report = vault.validate_and_save_backtest(
        ver.id,
        {"is_sharpe": 2.0, "oos_sharpe": 1.6, "n_obs": 750, "max_drawdown_pct": 0.10},
        n_trials=5,
        pit_passed=True,
    )
    assert report.passed is True
    loaded = vault.load("demo_strat")
    assert loaded is not None
    assert loaded.backtest_result["oos_sharpe"] == 1.6
    assert loaded.review_result["validation_gate"]["verdict"] == "approved"


def test_native_rust_validation_matches_python():
    from aria_code.domain.rust_quant import find_aria_quant_binary

    if find_aria_quant_binary() is None:
        return
    policy = RiskPolicy(max_position_weight=0.25, max_drawdown_pct=0.20, approval_required_above_weight=0.15)
    py_rep = validate_research_backtest(
        is_sharpe=2.0,
        oos_sharpe=1.6,
        n_obs=750,
        n_trials=5,
        max_drawdown_pct=0.10,
        pit_passed=True,
        risk_policy=policy,
        target_weight=0.10,
        prefer_native=False,
    )
    rs_rep = validate_research_backtest(
        is_sharpe=2.0,
        oos_sharpe=1.6,
        n_obs=750,
        n_trials=5,
        max_drawdown_pct=0.10,
        pit_passed=True,
        risk_policy=policy,
        target_weight=0.10,
        prefer_native=True,
    )
    assert rs_rep.passed == py_rep.passed
    assert rs_rep.verdict == py_rep.verdict
    assert abs(rs_rep.dsr - py_rep.dsr) < 1e-6
    assert abs(rs_rep.min_btl_years - py_rep.min_btl_years) < 1e-2
