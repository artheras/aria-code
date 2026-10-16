"""backtest_engine.py — event-driven, multi-asset backtest engine.

A small, dependency-light engine that fixes the gaps of the template backtests:

  * Event-driven: iterate bars chronologically; the strategy decides on each
    bar's CLOSE and orders fill at the NEXT bar's OPEN (no look-ahead bias).
  * Multi-asset portfolio: cash + per-symbol positions, target-weight rebalance.
  * Realistic costs: commission + slippage on every fill.
  * Backtest <-> live parity: a strategy implements ``Strategy.on_bar(ctx)`` and
    returns ``StrategyOrder``s. The SAME strategy object can drive live trading —
    ``strategy_order_to_intent`` converts an order into a broker ``OrderIntent``
    for the preview -> risk -> (auto)execute pipeline.

The engine is pure (operates on provided ``Bar`` data) so it is fully unit
testable offline; ``load_bars`` bridges to DataService for real runs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from aria_code.domain import performance_stats as stats


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class StrategyOrder:
    symbol: str
    action: str                      # "buy" | "sell" | "target"
    quantity: float = 0.0            # shares, for buy/sell
    target_weight: Optional[float] = None  # fraction of equity, for "target"


@dataclass
class Portfolio:
    cash: float
    positions: Dict[str, float] = field(default_factory=dict)

    def market_value(self, prices: Dict[str, float]) -> float:
        return sum(qty * prices.get(s, 0.0) for s, qty in self.positions.items())

    def equity(self, prices: Dict[str, float]) -> float:
        return self.cash + self.market_value(prices)


class Context:
    """Read-only view handed to ``Strategy.on_bar`` each step."""

    def __init__(self, date: str, bars: Dict[str, Bar],
                 history: Dict[str, List[Bar]], portfolio: Portfolio):
        self.date = date
        self.bars = bars
        self.history = history
        self.portfolio = portfolio

    def price(self, symbol: str) -> float:
        b = self.bars.get(symbol)
        return b.close if b else 0.0

    def position(self, symbol: str) -> float:
        return self.portfolio.positions.get(symbol, 0.0)

    def closes(self, symbol: str, n: int | None = None) -> List[float]:
        hist = [b.close for b in self.history.get(symbol, [])]
        return hist[-n:] if n else hist

    def equity(self) -> float:
        return self.portfolio.equity({s: self.price(s) for s in self.bars})


class Strategy(ABC):
    """Backtest/live strategy. ``on_bar`` is called on each bar's close."""

    name: str = "strategy"

    def on_bar(self, ctx: Context) -> List[StrategyOrder]:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass
class BacktestResult:
    strategy: str
    symbols: List[str]
    starting_cash: float
    equity_curve: List[Dict[str, Any]]   # [{date, equity}]
    trades: List[Dict[str, Any]]
    metrics: Dict[str, Any]
    benchmark: Dict[str, Any]


def _metrics(equity_curve: List[Dict[str, Any]], trades: List[Dict[str, Any]],
             periods: Optional[int] = None) -> Dict[str, Any]:
    """Headline statistics for a run.

    `periods` is the caller's own periods-per-year when they gave one, and then
    behaves exactly as it always did. Left out, the span comes from the equity
    curve's dates (see performance_stats), so a year of weekly bars and a year
    of daily bars both annualize to a year. A curve whose dates cannot be read
    falls back to 252, the old behaviour.
    """
    eq = [pt["equity"] for pt in equity_curve]
    if len(eq) < 2 or eq[0] <= 0:
        return {"total_return": 0.0, "annual_return": 0.0, "sharpe": 0.0,
                "max_drawdown": 0.0, "n_trades": len(trades), "win_rate": 0.0}
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    total = eq[-1] / eq[0] - 1
    n = len(eq)
    years = stats.span_years(equity_curve[0].get("date"), equity_curve[-1].get("date"))
    if periods is not None:
        sharpe_periods = periods
        annual = -1.0 if total <= -1 else (1 + total) ** (periods / n) - 1
    elif years is not None:
        sharpe_periods = len(rets) / years
        annual = stats.annualized_return(total, years)
    else:
        sharpe_periods = stats.FALLBACK_PERIODS_PER_YEAR
        annual = -1.0 if total <= -1 else (1 + total) ** (sharpe_periods / n) - 1
    sells = [t for t in trades if t["side"] == "sell"]
    wins = [t for t in sells if t.get("realized_pnl", 0) > 0]
    return {
        "total_return": round(total, 6),
        "annual_return": round(annual, 6),
        "sharpe": round(stats.sharpe(rets, sharpe_periods), 4),
        "max_drawdown": round(stats.max_drawdown(eq), 6),
        "n_trades": len(trades),
        "win_rate": round(len(wins) / len(sells), 4) if sells else 0.0,
    }


class BacktestEngine:
    def __init__(self, *, starting_cash: float = 100_000.0,
                 commission: float = 0.0005, slippage: float = 0.0005,
                 periods_per_year: Optional[int] = None):
        self.starting_cash = float(starting_cash)
        self.commission = float(commission)
        self.slippage = float(slippage)
        # None: read it from the data's dates. A number: the caller knows their data.
        self.periods_per_year = None if periods_per_year is None else int(periods_per_year)

    def run(self, data: Dict[str, List[Bar]], strategy: Strategy) -> BacktestResult:
        symbols = list(data.keys())
        by_date: Dict[str, Dict[str, Bar]] = {
            s: {b.date: b for b in data[s]} for s in symbols
        }
        all_dates = sorted({b.date for bars in data.values() for b in bars})

        cash = self.starting_cash
        positions: Dict[str, float] = {s: 0.0 for s in symbols}
        cost_basis: Dict[str, float] = {s: 0.0 for s in symbols}  # avg cost
        history: Dict[str, List[Bar]] = {s: [] for s in symbols}
        last_close: Dict[str, float] = {s: 0.0 for s in symbols}
        pending: List[StrategyOrder] = []   # buy/sell orders queued for next open
        equity_curve: List[Dict[str, Any]] = []
        trades: List[Dict[str, Any]] = []

        for date in all_dates:
            cur: Dict[str, Bar] = {}
            for s in symbols:
                b = by_date[s].get(date)
                if b is not None:
                    cur[s] = b
                    history[s].append(b)
                    last_close[s] = b.close

            # 1) Fill yesterday's orders at today's open (cost + slippage).
            for order in pending:
                b = cur.get(order.symbol)
                if b is None or order.quantity <= 0:
                    continue
                side = order.action
                fill = b.open * (1 + self.slippage) if side == "buy" else b.open * (1 - self.slippage)
                qty = order.quantity
                if side == "buy":
                    gross = fill * qty
                    fee = gross * self.commission
                    if gross + fee > cash:                 # cap to affordable size
                        qty = max(0.0, (cash) / (fill * (1 + self.commission)))
                        gross = fill * qty
                        fee = gross * self.commission
                    if qty <= 0:
                        continue
                    prev_qty = positions[order.symbol]
                    new_qty = prev_qty + qty
                    cost_basis[order.symbol] = (
                        (cost_basis[order.symbol] * prev_qty + fill * qty) / new_qty
                        if new_qty > 0 else 0.0
                    )
                    positions[order.symbol] = new_qty
                    cash -= gross + fee
                    trades.append({"date": date, "symbol": order.symbol, "side": "buy",
                                   "qty": round(qty, 6), "price": round(fill, 6),
                                   "fee": round(fee, 6)})
                else:  # sell
                    qty = min(qty, positions[order.symbol])
                    if qty <= 0:
                        continue
                    gross = fill * qty
                    fee = gross * self.commission
                    realized = (fill - cost_basis[order.symbol]) * qty - fee
                    positions[order.symbol] -= qty
                    cash += gross - fee
                    trades.append({"date": date, "symbol": order.symbol, "side": "sell",
                                   "qty": round(qty, 6), "price": round(fill, 6),
                                   "fee": round(fee, 6), "realized_pnl": round(realized, 6)})
            pending = []

            # 2) Mark-to-market at today's close; record equity.
            prices = {s: last_close[s] for s in symbols}
            equity = cash + sum(positions[s] * prices[s] for s in symbols)
            equity_curve.append({"date": date, "equity": round(equity, 4)})

            # 3) Strategy decides on today's close → queue orders for next open.
            ctx = Context(date, cur, history, Portfolio(cash, dict(positions)))
            try:
                orders = strategy.on_bar(ctx) or []
            except Exception:
                orders = []
            for o in orders:
                pending.append(self._normalize(o, equity, prices, positions))

        metrics = _metrics(equity_curve, trades, self.periods_per_year)
        benchmark = self._buy_hold(data, all_dates)
        return BacktestResult(
            strategy=getattr(strategy, "name", "strategy"),
            symbols=symbols, starting_cash=self.starting_cash,
            equity_curve=equity_curve, trades=trades,
            metrics=metrics, benchmark=benchmark,
        )

    def _normalize(self, o: StrategyOrder, equity: float,
                   prices: Dict[str, float], positions: Dict[str, float]) -> StrategyOrder:
        """Convert a target-weight order into a concrete buy/sell quantity."""
        if o.action != "target" or o.target_weight is None:
            return o
        px = prices.get(o.symbol, 0.0)
        if px <= 0:
            return StrategyOrder(o.symbol, "buy", 0.0)
        target_shares = (o.target_weight * equity) / px
        delta = target_shares - positions.get(o.symbol, 0.0)
        if delta >= 0:
            return StrategyOrder(o.symbol, "buy", round(delta, 6))
        return StrategyOrder(o.symbol, "sell", round(-delta, 6))

    def _buy_hold(self, data: Dict[str, List[Bar]], all_dates: List[str]) -> Dict[str, Any]:
        """Equal-weight buy-and-hold benchmark over the same window."""
        symbols = [s for s in data if data[s]]
        if not symbols or len(all_dates) < 2:
            return {"total_return": 0.0}
        firsts, lasts = {}, {}
        for s in symbols:
            bars = data[s]
            firsts[s] = bars[0].close
            lasts[s] = bars[-1].close
        rets = [(lasts[s] / firsts[s] - 1) for s in symbols if firsts[s] > 0]
        return {"total_return": round(sum(rets) / len(rets), 6) if rets else 0.0}


# ── Backtest <-> live parity bridge ────────────────────────────────────────────

def strategy_order_to_intent(order: StrategyOrder, *, price: float | None = None,
                             source: str = "strategy"):
    """Convert a StrategyOrder into a broker OrderIntent so the SAME strategy can
    drive live trading through the preview -> risk -> (auto)execute pipeline."""
    from brokers.trading import OrderIntent
    if order.action == "target":
        return OrderIntent(symbol=order.symbol, side="buy",
                           target_weight=order.target_weight, source=source)
    return OrderIntent(symbol=order.symbol, side=order.action,
                       quantity=order.quantity, price=price, source=source)


# ── Built-in strategies (validate the interface; reusable live) ─────────────────

class BuyHoldStrategy(Strategy):
    name = "buy_hold"

    def __init__(self, weight: float = 1.0):
        self.weight = weight
        self._entered: set[str] = set()

    def on_bar(self, ctx: Context) -> List[StrategyOrder]:
        orders = []
        per = self.weight / max(1, len(ctx.bars))
        for s in ctx.bars:
            if s not in self._entered:
                orders.append(StrategyOrder(s, "target", target_weight=per))
                self._entered.add(s)
        return orders


class SmaCrossStrategy(Strategy):
    name = "sma_cross"

    def __init__(self, fast: int = 20, slow: int = 60, weight: float = 0.95):
        self.fast, self.slow, self.weight = fast, slow, weight

    def on_bar(self, ctx: Context) -> List[StrategyOrder]:
        orders = []
        per = self.weight / max(1, len(ctx.bars))
        for s in ctx.bars:
            closes = ctx.closes(s)
            if len(closes) < self.slow:
                continue
            fast_ma = sum(closes[-self.fast:]) / self.fast
            slow_ma = sum(closes[-self.slow:]) / self.slow
            held = ctx.position(s) > 0
            if fast_ma > slow_ma and not held:
                orders.append(StrategyOrder(s, "target", target_weight=per))
            elif fast_ma <= slow_ma and held:
                orders.append(StrategyOrder(s, "target", target_weight=0.0))
        return orders


_STRATEGIES: Dict[str, Callable[..., Strategy]] = {
    "buy_hold": BuyHoldStrategy,
    "sma_cross": SmaCrossStrategy,
}


def get_strategy(name: str, **kwargs: Any) -> Strategy:
    cls = _STRATEGIES.get(str(name).lower())
    if not cls:
        raise ValueError(f"unknown strategy: {name} (have: {', '.join(_STRATEGIES)})")
    return cls(**kwargs)


def load_bars(symbol: str, days: int = 365, interval: str = "1d") -> List[Bar]:
    """Pull OHLC history via DataService and adapt to Bar objects (for real runs)."""
    from data_service import DataService
    res = DataService().history(symbol, days=days, interval=interval)
    rows = (res.data or {}).get("data") or []
    out: List[Bar] = []
    for r in rows:
        try:
            out.append(Bar(
                date=str(r.get("date"))[:10],
                open=float(r.get("open", 0) or 0), high=float(r.get("high", 0) or 0),
                low=float(r.get("low", 0) or 0), close=float(r.get("close", 0) or 0),
                volume=float(r.get("volume", 0) or 0),
            ))
        except (TypeError, ValueError):
            continue
    return out
