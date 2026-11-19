"""Tests for the event-driven backtest engine (offline, deterministic)."""
import pathlib
import sys

import pytest

_CLI_DIR = str(pathlib.Path(__file__).parents[1])
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from aria_code.domain.backtest_engine import (  # noqa: E402
    Bar,
    BacktestEngine,
    BuyHoldStrategy,
    SmaCrossStrategy,
    StrategyOrder,
    get_strategy,
    strategy_order_to_intent,
)


def _series(symbol_closes):
    """{symbol: [closes]} -> {symbol: [Bar]} with open==prev pattern simplified
    to open==close for exact-math tests unless overridden."""
    out = {}
    for sym, closes in symbol_closes.items():
        bars = []
        for i, c in enumerate(closes):
            bars.append(Bar(date=f"2026-01-{i+1:02d}", open=c, high=c, low=c, close=c))
        out[sym] = bars
    return out


def test_buy_hold_exact_pnl_zero_costs():
    # d1 close 10 -> queue buy; d2 open 10 fill; d3 close 12 -> +20%
    data = {"AAA": [
        Bar("2026-01-01", 10, 10, 10, 10),
        Bar("2026-01-02", 10, 12, 10, 12),
        Bar("2026-01-03", 12, 12, 12, 12),
    ]}
    eng = BacktestEngine(starting_cash=1000, commission=0.0, slippage=0.0)
    res = eng.run(data, BuyHoldStrategy())
    assert res.metrics["total_return"] == pytest.approx(0.20, abs=1e-6)
    assert res.benchmark["total_return"] == pytest.approx(0.20, abs=1e-6)
    assert res.metrics["n_trades"] == 1
    assert res.equity_curve[-1]["equity"] == pytest.approx(1200, abs=1e-3)


def test_costs_reduce_return():
    data = {"AAA": [Bar("2026-01-01", 10, 10, 10, 10),
                    Bar("2026-01-02", 10, 12, 10, 12),
                    Bar("2026-01-03", 12, 12, 12, 12)]}
    free = BacktestEngine(starting_cash=1000, commission=0, slippage=0).run(data, BuyHoldStrategy())
    costed = BacktestEngine(starting_cash=1000, commission=0.01, slippage=0.01).run(data, BuyHoldStrategy())
    assert costed.metrics["total_return"] < free.metrics["total_return"]


def test_no_lookahead_orders_fill_next_open():
    # A spike only at the close of the entry-decision bar must NOT be captured;
    # fills happen at the next bar's open.
    data = {"AAA": [Bar("2026-01-01", 10, 10, 10, 10),
                    Bar("2026-01-02", 20, 20, 20, 20)]}  # decide d1 close=10, fill d2 open=20
    eng = BacktestEngine(starting_cash=1000, commission=0, slippage=0)
    res = eng.run(data, BuyHoldStrategy())
    # Bought at 20 (next open), last close 20 → flat, ~0% (no look-ahead gain).
    assert res.metrics["total_return"] == pytest.approx(0.0, abs=1e-6)


def test_max_drawdown_detected():
    data = {"AAA": [Bar(f"2026-01-{i:02d}", c, c, c, c) for i, c in
                    enumerate([10, 12, 8, 9], start=1)]}
    res = BacktestEngine(starting_cash=1000, commission=0, slippage=0).run(data, BuyHoldStrategy())
    assert res.metrics["max_drawdown"] < 0  # there was a dip


def test_multi_asset_portfolio():
    data = _series({"AAA": [10, 11, 12, 13], "BBB": [20, 21, 22, 23]})
    res = BacktestEngine(starting_cash=10000, commission=0, slippage=0).run(data, BuyHoldStrategy())
    assert set(res.symbols) == {"AAA", "BBB"}
    assert res.metrics["total_return"] > 0
    # equal-weight: both held
    assert res.metrics["n_trades"] == 2


def test_sma_cross_generates_trades():
    # rise then fall to force a golden then death cross (fast=2, slow=3)
    closes = [10, 10, 10, 11, 13, 15, 14, 11, 9, 8]
    data = _series({"AAA": closes})
    res = BacktestEngine(starting_cash=10000, commission=0, slippage=0).run(
        data, SmaCrossStrategy(fast=2, slow=3))
    assert res.metrics["n_trades"] >= 1
    assert "win_rate" in res.metrics


def test_sell_records_realized_pnl_and_win_rate():
    closes = [10, 10, 10, 12, 14, 16, 10, 8, 7, 6]  # up then down → buy then sell
    data = _series({"AAA": closes})
    res = BacktestEngine(starting_cash=10000, commission=0, slippage=0).run(
        data, SmaCrossStrategy(fast=2, slow=3))
    sells = [t for t in res.trades if t["side"] == "sell"]
    assert sells and all("realized_pnl" in t for t in sells)


def test_get_strategy_and_unknown():
    assert isinstance(get_strategy("sma_cross", fast=5, slow=10), SmaCrossStrategy)
    with pytest.raises(ValueError):
        get_strategy("does_not_exist")


def test_strategy_order_to_intent_parity():
    bi = strategy_order_to_intent(StrategyOrder("AAPL", "buy", 10), price=100)
    assert bi.symbol == "AAPL" and bi.side == "buy" and bi.quantity == 10
    ti = strategy_order_to_intent(StrategyOrder("AAPL", "target", target_weight=0.5))
    assert ti.target_weight == 0.5
