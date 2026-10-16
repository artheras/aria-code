from aria_code.packages.aria_services.research_protocol import (
    grounding_tool_names,
    requires_financial_evidence,
)


def test_financial_evidence_intent_is_conservative():
    assert requires_financial_evidence("分析苹果股票走势和成交量")
    assert requires_financial_evidence("What is the current AAPL price?")
    assert not requires_financial_evidence("Explain what a Sharpe ratio is")


def test_grounding_tool_names_support_local_and_mcp_schemas():
    schemas = [
        {"name": "get_market_data"},
        {"name": "read_file"},
        {
            "type": "function",
            "function": {"name": "mcp__arthera_quant_engine__run_backtest"},
        },
    ]

    assert grounding_tool_names(schemas) == frozenset({
        "get_market_data",
        "mcp__arthera_quant_engine__run_backtest",
    })


# ── Ordinary words are not financial subjects ────────────────────────────────
# The subject list used to hold everyday words — market, share, option, volume,
# price, holding, fund, 市场, 组合, 加密 — so a question about the user's own
# product or code was gated as a market question and refused after the turn
# had done its real work, because no market-data tool had run. A turn that read
# the project structure and was then refused for 179 seconds is how this
# surfaced.

import pytest  # noqa: E402


@pytest.mark.parametrize("question", [
    "分析一下这个项目的市场定位",
    "帮我写一份产品的市场分析报告",
    "analyze the performance of the share button",
    "what is the current volume setting in the player",
    "how is option parsing doing in the CLI",
    "分析当前加密算法的风险",          # 加密 alone is encryption
    "分析一下用户组合功能的表现",      # 组合 is any combination
    "how is our marketing doing this week",   # market is inside marketing
    "analyze the inequity in current data",   # equity is inside inequity
])
def test_a_question_about_the_users_own_work_is_not_a_market_question(question):
    assert not requires_financial_evidence(question)


@pytest.mark.parametrize("question", [
    "how is the market doing today",
    "今天市场怎么样",
    "大盘走势",
    "美股今天表现怎么样",
])
def test_a_question_about_the_market_itself_still_needs_evidence(question):
    # Narrowing the list must not open the door the gate exists to close: a
    # conclusion about today's market with no data behind it.
    assert requires_financial_evidence(question)


def test_a_word_inside_a_longer_word_does_not_count_as_context():
    # "app" is inside "happening"; matched as a substring it would veto a real
    # market question as if it were about the user's app.
    assert requires_financial_evidence("what is happening in the market today")
