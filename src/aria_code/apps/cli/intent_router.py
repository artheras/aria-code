"""Canonical lightweight intent routing for CLI services.

This module keeps routing side-effect free.  It does not call tools, install
packages, or fetch data; it only classifies a user message into stable service
intents that other layers can reuse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable


@lru_cache(maxsize=None)
def _keyword_pattern(keyword: str) -> re.Pattern:
    """English keywords match whole words (plurals and -ing/-ed included).

    Plain substring matching made "ci" match deCImal and finanCIal, "eth"
    match mETHod and togETHer, "rsi" match peRSIst and "word" match passWORD,
    so a request to write Decimal code was routed as a GitHub task and held
    up by a demand to install the gh CLI. Chinese keywords keep substring
    matching: Chinese has no spaces to bound a word.
    """
    word = keyword.strip()
    left = r"(?<![a-z0-9])" if word[:1].isascii() and word[:1].isalnum() else ""
    right = r"(?:s|es|ing|ed)?(?![a-z0-9])" if word[-1:].isascii() and word[-1:].isalnum() else ""
    return re.compile(left + re.escape(word) + right)


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    for keyword in keywords:
        if keyword.strip() and keyword.strip().isascii():
            if _keyword_pattern(keyword).search(text):
                return True
        elif keyword in text:
            return True
    return False


def _first_token(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped.split(maxsplit=1)[0].lower()


def _add_unique(items: list[str], name: str) -> None:
    if name not in items:
        items.append(name)


@dataclass(frozen=True)
class IntentRoute:
    message: str
    primary: str
    intents: tuple[str, ...]
    services: tuple[str, ...]
    explicit_code: bool = False
    visual_artifact: bool = False
    market_related: bool = False

    @property
    def wants_market_prefetch(self) -> bool:
        return self.market_related and not self.visual_artifact and self.primary != "general"

    @property
    def allows_code_autorun(self) -> bool:
        return self.primary in {"code", "strategy", "backtest"} and self.explicit_code


COMMAND_INTENTS = {
    "/chart": "chart",
    "/dashboard": "dashboard",
    "/report": "report",
    "/team": "market_research",
    "/analyze": "market_analysis",
    "/ta": "market_analysis",
    "/quote": "market_snapshot",
    "/backtest": "backtest",
    "/auto-strategy": "strategy",
    "/strategy": "strategy",
    "/ui": "ui_artifact",
    "/vision": "vision",
    "/upload-image": "vision",
    "/screenshot": "screenshot",
    "/browser": "browser",
    "/file": "file_analysis",
    "/github": "github",
    "/mcp": "mcp",
    "/cloud": "cloud",
}


def detect_intents(message: str) -> tuple[str, ...]:
    from aria_code.apps.cli.conversation_policy import is_capability_question
    if is_capability_question(message):
        return ()
    low = message.lower().strip()
    cmd = _first_token(low)
    intents: list[str] = []

    mapped = COMMAND_INTENTS.get(cmd)
    if mapped:
        _add_unique(intents, mapped)
        # Arguments to /file are usually filenames.  Words such as “报告” or
        # “分析” inside those paths describe the document, not a second market
        # command, and must not pull in report/chart dependencies.
        if cmd == "/file":
            return tuple(intents)

    if _contains_any(low, ("k线图", "k线", "k-line", "kline", "candlestick", "走势图", "图表", "chart", "plot")):
        _add_unique(intents, "chart")
    if _contains_any(low, ("看板", "晨报", "日报", "dashboard", "market board", "持仓看板")):
        _add_unique(intents, "dashboard")
    if _contains_any(low, ("研究报告", "财报", "报告", "report", "研报")):
        _add_unique(intents, "report")
    if _contains_any(low, ("回测", "backtest", "收益曲线", "最大回撤")):
        _add_unique(intents, "backtest")
    if _contains_any(low, ("策略代码", "写策略", "量化策略", "交易策略", "strategy code", "trading bot")):
        _add_unique(intents, "strategy")
    if _contains_any(low, ("技术分析", "技术指标", "rsi", "macd", "均线", "支撑", "阻力")):
        _add_unique(intents, "market_analysis")
    if _contains_any(low, ("股票", "行情", "持仓", "portfolio", "quote", "market data")):
        _add_unique(intents, "market_snapshot")
    if _contains_any(low, ("图片", "图像", "截图", "上传图片", "分析图片", "识别图片", "image", "screenshot")):
        _add_unique(intents, "vision")
    if _contains_any(low, ("浏览器", "网页截图", "打开网页", "browser", "playwright")):
        _add_unique(intents, "browser")
    if _contains_any(low, ("pdf", "docx", "word", "excel", "xlsx", "csv", "文件分析", "上传文件")):
        _add_unique(intents, "file_analysis")
    # A bare "issue" is ordinary English ("any issue with this?"); only a
    # numbered one ("issue #12") points at GitHub.
    if _contains_any(low, ("github", "pull request", "pr", "ci", "gh")) or re.search(r"\bissues?\s*#\d+", low):
        _add_unique(intents, "github")
    if _contains_any(low, ("mcp", "server", "tools", "skills")):
        _add_unique(intents, "mcp")
    if _contains_any(low, ("ollama", "本地模型", "local model")):
        _add_unique(intents, "local_model")
    if _contains_any(low, ("阿里云", "aliyun", "cloud service", "云端服务")):
        _add_unique(intents, "cloud")
    if _contains_any(low, ("搜索", "联网", "上网查", "web search", "google", "查一下", "搜一下", "最新消息")):
        _add_unique(intents, "web_search")
    if _contains_any(low, ("加密货币", "比特币", "以太坊", "bitcoin", "btc", "eth", "crypto", "币安", "binance", "okx", "资金费率", "funding rate")):
        _add_unique(intents, "crypto")
    if _contains_any(low, ("外汇", "汇率", "美元兑", "欧元兑", "eur/", "usd/", "jpy", "forex", "fx ")):
        _add_unique(intents, "forex")
    if _contains_any(low, ("期货", "商品", "黄金", "白银", "原油", "铜价", "天然气", "commodity", "futures")):
        _add_unique(intents, "commodity")
    if _contains_any(low, ("a股", "沪深", "上证", "深证", "创业板", "科创板")):
        _add_unique(intents, "ashare")
    if _contains_any(low, ("港股", "恒生", "h股", ".hk")):
        _add_unique(intents, "hk_market")
    if _contains_any(low, ("美股", "纳斯达克", "纽交所", "标普500", "道琼斯", "us stock")):
        _add_unique(intents, "us_market")
    if _contains_any(low, ("足球", "球赛", "比分预测", "世界杯", "欧洲杯", "英超", "西甲", "football", "soccer", "world cup", "premier league")):
        _add_unique(intents, "sports")

    if "file_analysis" in intents and "report" in intents and cmd != "/report":
        intents = [intent for intent in intents if intent != "report"]

    return tuple(intents)


def _service_names(intents: tuple[str, ...]) -> tuple[str, ...]:
    services: list[str] = []

    def service(name: str) -> None:
        _add_unique(services, name)

    if any(i in intents for i in ("market_snapshot", "market_analysis", "chart", "dashboard", "report", "backtest", "strategy", "market_research", "ashare", "hk_market", "us_market", "forex", "commodity")):
        service("market_data")
    if "chart" in intents:
        service("chart_renderer")
    if "dashboard" in intents:
        service("dashboard_generator")
    if "report" in intents:
        service("report_generator")
    if "backtest" in intents or "strategy" in intents:
        service("backtest_engine")
    if "vision" in intents:
        service("vision_input")
    if "screenshot" in intents:
        service("screenshot")
    if "browser" in intents:
        service("browser")
    if "file_analysis" in intents:
        service("file_parser")
    if "github" in intents:
        service("github_cli")
    if "mcp" in intents:
        service("mcp")
    if "local_model" in intents:
        service("local_llm")
    if "cloud" in intents:
        service("cloud_runtime")
    if "web_search" in intents:
        service("web_search")
    if "crypto" in intents:
        service("crypto_data")
    if "forex" in intents:
        service("forex_data")
    if "commodity" in intents:
        service("commodity_data")
    if "ashare" in intents:
        service("ashare_data")
    if "hk_market" in intents:
        service("hk_market_data")
    if "us_market" in intents:
        service("us_market_data")
    if "sports" in intents:
        service("sports_data")
    return tuple(services)


def build_intent_route(message: str) -> IntentRoute:
    low = message.lower().strip()
    intents = detect_intents(message)
    conceptual_market_question = (
        any(topic in low for topic in ("新闻面", "技术面", "基本面"))
        and any(marker in low for marker in (
            "有用吗", "是否有用", "区别", "关系", "怎么结合", "如何结合",
            "你觉得", "为什么",
        ))
        and not any(marker in low for marker in (
            "最新", "今天", "当前", "实时", "查一下", "搜一下", "搜索",
            "/news", "/quote", "/analyze",
        ))
    )
    explicit_code = _contains_any(low, (
        "代码", "脚本", "python", "程序", "实现", "开发", "修改文件",
        "写代码", "编写代码", "策略代码", "保存为.py", ".py",
        "script", "code", "program", "implement", "edit file", "write file",
    ))
    try:
        try:
            from aria_code.intent_classifier import (
                INTENT_ANALYSIS,
                INTENT_CODING,
                INTENT_FINANCE,
                INTENT_GENERAL,
                INTENT_REALTIME,
                classify_intent_sync,
                is_visual_market_artifact_request,
            )
        except ImportError:
            from intent_classifier import (  # type: ignore[no-redef]
                INTENT_ANALYSIS,
                INTENT_CODING,
                INTENT_FINANCE,
                INTENT_GENERAL,
                INTENT_REALTIME,
                classify_intent_sync,
                is_visual_market_artifact_request,
            )
        classifier_intent = classify_intent_sync(message)
        visual_artifact = bool(is_visual_market_artifact_request(message))
    except Exception:
        INTENT_ANALYSIS = "analysis"
        INTENT_CODING = "coding"
        INTENT_FINANCE = "finance"
        INTENT_GENERAL = "general"
        INTENT_REALTIME = "realtime"
        classifier_intent = INTENT_FINANCE
        visual_artifact = any(i in intents for i in ("chart", "dashboard", "report", "ui_artifact"))

    if conceptual_market_question:
        # Methodology/capability questions need an explanation, not live market
        # calls.  Treating the word "新闻面" as a news lookup caused the whole
        # sentence to be forwarded to search providers as the query.
        primary = "general"
    elif intents:
        primary = intents[0]
    elif classifier_intent == INTENT_REALTIME:
        primary = "market_snapshot"
    elif classifier_intent == INTENT_ANALYSIS:
        primary = "market_analysis"
    elif classifier_intent == INTENT_CODING:
        primary = "code"
    elif classifier_intent == INTENT_GENERAL:
        primary = "general"
    elif classifier_intent == INTENT_FINANCE:
        primary = "finance"
    else:
        primary = "finance"

    market_related = not conceptual_market_question and (
        any(i in intents for i in (
            "market_snapshot", "market_analysis", "chart", "dashboard", "report",
            "backtest", "strategy", "market_research", "ashare", "hk_market",
            "us_market", "crypto", "forex", "commodity",
        ))
        or classifier_intent in {INTENT_ANALYSIS, INTENT_REALTIME, INTENT_FINANCE}
    )

    return IntentRoute(
        message=message,
        primary=primary,
        intents=intents,
        services=_service_names(intents),
        explicit_code=explicit_code,
        visual_artifact=visual_artifact,
        market_related=market_related,
    )
