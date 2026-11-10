"""A market question on the Arthera backend route gets data, not a refusal.

"分析苹果股票" on gemini-3.8-flash via the Arthera API ended with "This
request requires current verified financial data, but no usable tool result
was obtained". Three faults met there:

- That route (AriaSSEProvider) is sent no local tool schemas, yet the turn
  counted as tool-capable, which skipped the local market pre-fetch too — so
  nothing fetched any data, and the evidence gate refused.
- When the pre-fetch did run and the model was asked to comment, the prompt
  said "the data above" while the turn's history (conversation[:-1]) had just
  dropped that data, the last entry.
- The evidence rule took "API" and "CSV" for tickers and missed "分析AAPL",
  "分析一下特斯拉" and "How's NVDA this week?".
"""

from __future__ import annotations

import inspect

import pytest

from aria_code.apps.cli.prompt_assembly import (
    ANALYSIS_COMMENTARY_PROMPT_EN,
    ANALYSIS_COMMENTARY_PROMPT_ZH,
    build_base_message,
)
from aria_code.apps.cli.providers.chat_routing import model_receives_local_tools
from aria_code.packages.aria_services.research_protocol import requires_financial_evidence


def test_the_backend_route_receives_no_local_tools():
    backend = {"backend_chat": True}
    assert not model_receives_local_tools("gemini-3.8-flash", backend, "https://api.arthera.xyz")
    assert model_receives_local_tools("google/gemini-2.5-pro", {}, None)
    assert model_receives_local_tools("qwen2.5:7b", {"local_provider": "ollama"}, None)


def test_send_message_asks_before_skipping_the_pre_fetch():
    import aria_code.aria_cli as cli

    source = inspect.getsource(cli.ArtheraTerminal.send_message)
    assert "model_receives_local_tools" in source


def test_the_commentary_prompt_carries_the_data():
    snapshot = "## Apple Inc. AAPL\n| 最新价 | USD 333.69 |"
    zh = build_base_message("分析苹果股票", wants_analysis_commentary=True, snapshot=snapshot, lang="zh")
    en = build_base_message("analyze AAPL", wants_analysis_commentary=True, snapshot=snapshot, lang="en")
    assert zh.startswith(ANALYSIS_COMMENTARY_PROMPT_ZH) and "USD 333.69" in zh
    assert en.startswith(ANALYSIS_COMMENTARY_PROMPT_EN) and "USD 333.69" in en


@pytest.mark.parametrize("question", [
    "分析苹果股票", "分析AAPL", "分析一下特斯拉", "How's NVDA this week?",
    "what is the current price of BTC", "000001 走势",
])
def test_market_questions_need_evidence(question):
    assert requires_financial_evidence(question)


@pytest.mark.parametrize("question", [
    "analyze the API latency trend", "Analyze this CSV file", "分析一下这段代码的风险",
    "What does Apple make?", "Review PR 12",
])
def test_other_questions_do_not(question):
    assert not requires_financial_evidence(question)
