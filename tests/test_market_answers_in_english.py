"""A market question asked in English gets data, and gets it in English.

#93 made "分析苹果股票" on the Arthera backend fetch data instead of being
refused. "How's NVDA this week?" still was: the gate demanded data, but the
snapshot handler's word lists were Chinese ("分析", "走势", "行情"), so it did not
take the question, and a backend model has no tools to fetch anything. When a
reply did come, its frame and parts of its body stayed Chinese ("市场快照 ·
本内容不构成投资建议", "50.7，中性", "1-5 日"). And "Should I sell my Tesla
shares?" was compared against a stock called ``I``.

"analyze Apple stock" goes to /analyze in the CLI, which does fetch the data
and puts it in the prompt — and the gate still refused the model's answer,
because the data had not come from a tool call in that turn.
"""

from __future__ import annotations

import asyncio
import builtins
import re
from types import SimpleNamespace

import pytest

import aria_code.apps.cli.deterministic as deterministic
import aria_code.apps.cli.handlers.market_handlers as market_handlers
from aria_code.apps.cli.utils.market_detect import (
    _extract_market_symbol,
    _extract_market_symbols,
    _is_market_snapshot_request,
)

CJK = re.compile(r"[一-鿿，：（）]")


class _MarketData:
    """Quotes, indicators and history for one stock, with no network."""

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


@pytest.fixture
def offline_market(monkeypatch):
    real_import = builtins.__import__

    def no_yfinance(name, *args, **kwargs):
        if name == "yfinance":
            raise ImportError("offline")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_yfinance)
    monkeypatch.setattr(market_handlers, "_has_mdc_lazy", lambda: True)
    monkeypatch.setattr(market_handlers, "_get_mdc_lazy", lambda: _MarketData())
    monkeypatch.setattr(market_handlers, "_get_provider_key", lambda _provider: "")


@pytest.mark.parametrize("question", [
    "analyze Apple stock", "How's NVDA this week?", "Should I sell my Tesla shares before earnings?",
])
def test_whatever_the_gate_needs_data_for_is_a_snapshot_request(question):
    assert not _is_market_snapshot_request(question)
    assert _is_market_snapshot_request(question, evidence_required=True)


@pytest.mark.parametrize("question", [
    "write a backtest script for AAPL", "Show me the AAPL chart", "analyze the API latency trend",
])
def test_the_gate_does_not_turn_code_charts_or_no_ticker_into_a_snapshot(question):
    assert not _is_market_snapshot_request(question, evidence_required=True)


def test_the_chain_asks_the_gate_on_a_route_without_tools(monkeypatch):
    seen = {}

    def snapshot(message, history=None, *, evidence_required=False):
        seen[message] = evidence_required
        return {"success": False}

    monkeypatch.setattr(deterministic, "_try_handle_market_snapshot_analysis", snapshot)
    deterministic.run_deterministic_chain("analyze Apple stock", model_has_tools=False)
    deterministic.run_deterministic_chain("analyze the API latency trend", model_has_tools=False)
    assert seen == {"analyze Apple stock": True, "analyze the API latency trend": False}


def test_an_english_question_gets_an_english_snapshot(offline_market):
    result = deterministic.run_deterministic_chain("analyze Apple stock", model_has_tools=False)
    text = result["response"]
    assert result["success"] and result["symbol"] == "AAPL"
    assert "**Takeaway**: " in text and "| RSI(14) | 50.7 | Neutral |" in text
    assert "(1-5 days)" in text and "(1-8 weeks)" in text and "(2-12 months)" in text
    assert not CJK.findall(text), CJK.findall(text)


def test_a_chinese_question_still_gets_the_chinese_snapshot(offline_market):
    text = deterministic.run_deterministic_chain("分析苹果股票", model_has_tools=False)["response"]
    assert "**结论**：" in text and "| RSI(14) | 50.7，中性 | 中性 |" in text
    assert "（1-5 日）" in text and "**下一步**" in text


def test_an_english_failure_is_explained_in_english(monkeypatch):
    monkeypatch.setattr(market_handlers, "_has_mdc_lazy", lambda: False)
    text = market_handlers._try_handle_market_snapshot_analysis("AAPL price")["response"]
    assert "Run `/quote AAPL` to retry." in text and not CJK.findall(text)


def test_an_english_comparison_has_english_signal_labels(offline_market):
    text = market_handlers._try_handle_market_snapshot_analysis("AAPL vs MSFT price")["response"]
    assert "| AAPL |" in text and "| MSFT |" in text
    assert not CJK.findall(text), CJK.findall(text)


@pytest.mark.parametrize("question, symbols", [
    ("Should I sell my Tesla shares before earnings?", ["TSLA"]),
    ("I think AAPL is cheap, analyze it", ["AAPL"]),
    ("A good time to buy MSFT?", ["MSFT"]),
    ("Should I sell T now", ["T"]),
    ("Is C a buy?", ["C"]),
])
def test_the_pronoun_and_the_article_are_not_tickers(question, symbols):
    assert _extract_market_symbols(question) == symbols
    assert _extract_market_symbol(question) == symbols[0]


def test_a_blocked_word_still_ends_the_single_symbol_search():
    assert _extract_market_symbol("分析A股") == "A"
    assert _extract_market_symbol("Run the API tests for CI") == ""


def test_the_repeat_notice_follows_the_question():
    from aria_code.aria_cli import _build_market_snapshot_repeat_notice, _market_snapshot_cache_entry

    result = {"symbol": "AAPL", "name": "Apple Inc.", "price": 195.2, "change_pct": 0.8, "currency": "USD",
              "signal": "HOLD+", "support": "USD 190.00", "resistance": "USD 200.00", "as_of": "2026-10-05"}
    previous = _market_snapshot_cache_entry(result, now=100.0)
    english = _build_market_snapshot_repeat_notice(result, previous, now=120.0, lang="en")
    assert "**Unchanged**" in english and "`/quote AAPL`" in english and not CJK.findall(english)
    assert "行情未变化" in _build_market_snapshot_repeat_notice(result, previous, now=120.0)


@pytest.mark.parametrize("context, grounded", [
    ("## AAPL Market Data\n\n### Data Quality\n- Status: complete\n- Price: 333.69  (+0.87%)  [Apple Inc.]", True),
    ("## 600519 市场数据\n- 价格: 1452.00  (-0.35%)", True),
    ("## AAPL Market Data\n- Price: unavailable (configure a data service key via /apikey)", False),
    ("## 600519 市场数据\n- 价格: 获取失败（稍后重试或配置数据服务 key）", False),
    ("CTX", False),
])
def test_an_analyze_context_grounds_the_answer_only_with_a_price(context, grounded):
    from aria_code.apps.cli.commands.market_context import context_has_price

    assert context_has_price(context) is grounded


def test_analyze_tells_the_gate_whether_its_prompt_carries_data(monkeypatch):
    import aria_code.apps.cli.commands.analysis_cmds as analysis_cmds

    monkeypatch.setattr(analysis_cmds, "build_analyze_prompt", lambda symbol, ctx, is_cn, response_lang=None: ctx)
    monkeypatch.setattr(analysis_cmds, "_is_ashare_symbol", lambda _symbol: False)
    sent = []

    class Terminal:
        conversation: list = []

        async def send_message(self, prompt, evidence_grounded=False):
            sent.append(evidence_grounded)

    class Cli(analysis_cmds.AnalysisCommandsMixin):
        context = SimpleNamespace(has_rich=False, console=None)
        terminal = Terminal()

        def __init__(self, fetched):
            self.fetched = fetched

        async def _build_analyze_context(self, symbol, is_cn):
            return self.fetched

    asyncio.run(Cli("## AAPL Market Data\n- Price: 333.69  (+0.87%)").cmd_analyze("AAPL"))
    asyncio.run(Cli("## AAPL Market Data\n- Price: unavailable (configure a data service key via /apikey)")
                .cmd_analyze("AAPL"))
    assert sent == [True, False]


def test_the_snapshot_header_states_the_session_on_the_exchange_clock(offline_market, monkeypatch):
    monkeypatch.setattr(market_handlers, "market_session", lambda _symbol: "closed")
    header = market_handlers._try_handle_market_snapshot_analysis("AAPL price")["response"].splitlines()[1]
    assert "· Market closed ·" in header
    monkeypatch.setattr(market_handlers, "market_session", lambda _symbol: None)
    header = market_handlers._try_handle_market_snapshot_analysis("AAPL price")["response"].splitlines()[1]
    assert "Market" not in header and "· ·" not in header and header.endswith("Not investment advice*")


def test_takeaway_and_levels_render_as_two_lines(offline_market):
    text = market_handlers._try_handle_market_snapshot_analysis("AAPL price")["response"]
    assert re.search(r"\*\*Takeaway\*\*: .+  \n\*\*Levels\*\*: Watch ", text)
    assert "**Watch**" not in text


def test_each_timeframe_takes_two_lines(offline_market):
    """The snapshot ran to about 48 lines; each timeframe took four."""
    text = market_handlers._try_handle_market_snapshot_analysis("AAPL price")["response"]
    block = text.split("**Multi-timeframe key levels**", 1)[1].split("\n\n", 2)[0].strip().splitlines()
    assert len(block) == 6
    assert block[0].startswith("- **4H/Short-term** (1-5 days) — support USD ")
    assert " / " in block[0] and " · resistance USD " in block[0]


def test_the_currency_rule_matches_the_data():
    """The model was told to write "$" while the data it was given says USD."""
    from aria_code.apps.cli.prompts.system_prompts import build_response_style_rule

    en, zh = build_response_style_rule("en"), build_response_style_rule("zh")
    assert "USD for US assets" in en and "'$'" not in en
    assert "美股（如 AAPL、TSLA）用 USD" in zh and "'$'" not in zh
