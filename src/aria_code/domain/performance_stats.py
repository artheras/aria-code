"""Performance statistics, from the data's own calendar.

There were two backtest calculators in this package, and they disagreed. The
engine annualized as (1 + total) ** (252 / bars) - 1, which is right only when
a year holds 252 bars. Run on a calendar year of daily bars for a market that
trades every day it reported a 10.5% return as 7.1% a year, and on a year of
weekly bars it reported 59%. The report module annualized by calendar span but
still scaled volatility and Sharpe by sqrt(252). Neither could be right for a
24/7 market, for weekly bars, or for an intraday run, and nothing tested the
numbers, because the only check was the output of the same code.

The statistics here read the span from the dates themselves, so one rule holds
for every bar frequency:

    years              = (last date - first date) / 365.25
    annualized return  = (1 + total) ** (1 / years) - 1
    periods per year   = observations / years
    Sharpe             = mean / sample-std * sqrt(periods per year)

An explicit periods-per-year is still honoured: someone who passes 365 knows
their data. Dates that cannot be read fall back to 252, the previous behaviour,
which keeps synthetic series ("1", "2", "3") working. There is a Go port of
this arithmetic (go/runtime/quant) checked against the fixtures this module
generates; change both together.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Optional, Sequence

#: What the engine assumed for every series, and what an undatable one still gets.
FALLBACK_PERIODS_PER_YEAR = 252

DAYS_PER_YEAR = 365.25

#: A span shorter than one trading day would compound a result hundreds of
#: times over. The report module already clamped here; the engine now does too.
MIN_YEARS = 1.0 / FALLBACK_PERIODS_PER_YEAR


def parse_day(text: object) -> Optional[date]:
    """The calendar day of an ISO timestamp or date, or None when it is not one."""
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def span_years(first: object, last: object) -> Optional[float]:
    """Years between two dates, or None when either is unreadable or the span is not forward.

    Clamped to MIN_YEARS from below so a one-day span cannot compound into nonsense.
    """
    a, b = parse_day(first), parse_day(last)
    if a is None or b is None or b <= a:
        return None
    return max((b - a).days / DAYS_PER_YEAR, MIN_YEARS)


def annualized_return(total_return: float, years: float) -> float:
    """The constant yearly rate that compounds to `total_return` over `years`."""
    if total_return <= -1.0:
        return -1.0
    return (1.0 + total_return) ** (1.0 / years) - 1.0


def stddev(values: Sequence[float]) -> float:
    """Sample standard deviation (n - 1); 0 for fewer than two values."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(max(variance, 0.0))


def sharpe(returns: Sequence[float], periods_per_year: float) -> float:
    """Annualized mean over sample standard deviation; 0 when there is no spread."""
    if len(returns) < 2:
        return 0.0
    std = stddev(returns)
    if std <= 0:
        return 0.0
    return (sum(returns) / len(returns)) / std * math.sqrt(periods_per_year)


def max_drawdown(equity: Sequence[float]) -> float:
    """The worst peak-to-trough fall, as a negative fraction (0 when never below a peak)."""
    peak = equity[0] if equity else 0.0
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def periods_per_year_from_dates(n_returns: int, first: object, last: object) -> Optional[float]:
    """How many observations a year this series holds, or None when its dates cannot say."""
    years = span_years(first, last)
    return None if years is None else n_returns / years
