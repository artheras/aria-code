import numpy as np
import pandas as pd
import pytest

from aria_code.tools.data_analysis_tools import _annual_vol, _calc_sharpe, _periods_per_year


def test_monthly_series_is_scaled_by_twelve_not_252():
    idx = pd.date_range("2023-01-31", periods=24, freq="ME")
    r = pd.Series([0.02, -0.01] * 12, index=idx)
    assert _periods_per_year(r) == pytest.approx(12, rel=0.03)
    assert _annual_vol(r) == pytest.approx(r.std() * np.sqrt(12), rel=0.1)


def test_daily_and_undated_series_keep_252():
    idx = pd.bdate_range("2023-01-02", periods=260)
    assert _periods_per_year(pd.Series(0.001, index=idx)) == pytest.approx(252, rel=0.05)
    assert _periods_per_year(pd.Series([0.1, 0.2, 0.3])) == 252.0
    assert _calc_sharpe(pd.Series([0.01, -0.01, 0.02])) is not None
