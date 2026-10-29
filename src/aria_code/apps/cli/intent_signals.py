"""Signal extraction and intent precedence for message classification.

Replaces a keyword-ordering scheme that had accumulated six layers of
counter-patches (labelled "Bug ①"–"Bug ⑥" in intent_classifier.py) with no
regression tests behind any of them.  That design failed in four recurring
ways, all of which this module addresses directly:

  - **Substring collisions.**  The coding keyword "repo" matched inside
    "report_2024.docx", so asking about a document was classified as a code
    task.  ASCII terms are now matched on word boundaries.
  - **Spacing variants.**  The artifact keyword "k线" missed "K 线图", so a
    chart request fell through to general.  CJK terms are now matched against
    a whitespace-stripped form of the message.
  - **Rules cancelling each other.**  A question word suppressed the coding
    rule, so "这段代码为什么报错" — a debugging request — became a general
    question.  Signals are now extracted independently and combined by one
    explicit precedence table, rather than by the order of early returns.
  - **Generic words as intent.**  Bare "分析"/"研究"/"评估" are ordinary
    Chinese and were treated as market-analysis intent, held back only by an
    entity allowlist.  A market intent now *requires* a concrete entity.

The precedence table is the whole decision: read it top to bottom to know how
any message is classified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

INTENT_CODING = "coding"
INTENT_ANALYSIS = "analysis"
INTENT_REALTIME = "realtime"
INTENT_GENERAL = "general"
INTENT_FINANCE = "finance"

_CJK = r"一-鿿"
_ASCII_TERM_RE = re.compile(r"^[a-z0-9][a-z0-9 .+#/_-]*$")


def _normalize(text: str) -> tuple[str, str]:
    """Return (spaced, compact) lowercase forms of *text*.

    ``spaced`` collapses whitespace runs and is used for boundary-anchored
    ASCII matching.  ``compact`` removes whitespace entirely and is used for
    CJK terms, so "K 线图" and "K线图" are the same message.
    """
    low = (text or "").strip().lower()
    spaced = re.sub(r"\s+", " ", low)
    compact = re.sub(r"\s+", "", low)
    return spaced, compact


def _matches(term: str, spaced: str, compact: str) -> bool:
    """True if *term* occurs in the message under the right matching rule."""
    term = term.strip().lower()
    if not term:
        return False
    if _ASCII_TERM_RE.match(term):
        # Word boundaries stop "repo" from matching inside "report".
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", spaced) is not None
    return re.sub(r"\s+", "", term) in compact


def _any(terms, spaced: str, compact: str) -> bool:
    return any(_matches(t, spaced, compact) for t in terms)


# ── Term inventories ────────────────────────────────────────────────────────
# Each list answers one question.  Terms live in exactly one list, so a change
# has one obvious place to go.

_CODE_TERMS = (
    # Actions on code
    "写代码", "编写代码", "重构", "调试", "报错", "修复", "改一下", "改成",
    "实现", "开发", "部署", "上线", "打包", "编译", "跑一下", "运行一下",
    "单元测试", "集成测试", "代码审查", "代码审核", "审核代码", "审查代码",
    "refactor", "debug", "implement", "deploy", "compile", "lint",
    "unit test", "integration test", "code review", "stack trace", "traceback",
    "bug", "fix", "patch", "commit", "merge", "rebase", "pull request",
    # Code nouns
    "代码", "脚本", "函数", "方法", "类", "模块", "接口", "仓库", "代码库",
    "前端", "后端", "数据库", "编译器", "依赖", "报错信息", "异常",
    "script", "function", "module", "repo", "repository", "codebase",
    "frontend", "backend", "database", "api", "sdk", "cli",
    "python", "typescript", "javascript", "rust", "golang", "java",
    "react", "vue", "next.js", "nextjs", "svelte", "swiftui", "flutter",
    "fastapi", "django", "flask", "express", "pytest", "docker", "kubernetes",
    # Build requests.  "写一个 AAPL 动量策略" is a request to produce code, even
    # though the subject is a ticker — without these it fell through to the
    # market branches and was answered as prose.
    "写一个", "写个", "做一个", "搭一个", "建一个", "编写", "创建", "生成",
    "策略", "交易策略", "量化策略", "回测", "选股",
    "write a", "create a", "build a", "generate", "backtest", "strategy",
    # How people actually report a defect.  The list above named tools and
    # actions ("重构", "pytest", "traceback") but not the plain sentence a
    # person writes when something is broken, so "这个项目的测试挂了，找出原因
    # 并修好" classified as GENERAL — and the general prompt does not tell the
    # model to act, so a 7B model with every tool available simply asked for
    # more details and changed nothing.  An eval suite run caught this at 0/5.
    #
    # The bias here is deliberate: an unsure classification should fail toward
    # capability. Sending "测试一下这个想法" down the coding path costs a longer
    # system prompt; sending "测试挂了，修好" to general costs the entire task,
    # silently.
    "测试挂", "测试失败", "测试不过", "测试没过", "跑不通", "跑不过",
    "修好", "修一下", "修掉", "改好", "没通过", "挂了",
    "断言", "崩溃", "空指针", "越界", "校验", "入参", "边界条件", "打日志",
    "failing test", "test fails", "tests fail", "test failure", "broken",
    "crash", "assertion", "exception", "stacktrace", "regression",
    "edge case", "off-by-one", "validation", "refactoring",
)

# Words that name a project/product artefact rather than code.  When these
# appear without a code term, the task is documentation or product work.
_PRODUCT_TERMS = (
    "产品", "需求", "文档", "架构设计", "商业模式", "市场定位", "用户体验",
    "运营", "品牌", "文案", "白皮书", "商业计划",
)

_ARTIFACT_TERMS = (
    "图表", "走势图", "k线图", "k线", "k-line", "kline", "candlestick",
    "蜡烛图", "热力图", "heatmap", "chart", "plot", "dashboard",
    "看板", "晨报", "日报", "周报", "月报", "报表",
)

_MARKET_CONTEXT_TERMS = (
    "股票", "股价", "行情", "市场", "美股", "港股", "a股", "指数", "大盘",
    "持仓", "portfolio", "回测", "财报", "earnings", "基金", "etf",
    "资产", "组合", "市值", "backtest", "market data", "stock",
)

_REALTIME_TERMS = (
    "今天", "今日", "现在", "当前", "此刻", "最新", "最近", "实时",
    "多少钱", "多少点", "是多少", "什么价", "现价", "报价", "涨跌", "涨幅",
    "新闻", "消息", "快讯", "今天的", "盘中",
    "today", "now", "current", "latest", "price", "quote", "news",
    "market cap", "how much",
)

_ANALYSIS_TERMS = (
    "分析", "研究", "评估", "研判", "看法", "怎么样", "如何",
    "技术面", "基本面", "走势", "趋势", "估值",
    "analysis", "analyze", "outlook", "valuation", "technical analysis",
    "fundamental",
)

_CONCEPTUAL_TERMS = (
    "什么是", "什么叫", "是什么", "为什么", "怎么理解", "如何理解",
    "解释", "定义", "概念", "原理", "介绍一下", "区别", "举例",
    "what is", "what are", "how does", "explain", "define",
    "difference between", "tell me about",
)

_MACRO_TERMS = (
    "宏观", "宏观经济", "宏观政策", "宏观角度", "货币政策", "财政政策",
    "gdp", "通胀", "通货膨胀", "cpi", "ppi", "利率", "加息", "降息",
    "美联储", "央行", "收益率曲线", "国债", "产业政策", "行业监管",
)

_REALTY_TERMS = (
    "房价", "楼市", "房产", "房地产", "租金", "二手房", "商铺", "户型",
)

_RECOMMENDATION_TERMS = (
    "应该买", "应该卖", "该不该", "要不要", "值不值", "是否值得", "值得投资",
    "建议买", "建议卖", "能买吗", "可以买吗",
)

_FINANCE_CONCEPT_TERMS = (
    "dcf", "wacc", "capm", "sharpe", "sortino", "ebitda", "beta", "alpha",
    "var", "cvar", "drawdown", "black-scholes", "期权", "期货", "衍生品",
    "套利", "量化", "quant", "因子", "夏普比率", "最大回撤",
    "市盈率", "市净率", "估值模型", "pe", "pb", "roe", "roa",
)

_METRIC_TERMS = (
    "pe", "pb", "ps", "市盈率", "市净率", "市销率", "eps", "净利润",
    "营收", "市值", "股息", "分红", "毛利率", "roe", "roa",
)

_FILE_EXT_RE = re.compile(
    r"[^\s]+\.(?:docx?|pdf|xlsx?|pptx?|txt|csv|json|ya?ml|md|log|ipynb)\b"
)
_CODE_EXT_RE = re.compile(r"[^\s]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|c|cpp|h|sh)\b")

# Concrete, named financial entities.  A market intent requires one of these
# (or a resolvable ticker) — generic category words like "美股" deliberately do
# not qualify, because they appear throughout macro discussion.
_ENTITY_TERMS = (
    "苹果", "谷歌", "英伟达", "微软", "特斯拉", "亚马逊", "腾讯", "阿里",
    "百度", "比亚迪", "茅台", "招商银行", "中国平安", "华为", "小米",
    "美团", "京东", "字节", "宁德时代", "中芯国际",
    "apple", "google", "nvidia", "microsoft", "tesla", "amazon", "meta",
    "netflix", "palantir", "snowflake", "mongodb",
    "比特币", "以太坊", "bitcoin", "ethereum",
    "纳斯达克", "标普500", "道琼斯", "沪深300", "中证500", "恒生指数",
)

_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,5}(?![A-Za-z0-9])")
_A_SHARE_RE = re.compile(r"(?<!\d)(?:60|68|00|30)\d{4}(?!\d)")


@dataclass(frozen=True)
class IntentSignals:
    """Independent observations about a message, with no precedence applied."""

    entity: bool
    ticker: bool
    market_context: bool
    code: bool
    product: bool
    file_ref: bool
    code_file_ref: bool
    artifact: bool
    realtime: bool
    analysis: bool
    conceptual: bool
    macro: bool
    realty: bool
    recommendation: bool
    finance_concept: bool
    metric: bool

    @property
    def any_entity(self) -> bool:
        return self.entity or self.ticker


def extract_signals(message: str) -> IntentSignals:
    """Observe *message* without deciding anything about it."""
    spaced, compact = _normalize(message)
    raw = message or ""
    return IntentSignals(
        entity=_any(_ENTITY_TERMS, spaced, compact),
        ticker=bool(_TICKER_RE.search(raw) or _A_SHARE_RE.search(raw)),
        market_context=_any(_MARKET_CONTEXT_TERMS, spaced, compact),
        code=_any(_CODE_TERMS, spaced, compact),
        product=_any(_PRODUCT_TERMS, spaced, compact),
        file_ref=bool(_FILE_EXT_RE.search(raw)),
        code_file_ref=bool(_CODE_EXT_RE.search(raw)),
        artifact=_any(_ARTIFACT_TERMS, spaced, compact),
        realtime=_any(_REALTIME_TERMS, spaced, compact),
        analysis=_any(_ANALYSIS_TERMS, spaced, compact),
        conceptual=_any(_CONCEPTUAL_TERMS, spaced, compact),
        macro=_any(_MACRO_TERMS, spaced, compact),
        realty=_any(_REALTY_TERMS, spaced, compact),
        recommendation=_any(_RECOMMENDATION_TERMS, spaced, compact),
        finance_concept=_any(_FINANCE_CONCEPT_TERMS, spaced, compact),
        metric=_any(_METRIC_TERMS, spaced, compact),
    )


def is_visual_artifact_request(message: str) -> bool:
    """True for chart/dashboard/report requests that should be built, not looked up."""
    spaced, _ = _normalize(message)
    if spaced.startswith(("/chart", "/dashboard", "/report")):
        return True
    signals = extract_signals(message)
    if not signals.artifact:
        return False
    return signals.market_context or signals.any_entity or signals.finance_concept


def classify(message: str) -> str:
    """Classify *message* into one INTENT_* label.

    The precedence below is the entire decision. Each rule states why it
    outranks the ones after it.
    """
    if not (message or "").strip():
        return INTENT_GENERAL
    from aria_code.apps.cli.conversation_policy import is_capability_question
    if is_capability_question(message):
        return INTENT_GENERAL

    s = extract_signals(message)

    # 1. A chart/dashboard/report over market data is something to *build*.
    #    It needs the code path, not a quote lookup.
    if is_visual_artifact_request(message):
        return INTENT_CODING

    # 2. A source file named outright is a code task regardless of phrasing.
    if s.code_file_ref:
        return INTENT_CODING

    # 3. A document named outright is a reading task, not a market query —
    #    even though "report_2024.docx" contains market-ish letters.
    if s.file_ref:
        return INTENT_CODING if s.code else INTENT_GENERAL

    # 4. Code signals win over question phrasing.  "这段代码为什么报错" is a
    #    debugging request; the old order let "为什么" demote it to general.
    if s.code:
        return INTENT_CODING

    # 5. Real estate never routes to the stock templates, which expect market
    #    data that does not exist for a housing question.
    if s.realty:
        return INTENT_GENERAL

    # 6. Macro topics are discussion unless a specific company is named.
    if s.macro and not s.any_entity:
        return INTENT_GENERAL

    # 7. "What is X" wants an explanation, not a lookup — but only when it is
    #    not asking about a specific company's live numbers.
    if s.conceptual and not (s.any_entity and (s.realtime or s.metric)):
        return INTENT_GENERAL

    # 8. "Should I buy X" is advice, not a chart.
    if s.recommendation:
        return INTENT_FINANCE if (s.any_entity or s.market_context) else INTENT_GENERAL

    # 9. Live data wins over analysis: "分析苹果今天的市场" needs a fetch, or
    #    the model answers a price question from memory.
    if s.realtime and (s.any_entity or s.market_context or s.metric):
        return INTENT_REALTIME

    # 10. Market analysis requires a concrete entity.  Bare "分析"/"研究" is
    #     ordinary Chinese and must not reach the stock-analysis template.
    if s.analysis and s.any_entity:
        return INTENT_ANALYSIS

    # 11. Finance vocabulary without an entity is finance chat.
    if s.finance_concept or (s.market_context and s.analysis):
        return INTENT_FINANCE

    # 12. A bare live-data question with no market subject ("最近有什么新闻").
    if s.realtime:
        return INTENT_REALTIME

    return INTENT_GENERAL


__all__ = [
    "INTENT_ANALYSIS",
    "INTENT_CODING",
    "INTENT_FINANCE",
    "INTENT_GENERAL",
    "INTENT_REALTIME",
    "IntentSignals",
    "classify",
    "extract_signals",
    "is_visual_artifact_request",
]
