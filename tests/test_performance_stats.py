"""Hand-computed checks for the shared backtest statistics (not output of the same code)."""
import math
from datetime import date, timedelta

import pytest

from aria_code.domain import performance_stats as stats
from aria_code.domain.backtest_engine import _metrics


def test_annualized_return_one_year_is_total():
    years = stats.span_years("2023-01-01", "2024-01-02")
    assert years == pytest.approx(366 / 365.25)
    assert stats.annualized_return(0.105, 1.0) == pytest.approx(0.105)


def test_annualized_return_two_years():
    assert stats.annualized_return(0.21, 2.0) == pytest.approx(0.1)  # 1.1**2 = 1.21


def test_total_loss_is_minus_one():
    assert stats.annualized_return(-1.0, 1.0) == -1.0
    assert stats.annualized_return(-1.5, 1.0) == -1.0


def test_span_clamp_and_unreadable():
    assert stats.span_years("2024-01-01", "2024-01-02") == pytest.approx(1 / 252)
    assert stats.span_years("1", "2") is None
    assert stats.span_years("2024-02-01", "2024-01-01") is None


def test_stddev_sample():
    assert stats.stddev([1, 2, 3, 4]) == pytest.approx(math.sqrt(5 / 3))
    assert stats.stddev([1]) == 0.0


def test_sharpe_scales_with_sqrt_periods():
    r = [0.01, -0.01, 0.02, 0.0]
    assert stats.sharpe(r, 4) == pytest.approx(2 * stats.sharpe(r, 1))
    assert stats.sharpe([0.01, 0.01], 252) == 0.0


def test_max_drawdown():
    assert stats.max_drawdown([100, 120, 90, 130]) == pytest.approx(-0.25)
    assert stats.max_drawdown([100, 110]) == 0.0


def _curve(dates, equity):
    return [{"date": d, "equity": e} for d, e in zip(dates, equity)]


def test_engine_uses_calendar_not_bar_count():
    # 53 weekly bars over one year, +10%: must annualize to ~10%, not 59%.
    d0 = date(2023, 1, 2)
    dates = [(d0 + timedelta(weeks=i)).isoformat() for i in range(53)]
    eq = [100 * (1.1 ** (i / 52)) for i in range(53)]
    m = _metrics(_curve(dates, eq), [])
    assert m["annual_return"] == pytest.approx(0.1, abs=0.01)


def test_engine_explicit_periods_keeps_old_formula():
    dates = ["2023-01-01", "2023-01-02", "2023-01-03"]
    m = _metrics(_curve(dates, [100, 105, 110]), [], 252)
    assert m["annual_return"] == pytest.approx(1.1 ** (252 / 3) - 1, rel=1e-4)


def test_engine_undatable_falls_back_to_252():
    m = _metrics(_curve(["1", "2", "3"], [100, 105, 110]), [])
    assert m["annual_return"] == pytest.approx(1.1 ** (252 / 3) - 1, rel=1e-4)


def test_engine_total_loss_does_not_raise():
    m = _metrics(_curve(["2023-01-01", "2023-06-01"], [100, 0.0001]), [])
    assert m["annual_return"] <= 0
