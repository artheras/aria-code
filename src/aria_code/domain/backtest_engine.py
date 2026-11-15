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


def load_bars(
    symbol: str,
    days: int = 365,
    interval: str = "1d",
    *,
    as_of: str | None = None,
    enforce_pit: bool = True,
) -> List[Bar]:
    """Pull OHLC history via DataService and adapt to Bar objects (for real runs).

    When ``enforce_pit`` is enabled and ``as_of`` is provided, rejects payloads
    whose ``available_at`` is after ``as_of`` and filters out any bars dated
    after ``as_of[:10]`` to prevent look-ahead leakage.
    """
    from aria_code.data_service import DataService, validate_point_in_time

    res = DataService().history(symbol, days=days, interval=interval)
    if enforce_pit and as_of is not None:
        pit = validate_point_in_time(res, as_of=as_of)
        if not pit["passed"]:
            raise ValueError(
                f"Point-in-Time gate rejected bars for {symbol}: {'; '.join(pit['reasons'])}"
            )
    rows = (res.data or {}).get("data") or []
    as_of_date = str(as_of)[:10] if (enforce_pit and as_of is not None) else None
    out: List[Bar] = []
    for r in rows:
        try:
            bar_date = str(r.get("date"))[:10]
            if as_of_date is not None and bar_date > as_of_date:
                continue
            out.append(
                Bar(
                    date=bar_date,
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
