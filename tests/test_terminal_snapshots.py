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
