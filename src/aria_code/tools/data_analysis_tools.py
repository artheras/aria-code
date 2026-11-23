"""Return series frequency and performance helpers (analytics migrated to artheras/financial-service)."""

from __future__ import annotations

from typing import Optional

try:
    import pandas as pd
    _HAS_PD = True
except ImportError:
    _HAS_PD = False

try:
    import numpy as np
    _HAS_NP = True
except ImportError:
    _HAS_NP = False


def _periods_per_year(rets) -> float:
    """Observations a year in this return series, read from its index.

    Monthly-rebalanced portfolio returns are monthly, so scaling them by
    sqrt(252) overstated volatility and Sharpe about 4.6x. An index that is not
    dates falls back to 252 (daily), the old assumption.
    """
    try:
        idx = rets.index
        intervals = len(rets) - 1
        if intervals >= 1:
            span_days = (idx[-1] - idx[0]).days
            if span_days > 0:
                return float(intervals) * 365.25 / float(span_days)
    except Exception:
        pass
    return 252.0


def _annual_vol(rets) -> float:
    return float(rets.std() * np.sqrt(_periods_per_year(rets)))


def _calc_sharpe(rets, rf_daily: float = 0.0) -> Optional[float]:
    if not _HAS_NP or not hasattr(rets, "__len__") or len(rets) < 2:
        return None
    try:
        excess = rets - rf_daily
        if float(excess.std()) == 0:
            return None
        return round(float(excess.mean() / excess.std() * np.sqrt(_periods_per_year(rets))), 3)
    except Exception:
        return None


def _max_drawdown(cum_series) -> float:
    if not _HAS_NP or not _HAS_PD:
        return 0.0
    try:
        s = pd.Series(cum_series)
        roll_max = s.cummax()
        drawdown = (s - roll_max) / roll_max
        return float(drawdown.min())
    except Exception:
        return 0.0
