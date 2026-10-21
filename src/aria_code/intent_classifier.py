"""
intent_classifier.py — Aria intent classification using Prelude model or keyword fallback.

Two-tier design:
  Tier 1 (fast, accurate): aria-prelude via Ollama — the model trained specifically
                           for intent routing (adapters: intent-route-control,
                           intent-rag_gate, intent-upgrade_gate, intent-clarify).
  Tier 2 (instant, offline): keyword regex — exact same logic as before, used when
                              Ollama / aria-prelude is not available.

Intent labels (matching CODING_SYSTEM_PROMPT routing in aria_cli.py):
  "coding"      → code generation, backtest scripts, chart scripts
  "analysis"    → stock/macro analysis, technical/fundamental research
  "realtime"    → live price / quote / market data queries (needs tool)
  "general"     → product, engineering, document, and general questions
  "finance"     → explicit finance chat with market-aware tool access

Usage::

    from aria_code.intent_classifier import classify_intent, INTENT_CODING, INTENT_ANALYSIS

    intent = await classify_intent_async("写一个 AAPL 动量策略", ollama_url)
    # → "coding"

    intent = classify_intent_sync("什么是夏普比率")
    # → "general"
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.request
from typing import Optional

# ── Intent constants ──────────────────────────────────────────────────────────
INTENT_CODING   = "coding"
INTENT_ANALYSIS = "analysis"
INTENT_REALTIME = "realtime"
INTENT_GENERAL  = "general"
INTENT_FINANCE  = "finance"

# ── Prelude model name ────────────────────────────────────────────────────────
_PRELUDE_MODEL = "aria-prelude"
_PRELUDE_TIMEOUT = 3.0   # seconds — must be fast; fallback on timeout

# ── Prelude system prompt (mirrors the adapter training format) ───────────────
_PRELUDE_SYSTEM = (
    "You are an intent classifier for a general product and software-engineering AI assistant.\n"
    "Classify the user message into EXACTLY ONE of these labels:\n"
    "  coding    — code generation, debugging, repository review, product implementation\n"
    "  analysis  — stock analysis, market research, technical/fundamental analysis\n"
    "  realtime  — live price, current quote, today's market data\n"
    "  general   — product review, architecture, UX, documentation, or general question\n"
    "  finance   — explicit finance, portfolio, risk, or investment discussion\n"
    "Reply with ONLY the label word, nothing else."
)


# ── Keyword fallback (tier-2, instant) ───────────────────────────────────────

_CODING_KW = (
    "write", "generate", "create", "script", "code", "plot", "backtest",
    "策略", "代码", "回测", "编写", "生成", "k线", "k-line", "kline",
    "python", "dashboard", "写一个", "生成代码", "写代码", "编写代码",
    "analyze and save", "analysis script",
    "review code", "code review", "review project", "audit project", "audit code",
    "repository", "repo", "codebase", "pull request", "debug", "refactor", "fix bug",
    "implement", "implementation", "react", "next.js", "nextjs", "vue", "swiftui",
    "flutter", "fastapi", "django", "express", "typescript", "frontend", "backend",
    "审核代码", "代码审查", "审核项目", "项目审核", "审查项目", "代码库", "仓库",
    "调试", "修复 bug", "重构", "实现", "实现功能", "开发产品", "编写应用",
    "前端", "后端", "登录页面", "单元测试", "集成测试",
)

_VISUAL_ARTIFACT_KW = (
    "图表", "走势图", "k线图", "k线", "k-line", "kline", "candlestick",
    "chart", "plot", "dashboard", "看板", "晨报", "日报", "周报", "月报",
    "report", "热力图", "heatmap",
)

_VISUAL_MARKET_CONTEXT_KW = (
    "股票", "股价", "行情", "市场", "美股", "港股", "a股", "指数",
    "持仓", "portfolio", "回测", "财报", "earnings", "基金", "etf",
    "资产", "组合", "市场数据", "market data",
)
_ANALYSIS_KW = (
    "analyze", "analysis", "分析", "研究", "评估", "研判",
    "技术面", "基本面", "走势", "趋势", "行情",
    "stock analysis", "technical analysis", "fundamental",
    "valuation", "estimate", "outlook", "投资建议", "买入", "卖出",
)

# Topics that must NOT be classified as stock technical "analysis" even if they
# contain analysis keywords.  They get routed to "finance" (general chat) instead,
# because the stock-analysis prompt requires injected market data that doesn't
# exist for real estate or pure macroeconomic questions.
_NON_STOCK_ANALYSIS_TOPICS = (
    # Real-estate
    "房价", "楼市", "房产", "房地产", "租金", "二手房", "折旧价", "商铺",
    # Pure macro — "宏观角度分析" should be finance chat, not stock template
    "宏观", "宏观经济", "宏观政策", "宏观角度",
    "货币政策", "财政政策", "gdp", "通胀", "通货膨胀", "cpi", "ppi",
    # Non-chartable commodities / currencies
    "黄金走势", "原油走势", "汇率走势", "美元指数",
)

# Pure macro/conceptual topics — no live market data needed, route to GENERAL
# (no tools invoked).  More specific than _NON_STOCK_ANALYSIS_TOPICS: real-estate
# still goes to FINANCE because the user might want live data, but "宏观角度" is
# clearly a discussion question, not a quote lookup.
_MACRO_GENERAL_TOPICS = (
    "宏观", "宏观经济", "宏观政策", "宏观角度", "宏观分析",
    "货币政策", "财政政策", "gdp", "通胀", "通货膨胀", "cpi", "ppi",
    "值得投资吗", "应该投资吗", "是否值得", "投资逻辑",
    "长期展望", "未来前景", "宏观前景",
    # Interest rate / bond macro concepts
    "利率", "加息", "降息", "美联储政策", "央行政策",
    "债券市场", "利差", "收益率曲线", "国债收益",
    # Structural/sector macro
    "产业政策", "行业监管", "政策影响", "监管政策",
)
_REALTIME_KW = (
    "今天", "today", "现在", "now", "current", "latest", "最新",
    "市值", "price", "股价", "quote", "行情", "涨跌", "涨幅",
    "market cap", "how much", "what is the price",
    "是多少", "多少钱", "多少点", "多少美元", "多少港元",
)

# Question words that, when combined with finance concept terms, mean "explain X"
# rather than "look up X" — should route to general, not finance.
_QUESTION_PREFIX = (
    "什么是", "什么叫", "how does", "what is", "what are",
    "explain", "define", "解释", "定义", "概念", "原理", "介绍",
    "是什么", "为什么", "区别", "difference", "如何理解", "怎么理解",
)

# Finance-metric terms that combined with realtime words → live lookup
_METRIC_KW = (
    "pe", "pb", "ps", "市盈率", "市净率", "市销率",
    "eps", "净利润", "营收", "市值", "股息", "分红",
    "ebitda", "利润率", "毛利率", "roe", "roa",
)

# File path extensions — presence means it's a document/code task, not stock analysis
_FILE_EXT_RE = r'\S+\.(?:docx|pdf|xlsx|pptx|txt|csv|json|py|md|log)\b'

# Specific financial entity signals — must be present for "分析" to route as stock analysis.
# Keep to SPECIFIC company names and ticker symbols only.
# Generic market categories ("美股", "债券", "股市") must NOT be here — they appear in
# macro conceptual questions and would incorrectly block the → general route.
_FIN_ENTITY_KW = (
    # Company names (CN)
    "苹果", "谷歌", "英伟达", "微软", "特斯拉", "亚马逊", "腾讯",
    "阿里", "百度", "比亚迪", "茅台", "招商银行", "中国平安", "恒生指数",
    "华为", "小米", "美团", "京东", "字节", "滴滴",
    # Company names (EN)
    "apple", "google", "nvidia", "microsoft", "tesla", "amazon",
    "meta", "netflix", "palantir", "snowflake",
    # Common tickers (lowercase)
    "aapl", "nvda", "msft", "tsla", "amzn", "googl", "meta", "baba",
    "spy", "qqq", "iwm", "dia", "gld", "uso",
    # Specific crypto coins (named assets, not generic "加密货币")
    "比特币", "以太坊", "bitcoin", "ethereum", "btc", "eth", "sol", "bnb",
    # Named indices (specific, not generic "指数")
    "纳斯达克", "标普500", "道琼斯", "沪深300", "中证500",
)

# Broader market terms (generic) — used only to check for stock-analysis context,
# NOT used to block macro-general classification (avoid false negatives on conceptual Qs)
_MARKET_GENERAL_KW = (
    "股票", "股市", "股价", "美股", "港股", "a股",
    "etf", "基金", "指数", "期货", "期权", "加密", "数字货币",
)
_GENERAL_KW = (
    "什么是", "what is", "what are", "how does", "explain", "define",
    "解释", "定义", "概念", "原理", "介绍", "怎么", "如何理解",
    "是什么", "为什么", "区别", "difference between",
    "tell me about", "describe", "how to", "举例", "example",
)
_FINANCE_CONCEPT_KW = (
    "dcf", "pe", "pb", "ps", "ev", "ebitda", "wacc", "capm",
    "beta", "alpha", "sharpe", "sortino", "var", "cvar", "drawdown",
    "black-scholes", "期权", "期货", "衍生品", "套利",
    "量化", "quant", "回测", "因子", "ic值", "ir值",
    "market cap", "市盈率", "市净率", "净利润", "估值", "valuation",
)


def is_visual_market_artifact_request(message: str) -> bool:
    """Return True for finance-adjacent visual artifact requests.

    These should prefer chart/dashboard/report workflows instead of generic
    market-data prefetch.

    Delegates to apps/cli/intent_signals, which matches CJK terms against a
    whitespace-stripped message so "K 线图" and "K线图" behave identically.
    The legacy body below is the fallback if that module is unavailable.
    """
    try:
        from aria_code.apps.cli.intent_signals import is_visual_artifact_request
        return is_visual_artifact_request(message)
    except Exception:
        pass
    low = message.lower().strip()
    if low.startswith(("/chart", "/dashboard", "/report")):
        return True
    if not any(k in low for k in _VISUAL_ARTIFACT_KW):
        return False
    if any(k in low for k in _VISUAL_MARKET_CONTEXT_KW):
        return True
    if any(k in low for k in _MARKET_GENERAL_KW):
        return True
    if any(e in low for e in _FIN_ENTITY_KW):
        return True
    return any(k in low for k in ("公司", "集团", "股份", "科技", "银行", "证券", "能源", "汽车"))


def classify_intent_sync(message: str) -> str:
    """
    Tier-2 classification (synchronous, always available).
    Returns one of the INTENT_* constants.

    Delegates to apps/cli/intent_signals, which extracts signals independently
    and applies one explicit precedence table.  The keyword cascade kept below
    is the fallback if that module cannot be imported; it is the historical
    implementation and is known to misclassify the cases listed in
    tests/test_intent_signals.py.
    """
    try:
        from aria_code.apps.cli.intent_signals import classify as _classify
        return _classify(message)
    except Exception:
        pass

    import re as _re
    low = message.lower().strip()

    if is_visual_market_artifact_request(message):
        return INTENT_CODING

    # Bug ⑥ — file path present → document/code task, never stock analysis template
    if _re.search(_FILE_EXT_RE, low):
        if any(k in low for k in _CODING_KW):
            return INTENT_CODING   # "帮我写一个解析这个 csv 的脚本"
        return INTENT_GENERAL      # document/product analysis without market tools

    has_coding = any(k in low for k in _CODING_KW)
    has_realtime = any(k in low for k in _REALTIME_KW)
    has_question = any(q in low for q in _QUESTION_PREFIX)

    # Coding intent — but skip if phrased as a conceptual question ("X的核心是什么")
    if has_coding and not has_question:
        return INTENT_CODING

    # Bug ⑤ — "metric是多少/how much" → realtime lookup, not finance concept chat
    if any(m in low for m in _METRIC_KW) and any(k in low for k in _REALTIME_KW):
        return INTENT_REALTIME

    # Live-data terms must win over broad analysis words.  "分析苹果今天的市场"
    # contains both "分析" and "今天"; routing it as generic analysis lets the
    # model answer from memory instead of using market data.
    if has_realtime:
        return INTENT_REALTIME

    # Bug ② — "什么是X" + finance concept → general (explain, not look up)
    if any(q in low for q in _QUESTION_PREFIX) and any(k in low for k in _FINANCE_CONCEPT_KW):
        return INTENT_GENERAL

    # Stock/market analysis — but only if a financial entity is present.
    if any(k in low for k in _ANALYSIS_KW):
        # Bug ③ — macro topics check (also works independently below)
        if any(t in low for t in _MACRO_GENERAL_TOPICS):
            return INTENT_GENERAL
        if any(t in low for t in _NON_STOCK_ANALYSIS_TOPICS):
            return INTENT_GENERAL
        # Bug ① — "分析" without a financial entity = general task (project, doc, etc.)
        if not any(e in low for e in _FIN_ENTITY_KW):
            # Check for uppercase ticker pattern (e.g. AAPL, BTC)
            if not _re.search(r'\b[A-Z]{2,5}\b', message):
                return INTENT_GENERAL
        # Bug ④ — recommendation phrasing ("应该买入吗") → finance chat, not chart analysis
        _rec_phrases = ("应该", "是否值得", "要不要", "该不该", "值不值", "建议买", "建议卖")
        if any(p in low for p in _rec_phrases):
            return INTENT_FINANCE
        return INTENT_ANALYSIS

    # Bug ③ (standalone) — macro conceptual topics, unless a SPECIFIC entity is named.
    # Generic market terms like "股市"/"美股" don't block this — "宏观经济对美股的影响"
    # is still a macro discussion, not a specific stock query.
    if any(t in low for t in _MACRO_GENERAL_TOPICS):
        _has_specific = (
            any(e in low for e in _FIN_ENTITY_KW)
            or _re.search(r'\b[A-Z]{2,5}\b', message)
        )
        if not _has_specific:
            return INTENT_GENERAL

    # Finance concept terms → keep full finance context, not general
    if any(k in low for k in _FINANCE_CONCEPT_KW):
        return INTENT_FINANCE

    if any(k in low for k in _GENERAL_KW):
        return INTENT_GENERAL

    return INTENT_GENERAL


def _prelude_available(ollama_url: str) -> bool:
    """Quick sync check: is aria-prelude loaded in Ollama?"""
    try:
        with urllib.request.urlopen(
            ollama_url.rstrip("/") + "/api/tags", timeout=1
        ) as r:
            data = json.loads(r.read())
        models = [m["name"] for m in data.get("models", [])]
        return any(m.startswith(_PRELUDE_MODEL) for m in models)
    except Exception:
        return False


_MLX_MODE_ENV = "ARIA_PRELUDE_MLX"        # off (default) | shadow | on
_MLX_URL_ENV = "ARIA_PRELUDE_MLX_URL"     # an mlx_lm.server, e.g. http://127.0.0.1:8099
_MLX_TIMEOUT = 2.0
_LABELS = (INTENT_CODING, INTENT_ANALYSIS, INTENT_REALTIME, INTENT_GENERAL, INTENT_FINANCE)


def mlx_mode() -> str:
    """``off`` unless the experiment is switched on; ``shadow`` only compares, ``on`` routes."""
    import os

    value = os.environ.get(_MLX_MODE_ENV, "off").strip().lower()
    return value if value in {"shadow", "on"} else "off"


def parse_label(raw: str) -> Optional[str]:
    """The one routing label a reply names, or None when it names none or several."""
    words = set(re.findall(r"[a-z]+", (raw or "").lower()))
    found = [label for label in _LABELS if label in words]
    return found[0] if len(found) == 1 else None


async def _classify_mlx(message: str, *, timeout: float = _MLX_TIMEOUT) -> Optional[str]:
    """Ask a local MLX server running the fine-tuned Prelude; None on any problem."""
    import os

    try:
        import aiohttp
    except ImportError:
        return None
    base = os.environ.get(_MLX_URL_ENV, "http://127.0.0.1:8099").rstrip("/")
    payload = {
        "model": "default_model", "max_tokens": 6, "temperature": 0.0, "stream": False,
        "messages": [{"role": "system", "content": _PRELUDE_SYSTEM}, {"role": "user", "content": message}],
    }
    try:
        async with aiohttp.ClientSession(trust_env=False) as sess:
            async with sess.post(base + "/v1/chat/completions", json=payload,
                                 timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        return parse_label(data["choices"][0]["message"]["content"])
    except Exception:
        return None


async def classify_intent_async(
    message: str,
    ollama_url: str = "http://localhost:11434",
    *,
    timeout: float = _PRELUDE_TIMEOUT,
) -> str:
    """Route a message. The default path is unchanged; ``ARIA_PRELUDE_MLX`` opts into an experiment.

    ``shadow`` asks the fine-tuned model too, returns the default answer, and records only
    whether the two agreed (never the message). ``on`` returns the model's label when it
    gives one and otherwise falls back to the default path.
    """
    mode = mlx_mode()
    if mode == "off" or is_visual_market_artifact_request(message):
        return await _classify_default_async(message, ollama_url, timeout=timeout)
    candidate = await _classify_mlx(message)
    if mode == "on" and candidate:
        return candidate
    baseline = await _classify_default_async(message, ollama_url, timeout=timeout)
    if mode == "shadow":
        from aria_code.apps.cli.intent_shadow import record

        record(message, candidate, baseline)
    return baseline


async def _classify_default_async(
    message: str,
    ollama_url: str = "http://localhost:11434",
    *,
    timeout: float = _PRELUDE_TIMEOUT,
) -> str:
    """
    Tier-1 classification using aria-prelude via Ollama.
    Falls back to tier-2 keyword classification on any error or timeout.
    """
    try:
        import aiohttp
    except ImportError:
        return classify_intent_sync(message)

    if is_visual_market_artifact_request(message):
        return INTENT_CODING

    if not _prelude_available(ollama_url):
        return classify_intent_sync(message)

    payload = {
        "model": _PRELUDE_MODEL,
        "messages": [
            {"role": "system", "content": _PRELUDE_SYSTEM},
            {"role": "user",   "content": message},
        ],
        "stream": False,
        "options": {"num_predict": 8, "temperature": 0.0},
    }
    url = ollama_url.rstrip("/") + "/api/chat"
    try:
        async with aiohttp.ClientSession(trust_env=True) as sess:
            async with sess.post(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    return classify_intent_sync(message)
                data = await resp.json()
        raw = data.get("message", {}).get("content", "").strip().lower()
        # Accept only known labels
        for label in (INTENT_CODING, INTENT_ANALYSIS, INTENT_REALTIME,
                      INTENT_GENERAL, INTENT_FINANCE):
            if label in raw:
                if is_visual_market_artifact_request(message):
                    return INTENT_CODING
                # Post-override: even if the prelude model says "analysis" or "finance",
                # non-stock topics must not get the stock-analysis template.
                if label == INTENT_ANALYSIS:
                    low_msg = message.lower()
                    if any(t in low_msg for t in _MACRO_GENERAL_TOPICS):
                        return INTENT_GENERAL   # macro → educational, no tools
                    if any(t in low_msg for t in _NON_STOCK_ANALYSIS_TOPICS):
                        return INTENT_FINANCE   # real-estate etc. → finance chat
                if label == INTENT_FINANCE:
                    # Even a "finance" label from the model shouldn't invoke live tools
                    # for pure macro conceptual questions.
                    low_msg = message.lower()
                    if any(t in low_msg for t in _MACRO_GENERAL_TOPICS):
                        return INTENT_GENERAL
                return label
    except Exception:
        pass
    return classify_intent_sync(message)
