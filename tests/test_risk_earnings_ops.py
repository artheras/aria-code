"""/risk shows its numbers, /earnings shows earnings, and the realty commands do not analyse nothing.

Recorded: /risk AAPL printed a heading and no numbers (the local tool returns
them at the top level, the command read "data"); /earnings MSFT ran a
general technical report; /ops-report with no data gave a "very high risk"
verdict and advice about member repurchase rates.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pandas as pd
import pytest

RISK = {"success": True, "symbol": "AAPL", "confidence_level": 0.95, "var_daily": 0.02444,
        "var_monthly": 0.11198, "cvar_daily": 0.03993, "max_drawdown": -0.13799,
        "annual_volatility": 0.2473, "annual_return": 0.29954, "sharpe_ratio": 1.049,
        "calmar_ratio": 2.171, "downside_deviation": 0.18408, "skewness": -0.492, "kurtosis": 3.191}


def _core(snapshot, lang="en"):
    import aria_code.apps.cli.commands.core_cmds as core_cmds

    class Cli(core_cmds.CoreCommandsMixin):
        pass

    cli = Cli()
    con = snapshot.console()
    cli.context = SimpleNamespace(has_rich=True, console=con)
    cli.terminal = SimpleNamespace(config={"ui_lang": lang}, api_url="http://127.0.0.1:9")
    return cli, con


def test_risk_prints_the_local_numbers(snapshot, monkeypatch):
    import aria_code.apps.cli.tool_executor as tool_executor
    import aria_code.apps.cli.tool_registry as tool_registry

    async def backend_down(*a, **k):
        return {"success": False, "error": "connection refused"}

    monkeypatch.setattr(tool_executor, "execute_aria_tool", backend_down)
    monkeypatch.setitem(tool_registry.LOCAL_TOOLS, "get_risk_metrics", (lambda params: dict(RISK), ""))
    cli, con = _core(snapshot)
    asyncio.run(cli.cmd_risk("AAPL"))
    text = con.file.getvalue()
    assert "VaR 95% · 1 day" in text and "2.44%" in text and "-13.80%" in text and "+29.95%" in text
    snapshot("risk_local_en", text)


class _Ticker:
    def __init__(self):
        idx = pd.to_datetime(["2026-10-28 16:00", "2026-07-29 16:00", "2026-04-29 16:00"])
        self.earnings_dates = pd.DataFrame({"EPS Estimate": [4.72, 4.24, 4.07],
                                            "Reported EPS": [float("nan"), 4.74, 4.27],
                                            "Surprise(%)": [float("nan"), 11.81, 4.90]}, index=idx)
        self.calendar = {"Earnings Date": ["2026-10-29"], "Earnings Average": 4.72,
                         "Earnings Low": 4.43, "Earnings High": 4.83, "Revenue Average": 90.67e9}
        cols = pd.to_datetime(["2026-06-30", "2026-03-31"])
        self.quarterly_income_stmt = pd.DataFrame(
            {cols[0]: [90.007e9, 35.766e9, 4.81], cols[1]: [82.886e9, 31.778e9, 4.27]},
            index=["Total Revenue", "Net Income", "Diluted EPS"])


def test_earnings_shows_reports_against_estimates(snapshot):
    from aria_code.apps.cli.earnings_view import fetch_earnings, render_earnings

    data = fetch_earnings("MSFT", ticker=_Ticker())
    assert [r["date"] for r in data["history"]] == ["2026-07-29", "2026-04-29"]   # no future row
    assert data["next"]["date"] == "2026-10-29"
    con = snapshot.console()
    render_earnings(data, con, "en")
    snapshot("earnings_en", con.file.getvalue())


def test_a_symbol_without_earnings_says_so():
    from aria_code.apps.cli.earnings_view import fetch_earnings

    empty = SimpleNamespace(earnings_dates=None, calendar={}, quarterly_income_stmt=None)
    result = fetch_earnings("SPY", ticker=empty)
    assert result["success"] is False and "No earnings data for SPY" in result["error"]
