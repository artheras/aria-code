"""What the terminal shows, kept as text in tests/snapshots/ and compared exactly.

Most fixes here were found by recording a live session and reading the
screen: an English /portfolio in Chinese with a log line and two verdicts, a
/compare of four identical strategies, a dividend yield of 32%. The other
tests check pieces; these check the screen. A change to one of these outputs
fails until it is looked at: if it is meant, run

    ARIA_UPDATE_SNAPSHOTS=1 pytest tests/test_terminal_snapshots.py

and review the diff in tests/snapshots/ like any other change.

Everything runs offline on fixed data, at 100 columns, without colour.
"""

from __future__ import annotations

import asyncio
import builtins
import re
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest


def _text(console) -> str:
    return console.file.getvalue()


# ── /team ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("lang", ["en", "zh"])
def test_team_tree(lang, snapshot):
    from aria_code.ui.render.team import render_agent_node, render_agent_synthesis_leaf, render_agent_tree_root

    con = snapshot.console()
    render_agent_tree_root(con, "AAPL", 4, lang=lang)
    render_agent_node(con, "technical", "BUY", "RSI 58 · MACD above signal · price above MA20", lang=lang)
    render_agent_node(con, "fundamental", "HOLD", "PE 38.1 above sector median", degraded=True, lang=lang)
    render_agent_node(con, "news", None, None, success=False, error="rate_limited", lang=lang)
    render_agent_node(con, "risk", "SELL", "Annualised volatility 31% with a drawdown of 24% over the "
                                           "past year, well above the index", lang=lang)
    render_agent_synthesis_leaf(con, "HOLD", 0.45, 81.9, lang=lang)
    snapshot(f"team_tree_{lang}", _text(con))


@pytest.mark.parametrize("verdict", ["HEALTHY", "NEEDS_ATTENTION", "HIGH_RISK", "BUY", "SELL"])
def test_verdict_banner(verdict, snapshot):
    from aria_code.ui.render.team import render_verdict_banner

    con = snapshot.console()
    render_verdict_banner(verdict, "5 symbols, equal weight · Annualised portfolio volatility 22.0% (medium)",
                          0.65, console=con, lang="en")
    snapshot(f"verdict_{verdict.lower()}", _text(con))


# ── /portfolio ───────────────────────────────────────────────────────────────

STATS = {
    "valid_symbols": ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"], "weight_source": "equal",
    "port_vol_ann": 0.22, "div_ratio": 1.57, "high_corr": [{"sym1": "AAPL", "sym2": "MSFT", "corr": 0.74}],
}


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_portfolio(lang, snapshot, monkeypatch):
    import aria_code.aria_cli as cli
    import aria_code.apps.cli.commands.portfolio_cmds as portfolio_cmds
    import aria_code.apps.cli.commands.team as team
    from agents.portfolio_agent import PortfolioAgent

    con = snapshot.console()
    monkeypatch.setattr(cli, "console", con)
    monkeypatch.setattr(cli, "HAS_RICH", True)
    monkeypatch.setattr(team, "build_team_llm_provider", lambda config: None)

    async def run_portfolio(self, symbols, weights=None):
        return await self.analyze_portfolio(symbols, dict(STATS))

    monkeypatch.setattr(PortfolioAgent, "run_portfolio", run_portfolio)
    monkeypatch.setattr(sys, "stdout", con.file)

    class Cli(portfolio_cmds.PortfolioCommandsMixin):
        context = SimpleNamespace(has_rich=True, console=con)
        terminal = SimpleNamespace(config={"ui_lang": lang,
                                           "watchlist": ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"]})

    monkeypatch.setitem(sys.modules, "portfolio_ledger", None)   # no recorded positions
    asyncio.run(Cli().cmd_portfolio(""))
    snapshot(f"portfolio_{lang}", _text(con))


# ── /compare and /peer ───────────────────────────────────────────────────────

def test_compare_local(snapshot, monkeypatch):
    import aria_code.apps.cli.commands.data_cmds as data_cmds

    results = {
        "momentum": (0.181, 0.831, -0.184, 1.15, 0.39, 24),
        "rsi_mean_revert": (0.145, 0.771, -0.113, 0.75, 0.83, 6),
        "sma_cross": (0.216, 1.060, -0.151, 1.22, 1.00, 4),
        "buy_hold": (0.424, 1.578, -0.166, 2.50, 0.0, 1),
    }

    def backtest(params):
        ann, sharpe, mdd, sortino, win, trades = results[params["strategy"]]
        return {"success": True, "data": {"annual_return": ann, "sharpe_ratio": sharpe, "max_drawdown": mdd,
                                          "sortino_ratio": sortino, "win_rate": win, "total_trades": trades,
                                          "start": "2023-01-03", "end": "2024-12-31"}}

    monkeypatch.setattr(data_cmds, "_get_LOCAL_TOOLS", lambda: {"backtest_strategy": (backtest, "")})
    con = snapshot.console()

    class Cli(data_cmds.DataCommandsMixin):
        context = SimpleNamespace(has_rich=True, console=con)
        terminal = SimpleNamespace(config={"ui_lang": "en", "api_url": "http://127.0.0.1:9"})

    asyncio.run(Cli().cmd_compare("AAPL 2023-01-01 2025-01-01"))
    snapshot("compare_local_en", _text(con))


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_peer(lang, snapshot, monkeypatch):
    from aria_code.tools import local_finance_tools as lft
    from aria_code.ui.render.finance import render_peer_comparison

    infos = {
        "AAPL": {"trailingPE": 38.1, "priceToBook": 45.19, "returnOnEquity": 1.488, "shortName": "Apple Inc.",
                 "marketCap": 4.854e12, "regularMarketPrice": 325.0, "trailingAnnualDividendYield": 0.0032,
                 "sector": "Technology"},
        "MSFT": {"trailingPE": 29.7, "priceToBook": 8.94, "returnOnEquity": 0.34, "shortName": "Microsoft Corp",
                 "marketCap": 3.953e12, "regularMarketPrice": 500.0, "trailingAnnualDividendYield": 0.0069},
        "GOOGL": {"trailingPE": 17.4, "priceToBook": 6.83, "returnOnEquity": 0.487, "shortName": "Alphabet Inc",
                  "marketCap": 4.251e12, "regularMarketPrice": 250.0, "trailingAnnualDividendYield": 0.0025},
    }
    monkeypatch.setattr(lft, "yf", SimpleNamespace(Ticker=lambda s: SimpleNamespace(info=infos[s])), raising=False)
    monkeypatch.setattr(lft, "_HAS_YF", True, raising=False)
    result = lft._peer_comparison({"symbol": "AAPL", "peers": ["MSFT", "GOOGL"], "lang": lang})
    con = snapshot.console()
    render_peer_comparison(result, console=con, has_rich=True)
    snapshot(f"peer_{lang}", _text(con))


# ── run_command and wrapped text ─────────────────────────────────────────────

def test_command_outcome(snapshot):
    from aria_code.apps.cli.tools.system_tools import print_command_outcome

    con = snapshot.console()
    print_command_outcome(con, 0, "collected 12 items\n\n12 passed in 0.41s\n", "")
    print_command_outcome(con, 1, "", "Traceback (most recent call last):\n  File \"calc.py\", line 2\n"
                                      "NameError: name 'np' is not defined\n")
    snapshot("command_outcome", _text(con))


def test_hanging_wrap(snapshot):
    from aria_code.ui.render.output import print_hanging

    con = snapshot.console(width=60)
    print_hanging(con, "  ! ", "Turns go to Arthera cloud chat (backend_chat), where the model cannot read "
                               "this folder or run commands; anything it says about files here is a guess.")
    print_hanging(con, "  └ ", "Usage: /compare SYMBOL [YYYY-MM-DD] [YYYY-MM-DD] — brackets kept, not markup")
    snapshot("hanging_wrap", _text(con))


# ── Market snapshot (an English question) ────────────────────────────────────

class _MarketData:
    def quote(self, symbol):
        return {"success": True, "symbol": symbol, "name": "Apple Inc.", "price": 195.2, "change_pct": 0.8,
                "market_cap": 2_990_000_000_000, "high": 197.0, "low": 193.0, "currency": "USD",
                "provider": "test", "provider_chain": ["test"]}

    def fundamentals(self, symbol):
        return {"success": True, "provider": "test", "market_cap": 2_990_000_000_000}

    def technical_indicators(self, *args, **kwargs):
        return {"success": True, "provider": "test", "rsi": 50.7, "macd_hist": -0.18,
                "ma20": 194.4, "ma60": 188.7, "bb_upper": 207.5, "bb_lower": 184.2}

    def history(self, symbol, days=252, interval="1d"):
        count = 96 if interval == "1h" else 260
        records = []
        for i in range(count):
            close = 185.0 + (i % 30) * 0.4 + i * 0.02
            records.append({"date": f"2026-01-{(i % 28) + 1:02d}", "open": close - 0.3,
                            "high": close + 1.2 + (2.4 if i % 17 == 0 else 0),
                            "low": close - 1.1 - (2.2 if i % 19 == 0 else 0),
                            "close": close, "volume": 1_000_000 + i})
        return {"success": True, "symbol": symbol, "data": records, "provider": "test"}


def test_market_snapshot_en(snapshot, monkeypatch):
    import aria_code.apps.cli.deterministic as deterministic
    import aria_code.apps.cli.handlers.market_handlers as market_handlers

    real_import = builtins.__import__

    def no_yfinance(name, *args, **kwargs):
        if name == "yfinance":
            raise ImportError("offline")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_yfinance)
    monkeypatch.setattr(market_handlers, "_has_mdc_lazy", lambda: True)
    monkeypatch.setattr(market_handlers, "_get_mdc_lazy", lambda: _MarketData())
    monkeypatch.setattr(market_handlers, "_get_provider_key", lambda _provider: "")
    monkeypatch.setattr(market_handlers, "market_session", lambda _symbol: "closed")
    text = deterministic.run_deterministic_chain("analyze Apple stock", model_has_tools=False)["response"]
    today = datetime.now().strftime("%Y-%m-%d")
    snapshot("market_snapshot_en", re.sub(re.escape(today) + r"( \d{2}:\d{2})?", "<today>", text))


# ── /help ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("lang", ["en", "zh"])
def test_help(lang, snapshot, monkeypatch):
    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli

    terminal = cli.ArtheraTerminal(dict(cli.DEFAULT_CONFIG, ui_lang=lang))
    con = snapshot.console()
    monkeypatch.setattr(terminal.commands.context, "console", con)
    monkeypatch.setattr(terminal.commands.context, "has_rich", True)
    terminal.commands.cmd_help("")
    snapshot(f"help_{lang}", _text(con))
