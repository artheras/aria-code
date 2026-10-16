"""Financial-research grounding policy shared by Aria adapters."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


_MARKET_IDENTIFIER = re.compile(
    r"\b[A-Z]{1,5}(?:\.[A-Z]{1,4})?\b|\b\d{6}(?:\.(?:SH|SZ|SS|HK))?\b"
)
# Words that name a financial subject on their own. Everyday words used to be
# in this list too — market, share, option, volume, price, holding, fund, and
# 市场, 组合, 加密 — so "analyze the performance of the share button", "how is
# option parsing doing", "分析一下这个项目的市场定位" and "分析当前加密算法的风险"
# (加密 alone is encryption) were all gated as market questions, and refused
# after the turn had already done its real work, because no market-data tool
# had run. Only terms that mean finance wherever they appear belong here.
_FINANCIAL_SUBJECTS = (
    "stock", "equity", "equities", "portfolio", "crypto", "bitcoin", "forex",
    "etf", "dividend", "earnings", "ticker", "share price", "stock market",
    "market cap", "trading volume", "bond yield", "mutual fund", "index fund",
    "hedge fund", "options chain", "call option", "put option", "price target",
    "股票", "股价", "股市", "大盘", "a股", "美股", "港股", "行情", "持仓", "基金",
    "期权", "债券", "外汇", "加密货币", "数字货币", "币价", "成交量", "市值",
    "财报", "基本面", "涨跌", "收盘价", "开盘价", "目标价",
)

# Words that are financial only some of the time: "how is the market doing
# today" is a market question, "这个项目的市场定位" is not. They count when the
# message asks about now and says nothing that places it in the user's own
# product or code.
_AMBIGUOUS_SUBJECTS = ("market", "price", "valuation", "市场", "价格", "估值")
_TIME_INTENTS = (
    "today", "current", "latest", "this week", "right now", "how's", "how is",
    "doing", "今天", "当前", "最新", "本周", "这周", "近期", "现在", "走势", "怎么样",
)
_WORKSPACE_CONTEXT = (
    "project", "product", "code", "app", "function", "button", "feature",
    "repo", "file", "api", "cli", "page", "plan", "pricing", "subscription",
    "competitor", "positioning", "user",
    "项目", "产品", "代码", "函数", "功能", "页面", "应用", "仓库", "文件",
    "定位", "竞品", "用户", "方案", "套餐",
)

_EVIDENCE_INTENTS = (
    "analyze", "analysis", "forecast", "predict", "recommend", "buy", "sell",
    "current", "latest", "today", "trend", "risk", "target", "outlook",
    "this week", "how's", "how is", "doing", "performance",
    "分析", "预测", "推荐", "买入", "卖出", "最新", "当前", "今天", "走势",
    "趋势", "风险", "目标价", "前景", "成交量", "全面分析",
    "本周", "这周", "近期", "怎么样", "表现", "看法", "值得",
)
_GROUNDING_TOOL_FRAGMENTS = (
    "market", "quote", "price", "ohlcv", "fundamental", "filing", "news",
    "factor", "risk", "backtest", "portfolio", "prediction", "signal",
    "regime", "financial", "earnings", "valuation", "macro", "flow",
)


def _mentions(lowered: str, terms: Iterable[str]) -> bool:
    """Whether any term occurs — as a word for English, as a substring for CJK.

    Substrings are wrong for short English words in both directions: "market"
    is inside "marketing" and "equity" inside "inequity", while "app" is inside
    "happening" and "api" inside "capital". Chinese has no spaces to bound a
    word, so it keeps substring matching.
    """
    for term in terms:
        if term.isascii():
            if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lowered):
                return True
        elif term in lowered:
            return True
    return False


def _names_an_instrument(text: str) -> bool:
    """A ticker, code or company the finance pack recognises, or a named company.

    This used to be a bare regex, \\b[A-Z]{1,5}\\b: it took "API" and "CSV"
    for tickers ("analyze the API latency trend" was gated as a market
    question and refused without market data), and missed "分析AAPL" — no word
    boundary between 析 and A — and every company named in Chinese.
    """
    try:
        from aria_code.packs import activate_packs, load_builtin_packs

        load_builtin_packs()
        if any(a.pack == "finance" for a in activate_packs(text)):
            return True
    except Exception:
        if _MARKET_IDENTIFIER.search(text):
            return True
    try:
        from aria_code.apps.cli.intent_signals import _ENTITY_TERMS
    except Exception:
        return False
    lowered = text.lower()
    return any(term in lowered for term in _ENTITY_TERMS)


def requires_financial_evidence(query: str) -> bool:
    """Return whether a request needs current external financial evidence."""
    text = str(query or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if not any(term in lowered for term in _EVIDENCE_INTENTS):
        return False
    if _mentions(lowered, _FINANCIAL_SUBJECTS) or _names_an_instrument(text):
        return True
    return (
        _mentions(lowered, _AMBIGUOUS_SUBJECTS)
        and _mentions(lowered, _TIME_INTENTS)
        and not _mentions(lowered, _WORKSPACE_CONTEXT)
    )


def grounding_tool_names(tool_schemas: Iterable[Mapping]) -> frozenset[str]:
    """Select registered tools whose outputs can ground a financial conclusion."""
    names: set[str] = set()
    for schema in tool_schemas:
        function = schema.get("function") if isinstance(schema, Mapping) else None
        name = str(
            (function or {}).get("name")
            if isinstance(function, Mapping)
            else schema.get("name", "")
        )
        canonical = name.rsplit("__", 1)[-1].lower()
        if name and any(fragment in canonical for fragment in _GROUNDING_TOOL_FRAGMENTS):
            names.add(name)
    return frozenset(names)

