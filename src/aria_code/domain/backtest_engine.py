"""Backtest compatibility API and host-specific data/broker adapters.

The pure simulation and strategy implementation is owned by financial-service
and pinned in finance-core.lock.json. This module retains existing imports,
data loading and order-intent conversion; it does not execute broker orders.
"""

from __future__ import annotations

from typing import List

from ._finance_core.backtest_engine import (
    Bar,
    BacktestEngine,
    BacktestResult,
    BuyHoldStrategy,
    Context,
    Portfolio,
    SmaCrossStrategy,
    Strategy,
    StrategyOrder,
    get_strategy,
    stats,
    _metrics,
    _STRATEGIES,
)

__all__ = [
    "Bar",
    "BacktestEngine",
    "BacktestResult",
    "BuyHoldStrategy",
    "Context",
    "Portfolio",
    "SmaCrossStrategy",
    "Strategy",
    "StrategyOrder",
    "get_strategy",
    "load_bars",
    "strategy_order_to_intent",
]


def strategy_order_to_intent(order: StrategyOrder, *, price: float | None = None, source: str = "strategy"):
    """Convert a StrategyOrder into a broker OrderIntent so the SAME strategy can
    drive live trading through the preview -> risk -> (auto)execute pipeline."""
    from aria_code.brokers.trading import OrderIntent

    if order.action == "target":
        return OrderIntent(symbol=order.symbol, side="buy", target_weight=order.target_weight, source=source)
    return OrderIntent(symbol=order.symbol, side=order.action, quantity=order.quantity, price=price, source=source)


def load_bars(symbol: str, days: int = 365, interval: str = "1d") -> List[Bar]:
    """Pull OHLC history via DataService and adapt to Bar objects (for real runs)."""
    from aria_code.data_service import DataService

    res = DataService().history(symbol, days=days, interval=interval)
    rows = (res.data or {}).get("data") or []
    out: List[Bar] = []
    for r in rows:
        try:
            out.append(
                Bar(
                    date=str(r.get("date"))[:10],
                    open=float(r.get("open", 0) or 0),
                    high=float(r.get("high", 0) or 0),
                    low=float(r.get("low", 0) or 0),
                    close=float(r.get("close", 0) or 0),
                    volume=float(r.get("volume", 0) or 0),
                )
            )
        except (TypeError, ValueError):
            continue
    return out
