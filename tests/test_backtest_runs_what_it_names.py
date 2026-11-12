"""/backtest runs the strategy it names on the symbol it names, and counts wins per trade.

"/backtest AAPL sma" ran a strategy called "AAPL" on the ticker SMA; the
engine did not know "AAPL" (nor "sma") and ran buy-and-hold under that name,
so the result matched buy-and-hold to the decimal. The yfinance fallback ran
momentum whatever was asked. "Win Rate" was the share of up days while
holding: one buy-and-hold trade showed 52.5%. And error guidance passed to
print_error was never shown; a hint guessed from "rate" in "strategy" said
"Rate limited".
"""

from __future__ import annotations

import pytest

from aria_code.domain.backtest_report import BacktestConfig, normalize_strategy, run_backtest_from_history
from aria_code.ui.render.output import error_hint


def _history(closes):
    return [{"date": f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}", "close": c, "volume": 1000} for i, c in enumerate(closes)]


@pytest.mark.parametrize("name, runs", [("sma", "sma_cross"), ("ma_crossover", "sma_cross"),
                                        ("buy_and_hold", "buy_hold"), ("mom", "momentum"), ("AAPL", None)])
def test_strategy_names(name, runs):
    assert normalize_strategy(name) == runs


def test_an_unknown_strategy_is_an_error_not_buy_and_hold():
    closes = [100 + i for i in range(120)]
    result = run_backtest_from_history(_history(closes), BacktestConfig(symbol="AAPL", strategy="turtle",
                                                                        start_date="2025-01-01",
                                                                        end_date="2025-12-31"))
    assert result["success"] is False and "Unknown strategy 'turtle'" in result["error"]


def test_win_rate_counts_trades_not_days():
    # Rises then falls every 20 days: SMA crossings make several round trips.
    closes = []
    price = 100.0
    for i in range(240):
        price *= 1.01 if (i // 20) % 2 == 0 else 0.99
        closes.append(round(price, 4))
    cfg = BacktestConfig(symbol="X", strategy="sma", start_date="2025-01-01", end_date="2025-12-31",
                         fast_period=5, slow_period=10)
    result = run_backtest_from_history(_history(closes), cfg)
    assert result["success"]
    trades = result["total_trades"]
    assert trades >= 2
    # A win rate over trades is a multiple of 1/trades.
    assert abs(result["win_rate"] * trades - round(result["win_rate"] * trades)) < 1e-6

    hold = run_backtest_from_history(_history(closes), BacktestConfig(
        symbol="X", strategy="buy_hold", start_date="2025-01-01", end_date="2025-12-31"))
    assert hold["total_trades"] == 1 and hold["win_rate"] in (0.0, 1.0)


@pytest.mark.parametrize("message", ["Unknown strategy: turtle", "No strategy results", "generate failed",
                                     "only 1500 bars"])
def test_hints_are_not_guessed_from_parts_of_words(message):
    assert "Rate limited" not in error_hint(message)
    assert "Server error" not in error_hint(message)


def test_real_rate_limits_still_get_the_hint():
    assert "Rate limited" in error_hint("HTTP 429 Too Many Requests")
    assert "Rate limited" in error_hint("yfinance rate limit reached")


def test_the_callers_hint_is_shown(snapshot):
    from aria_code.ui.render.output import print_error

    con = snapshot.console()
    print_error("No broker configured", "请先用 /broker add 添加", console=con, has_rich=True)
    print_error("Login failed", "login", console=con, has_rich=True)
    text = con.file.getvalue()
    assert "请先用 /broker add 添加" in text
    assert "Check email/password" in text


def test_backtest_takes_either_order(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import aria_code.apps.cli.commands.backtest_cmds as backtest_cmds

    seen = {}

    def fake_report(config, output_dir=None):
        seen["strategy"], seen["symbol"] = config.strategy, config.symbol
        return {"success": False, "error": "stop here"}

    from aria_code.domain import backtest_report
    monkeypatch.setattr(backtest_report, "generate_backtest_report", fake_report)
    monkeypatch.setitem(__import__("sys").modules, "yfinance", None)
    printed = []
    console = SimpleNamespace(print=lambda *a, **k: printed.append(" ".join(map(str, a))),
                              status=lambda *a, **k: __import__("contextlib").nullcontext(), width=100)

    class Cli(backtest_cmds.BacktestCommandsMixin):
        context = SimpleNamespace(has_rich=True, console=console)
        terminal = SimpleNamespace(config={"ui_lang": "en", "api_url": "http://127.0.0.1:9"})

    asyncio.run(Cli().cmd_backtest("AAPL sma"))
    assert seen == {"strategy": "sma", "symbol": "AAPL"}
