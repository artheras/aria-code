# Auto-extracted from aria_cli.py — shared market-detection constants & helpers.
# Multiple handler modules import from here to avoid circular dependencies.

import re as _re_sym

try:
    from aria_code.apps.cli.market_universe import (
        looks_like_unresolved_market_name as _looks_like_unresolved_market_name,
        resolve_market_mentions as _resolve_market_mentions,
        resolve_market_symbol as _resolve_market_symbol,
    )
except Exception:
    def _resolve_market_symbol(_message: str) -> str:
        return ""
    def _resolve_market_mentions(_message: str, limit: int = 6):
        return []
    def _looks_like_unresolved_market_name(_message: str) -> bool:
        return False

# Regex to find stock symbols in a message: e.g. "AAPL", "苹果AAPL", "TSLA股票", "BTC"
_STOCK_PATTERN = _re_sym.compile(
    r'\b([A-Z]{1,5}(?:\.(?:HK|SH|SZ))?)(?:\s*(?:股票|股价|价格|现在|今天|行情|涨跌))?\b'
    r'|(?:(?:苹果|特斯拉|英伟达|谷歌|亚马逊|微软|腾讯|阿里|百度|字节)).*?(\b[A-Z]{2,5}\b)',
    _re_sym.UNICODE
)
_CRYPTO_WORDS = {"比特币":"BTC","以太坊":"ETH","狗狗币":"DOGE","索拉纳":"SOL","BTC":"BTC","ETH":"ETH"}

_COMPANY_TO_TICKER = {
    # Minimal fallback aliases; full symbol universe lives in financial-service MCP.
    "四环生物": "000518",
    "贵州茅台": "600519", "茅台": "600519",
    "宁德时代": "300750", "比亚迪": "002594",
    "工商银行": "601398", "招商银行": "600036", "中国平安": "601318",
    "腾讯": "0700.HK", "腾讯控股": "0700.HK", "阿里巴巴": "BABA", "阿里": "BABA",
    "美团": "3690.HK", "小米": "1810.HK", "百度": "BIDU", "京东": "JD", "拼多多": "PDD",
    "苹果": "AAPL", "Apple": "AAPL",
    "微软": "MSFT", "Microsoft": "MSFT",
    "英伟达": "NVDA", "Nvidia": "NVDA", "NVIDIA": "NVDA",
    "特斯拉": "TSLA", "Tesla": "TSLA",
    "谷歌": "GOOGL", "Google": "GOOGL",
    "亚马逊": "AMZN", "Amazon": "AMZN",
    "Meta": "META", "台积电": "TSM", "TSMC": "TSM",
    "AMD": "AMD", "英特尔": "INTC", "Intel": "INTC",
    "Netflix": "NFLX", "奈飞": "NFLX",
    "比特币": "BTC-USD", "Bitcoin": "BTC-USD",
    "以太坊": "ETH-USD", "Ethereum": "ETH-USD",
    "黄金": "GC=F", "Gold": "GC=F", "原油": "CL=F", "WTI原油": "CL=F",
    "标普500": "^GSPC", "纳斯达克": "^IXIC", "道琼斯": "^DJI",
    "恒生指数": "^HSI", "上证指数": "000001.SS",
}

# Keywords that indicate the user is asking about their own portfolio/account
_BROKER_INTENT_KW = (
    "我的持仓", "我的仓位", "我持有", "我买了", "我的股票", "我的账户",
    "账户余额", "可用余额", "我有多少钱", "我的资金", "我还有多少",
    "持仓盈亏", "我的盈亏", "赚了多少", "亏了多少", "总盈亏", "当日盈亏",
    "委托", "我的订单", "挂单", "我下的单",
    "my portfolio", "my positions", "my holdings", "my account",
    "my balance", "my orders", "how much cash", "what do i own",
)


def _is_broker_intent(message: str) -> bool:
    """Return True if the message is asking about the user's own account/positions."""
    low = message.lower()
    return any(kw.lower() in low for kw in _BROKER_INTENT_KW)


# ── Broker setup / configuration intent ──────────────────────────────────────
# Maps broker type keys to their display names and recognition keywords.
_BROKER_SETUP_NAMES: dict[str, tuple[str, tuple]] = {
    "tiger":       ("老虎证券",   ("老虎", "tiger", "tigeropen", "tigerop")),
    "futu":        ("富途牛牛",   ("富途", "futu", "futuooo", "牛牛", "futuo")),
    "longbridge":  ("长桥证券",   ("长桥", "longbridge", "longport")),
    "ibkr":        ("盈透证券",   ("盈透", "ibkr", "ib ", "interactive broker", "ib_insync")),
    "alpaca":      ("Alpaca",     ("alpaca",)),
    "webull":      ("Webull",     ("webull", "微牛")),
    "xtquant":     ("迅投XTQuant", ("迅投", "xtquant", "qmt", "中信建投", "华鑫", "浙商")),
    "easytrader":  ("EasyTrader", ("easytrader", "通达信", "同花顺", "ths", "tdx", "华泰", "国君")),
}

_BROKER_SETUP_VERBS = (
    "配置", "连接", "接入", "安装", "设置", "开通", "使用", "启用", "绑定", "接口",
    "添加券商", "添加交易", "怎么用", "如何用", "怎样用", "怎么接", "如何接",
    "帮我", "帮忙", "教我", "指导", "setup", "connect", "configure", "install",
    "add broker", "use broker", "how to use", "how to connect", "how to setup",
    "guide", "tutorial", "凭证", "api key", "申请", "开发者",
)

_BROKER_GUIDE_INTENT_KW = (
    "支持哪些券商", "有哪些券商", "券商列表", "券商能力", "能力矩阵",
    "所有券商", "各个券商", "券商服务", "券商联动", "怎么连接各个券商",
    "如何连接各个券商", "如何和各个券商连接", "怎么和各个券商连接",
    "分析如何和各个券商连接", "使用这个项目的各个服务", "项目的各个服务",
    "broker guide", "broker matrix", "supported brokers", "broker capabilities",
    "broker services",
)


def _is_broker_guide_intent(message: str) -> bool:
    """Return True for broad broker/service discovery requests.

    These should show a capability guide instead of jumping straight into an
    interactive broker-add wizard.
    """
    low = message.lower()
    compact = low.replace(" ", "")
    if any(kw in low or kw.replace(" ", "") in compact for kw in _BROKER_GUIDE_INTENT_KW):
        return True
    has_broker_word = any(k in low for k in ("券商", "证券", "broker"))
    wants_overview = any(k in low for k in ("哪些", "列表", "支持", "能力", "矩阵", "各个", "所有", "服务"))
    asks_connection = any(k in low for k in ("怎么", "如何", "怎样", "连接", "接入", "使用"))
    return bool(has_broker_word and wants_overview and asks_connection)


def _is_broker_setup_intent(message: str) -> bool:
    """Return True if the user is asking to set up / configure a broker."""
    low = message.lower()
    has_verb = any(v in low for v in _BROKER_SETUP_VERBS)
    if not has_verb:
        return False
    # Check if any broker name/keyword is mentioned
    for _broker_type, (display, kws) in _BROKER_SETUP_NAMES.items():
        if any(k in low for k in kws):
            return True
    # Generic broker-setup phrases without a specific broker named
    generic_kws = ("券商", "证券", "broker", "交易所", "量化交易", "开户")
    return any(k in low for k in generic_kws) or bool(_re_sym.search(r"\bquant\b", low))


def _detect_broker_type(message: str) -> str:
    """Return the broker type key if a specific broker is mentioned, else ''."""
    low = message.lower()
    for broker_type, (_display, kws) in _BROKER_SETUP_NAMES.items():
        if any(k in low for k in kws):
            return broker_type
    return ""


_FINANCIAL_TERMS_BLOCKLIST: frozenset = frozenset({
    "DCF", "EPS", "ROE", "ROA", "ROI", "PEG", "NAV", "AUM", "NMV", "IRR",
    "NPV", "FCF", "OCF", "ICF", "EBIT", "WACC", "CAGR", "YTD", "QoQ",
    "YoY", "MoM", "GDP", "CPI", "PPI", "PMI", "NFP", "ISM", "PCE",
    "FED", "ECB", "PBC", "IMF", "BIS", "WTO", "SEC", "FSB",
    "QE", "QT", "VIX", "RSI", "MACD", "EMA", "SMA", "ATR", "ADX",
    "ATH", "ATL", "IPO", "SPO", "M&A", "LBO", "MBO",
    "NYSE", "NASDAQ", "AMEX", "LSE", "TSX", "ASX", "SGX", "HKE",
    "ETF", "CEF", "REIT", "MLP", "ADR", "GDR",
    "USD", "CNY", "EUR", "GBP", "JPY", "HKD", "SGD", "AUD",
    "BTC", "ETH", "XRP",  # crypto are handled by _CRYPTO_WORDS
    "AI", "AR", "VR", "ML", "NLP", "API", "SaaS", "B2B", "B2C",
})
_FINANCIAL_TERMS_SYMBOL_BLOCKLIST: frozenset = frozenset(
    str(item).upper() for item in _FINANCIAL_TERMS_BLOCKLIST
)

_MARKET_SYMBOL_WORD_BLOCKLIST: frozenset = frozenset({
    "ANALYZE", "ANALYSIS", "DATA", "QUOTE", "QUOTES", "PRICE", "PRICES",
    "MARKET", "MARKETS", "VOLUME", "VOLUMES", "CHART", "CHARTS", "PLOT",
    "YAHOO", "STOOQ", "YFINANCE", "AKSHARE", "FINNHUB", "SOURCE",
    "SOURCES", "PROVIDER", "PROVIDERS", "HISTORY", "HISTORICAL",
    # Trading action words from alert payloads (TradingView etc.) — the
    # channels e2e drill showed BUY being quoted as a ticker. MA is left
    # alone: it is Mastercard's real symbol despite doubling as a TA term.
    "BUY", "SELL", "EXIT", "HOLD", "FLAT", "LONG", "SHORT", "STOP", "ALERT",
    "CROSS", "SIGNAL", "ENTRY",
})


def _company_alias_symbol_blocklist() -> set[str]:
    """Words like APPLE/TESLA are company aliases, not Yahoo ticker symbols."""
    blocked: set[str] = set()
    for name, ticker in _COMPANY_TO_TICKER.items():
        alias = str(name or "").strip().upper()
        resolved = str(ticker or "").strip().upper()
        if (
            1 < len(alias) <= 5
            and alias.isalpha()
            and resolved
            and resolved not in ("未上市", "未独立")
            and not resolved.startswith("PRIVATE:")
            and alias != resolved
        ):
            blocked.add(alias)
    return blocked


_COMPANY_ALIAS_SYMBOL_BLOCKLIST: frozenset = frozenset(_company_alias_symbol_blocklist())


def _is_blocked_market_symbol_candidate(symbol: str) -> bool:
    """Return True for uppercase words that are context terms, not tickers."""
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return True
    return (
        normalized in _FINANCIAL_TERMS_SYMBOL_BLOCKLIST
        or normalized in _MARKET_SYMBOL_WORD_BLOCKLIST
        or normalized in _COMPANY_ALIAS_SYMBOL_BLOCKLIST
    )


# Numeric codes listed with an exchange suffix. The bare-ticker scan read
# "0700.HK" as the letters HK and "7203.T" as T — AT&T — so the quote shown
# was some other company's, though Aria's own hints say to type 0700.HK.
_NUMERIC_LISTING = _re_sym.compile(r'(?<![\w.])(\d{4,5})\.(HK|T|TW)(?![A-Za-z0-9])', _re_sym.I)


def _numeric_listings(message: str) -> list[tuple[int, int, str]]:
    """(start, end, symbol) of each numeric listing, in Yahoo's form (0700.HK)."""
    found = []
    for match in _NUMERIC_LISTING.finditer(message):
        code, suffix = match.group(1), match.group(2).upper()
        if suffix == "HK":
            code = f"{int(code):04d}"
        found.append((match.start(), match.end(), f"{code}.{suffix}"))
    return found


def _is_english_function_word(message: str, match) -> bool:
    """"I" the pronoun and "A" the article, read as one-letter tickers.

    "Should I sell my Tesla shares?" came back as a comparison of ``I`` and
    TSLA, and "I think AAPL is cheap" as a quote for ``I``. "A" is skipped only
    before a lowercase word ("A good time to buy MSFT?"), so "A股" is still
    the A-share market; F, T, C and the other one-letter tickers are untouched.
    """
    symbol = match.group(1)
    if symbol == "I":
        return True
    return symbol == "A" and _re_sym.match(r"A [a-z]", message[match.start(1):match.start(1) + 3]) is not None


def _first_bare_ticker(pattern: str, message: str) -> str:
    """The first capitalised word the pattern finds, unless it is a blocked term.

    English function words are passed over; a blocked term still ends the
    search, as it always has, so "Run the API tests for CI" stays no ticker.
    """
    for match in _re_sym.finditer(pattern, message):
        if _is_english_function_word(message, match):
            continue
        return "" if _is_blocked_market_symbol_candidate(match.group(1)) else match.group(1)
    return ""


def _extract_market_symbol(message: str) -> str:
    """Extract a likely market symbol from Chinese company names or tickers."""
    resolved = _resolve_market_symbol(message)
    if resolved:
        return resolved
    # 最长名称优先（避免"美的"抢先匹配"美的集团"）
    for cn, tick in sorted(_COMPANY_TO_TICKER.items(), key=lambda x: -len(x[0])):
        if cn in message:
            return tick
    listings = _numeric_listings(message)
    if listings:
        return listings[0][2]
    # A股裸6位代码（600519 / sh600519 / 688256.SH）
    m = _re_sym.search(r'(?<!\d)(?:[sS][hHzZ])?([036]\d{5}|68\d{4})(?:\.(?:SH|SZ|SS))?(?!\d)', message)
    if m:
        return m.group(1)
    # Yahoo-compatible index / futures / FX tickers: ^GSPC, GC=F, EURUSD=X
    m = _re_sym.search(r'(?<![A-Za-z0-9])(\^[A-Z0-9]{2,10}|[A-Z]{2,8}=F|[A-Z]{6}=X|DX-Y\.NYB)(?![A-Za-z0-9])', message, _re_sym.I)
    if m:
        return m.group(1).upper()
    symbol = _first_bare_ticker(r'\b([A-Z]{1,5}(?:\.(?:HK|SH|SZ))?)\b', message)
    if symbol:
        return symbol
    # Chinese text immediately after a ticker ("AAPL的市场") prevents \b from
    # matching because Unicode word-boundary rules treat 的 as a word char.
    return _first_bare_ticker(r'(?<![A-Za-z])([A-Z]{1,5}(?:\.(?:HK|SH|SZ))?)(?![A-Za-z])', message)

def _extract_market_symbols(message: str, limit: int = 6) -> list[str]:
    """Extract all likely market symbols, preserving mention order."""
    hits: list[tuple[int, str]] = []

    def add_hit(start: int, symbol: str) -> None:
        if start < 0 or not symbol:
            return
        # Higher-priority resolvers win at the same text position. This avoids
        # old aliases like "纳斯达克" -> QQQ duplicating the universe resolver's
        # "纳斯达克" -> ^IXIC result.
        if any(existing_start == start for existing_start, _ in hits):
            return
        hits.append((start, symbol))

    for start, item in _resolve_market_mentions(message, limit=limit):
        add_hit(start, item.symbol)

    for name, ticker in sorted(_COMPANY_TO_TICKER.items(), key=lambda x: -len(x[0])):
        if ticker.startswith("PRIVATE:") or ticker in ("未上市", "未独立"):
            continue
        start = message.find(name)
        add_hit(start, ticker)

    # Spans of coded listings: the letters in "0700.HK" or "600519.SS" are its
    # exchange, not a second ticker.
    coded_spans: list[tuple[int, int]] = []
    for match in _re_sym.finditer(r'(?<!\d)(?:[sS][hHzZ])?([036]\d{5}|68\d{4})(?:\.(?:SH|SZ|SS))?(?!\d)', message):
        add_hit(match.start(), match.group(1))
        coded_spans.append((match.start(), match.end()))

    for match in _re_sym.finditer(r'(?<![A-Za-z0-9])(\^[A-Z0-9]{2,10}|[A-Z]{2,8}=F|[A-Z]{6}=X|DX-Y\.NYB)(?![A-Za-z0-9])', message, _re_sym.I):
        add_hit(match.start(), match.group(1).upper())

    for start, end, symbol in _numeric_listings(message):
        add_hit(start, symbol)
        coded_spans.append((start, end))

    def inside_a_listing(match) -> bool:
        return any(start <= match.start() < end for start, end in coded_spans)

    for match in _re_sym.finditer(r'\b([A-Z]{1,5}(?:\.(?:HK|SH|SZ))?)\b', message):
        symbol = match.group(1)
        if (not _is_blocked_market_symbol_candidate(symbol) and not _is_english_function_word(message, match)
                and not inside_a_listing(match)):
            add_hit(match.start(), symbol)

    for match in _re_sym.finditer(r'(?<![A-Za-z])([A-Z]{1,5}(?:\.(?:HK|SH|SZ))?)(?![A-Za-z])', message):
        symbol = match.group(1)
        if (not _is_blocked_market_symbol_candidate(symbol) and not _is_english_function_word(message, match)
                and not inside_a_listing(match)):
            add_hit(match.start(), symbol)

    ordered: list[str] = []
    for _, symbol in sorted(hits, key=lambda item: item[0]):
        symbol = str(symbol).upper()
        if symbol not in ordered:
            ordered.append(symbol)
        if len(ordered) >= limit:
            break
    return ordered


def _extract_symbol_from_history(history: list, max_lookback: int = 4) -> str:
    """从最近的会话历史中提取标的（跟进问题继承上下文，如"现在的股票和趋势呢"）。

    只扫描 **user** 消息，倒序返回最先命中的标的代码。

    assistant 回复被刻意排除：模型回答里的普通英文词会被当成股票代码继承下来。
    实际发生过的例子——上一轮回答中提到 ``Cassandra/MongoDB`` 这样的技术选型，
    "MongoDB" 被解析成 ticker ``MDB``，用户接着说"开始完善"就被路由去查了
    MongoDB Inc. 的行情。标的意图必须来自用户本人，不能来自模型自己的输出。
    """
    if not history:
        return ""
    recent = history[-max_lookback:]
    for msg in reversed(recent):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):   # 多模态消息取 text 部分
            content = " ".join(p.get("text", "") for p in content
                               if isinstance(p, dict) and p.get("type") == "text")
        sym = _extract_market_symbol(str(content))
        if sym:
            return sym
    return ""

def _is_stock_chart_analysis_request(message: str) -> bool:
    """Return True for clear stock chart requests on stock-like symbols.

    This intentionally catches both:
    - "分析 AAPL 的 K 线图"
    - "生成 Apple 公司近一年的股票图表"
    so callers can route chart requests before generic market snapshot logic.
    """
    low = message.lower()
    has_chart = any(k in low for k in (
        "图表", "走势图", "k线", "k线图", "k-line", "kline", "chart", "plot",
        "蜡烛图", "candlestick", "生成", "绘制", "画", "制作", "生成一个", "生成近",
    ))
    has_stock = any(k in low for k in ("股票", "stock", "股价")) or bool(_extract_market_symbol(message))
    return has_chart and has_stock


def _is_visual_market_artifact_request(message: str) -> bool:
    """Return True for chart/dashboard/report requests tied to market data."""
    low = message.lower()
    has_visual = any(k in low for k in (
        "图表", "走势图", "k线", "k线图", "k-line", "kline", "candlestick",
        "chart", "plot", "dashboard", "看板", "晨报", "日报", "周报", "月报",
        "report", "热力图", "heatmap",
    ))
    if not has_visual:
        return False
    has_market = any(k in low for k in (
        "股票", "股价", "行情", "市场", "美股", "港股", "a股", "指数",
        "持仓", "portfolio", "回测", "财报", "earnings", "基金", "etf",
        "资产", "组合", "市场数据", "market data",
    ))
    return has_market or bool(_extract_market_symbol(message))


_UNRESOLVED_CO_INDICATORS = (
    # 中文公司后缀
    "公司", "集团", "股份", "有限", "控股", "科技", "电子", "能源", "银行",
    "汽车", "制药", "医疗", "传媒", "文化", "地产", "金融", "基金", "证券",
    # 外国品牌/公司名常见关键词（未进入字典的）
    "路易", "奔驰", "宝马", "大众", "爱马仕", "香奈儿", "耐克", "阿迪",
    "三星", "索尼", "丰田", "本田", "日产", "空客", "波音", "壳牌", "埃克森",
    "高盛", "摩根", "花旗", "巴克莱", "德银", "瑞信", "瑞银",
    "奈飞", "推特", "脸书", "优步", "滴滴", "字节", "快手", "拼多多",
)


def _has_unresolved_company_mention(message: str) -> bool:
    """Return True if message names a company/brand that _extract_market_symbol couldn't resolve.

    Used to block silent history inheritance: if user says "路易斯威登怎么看" and we
    can't resolve it, we should NOT fall back to the last queried ticker (e.g. 300124).
    """
    if _extract_market_symbol(message):
        return False  # already resolved — no problem
    return any(kw in message for kw in _UNRESOLVED_CO_INDICATORS) or _looks_like_unresolved_market_name(message)


# These keywords signal the user is asking about property markets, NOT stocks.
# Any message matching here must NOT be routed to the stock market snapshot path
# and must NOT silently inherit a stock ticker from session history.
_REALTY_QUERY_KEYWORDS = ()
_CN_CITIES = ()
_INTL_CITIES = ()
_STOCK_ONLY_MARKET_WORDS = ()


def _is_realty_query(message: str) -> bool:
    """Real-estate vertical moved to external service."""
    return False


def _detect_market_overview(message: str):
    """Detect a whole-market overview request → 'cn' | 'us' | 'hk' | None.

    Critical disambiguation: "分析A股市场行情" means the A-share MARKET (indices),
    NOT a stock with ticker 'A' (Agilent). This must run before any ticker
    extraction so a market name never collapses into a wrong single stock.
    Returns None for coding/chart/realty requests or specific-stock queries.
    """
    if _is_realty_query(message):
        return None
    low = message.lower().strip()
    _coding_kw = ("write", "generate", "script", "code", "plot", "backtest",
                  "策略", "代码", "回测", "编写", "k线", "kline", "python", "dashboard")
    if any(k in low for k in _coding_kw):
        return None
    if _is_stock_chart_analysis_request(message) or _is_visual_market_artifact_request(message):
        return None

    # Market-level intent words (overview, not a single stock)
    _overview_kw = (
        "市场", "大盘", "行情", "今天怎么样", "今日", "现在怎么样", "怎么样",
        "收盘", "开盘", "走势", "整体", "表现", "概况", "overview", "market",
    )
    if not any(k in low for k in _overview_kw):
        return None

    # Market identifiers (ordered: check before generic '大盘' defaults to CN)
    _us = ("美股", "美国股市", "美国市场", "华尔街", "纳斯达克", "纳指", "标普",
           "道琼斯", "道指", "us market", "wall street", "nasdaq", "s&p", "dow")
    _hk = ("港股", "恒生", "香港股市", "香港市场", "hk market", "hang seng", "hsi")
    _cn = ("a股", "a 股", "沪深", "上证", "深证", "创业板", "沪市", "深市",
           "科创板", "a-share", "ashare", "china market", "中国股市", "大盘")

    if any(k in low for k in _us):
        return "us"
    if any(k in low for k in _hk):
        return "hk"
    if any(k in low for k in _cn):
        return "cn"
    return None


def _is_market_snapshot_request(message: str, history: list = None, *,
                                evidence_required: bool = False) -> bool:
    """Return True for simple quote / market snapshot analysis requests.

    跟进问题（"现在的股票和趋势呢"）：当前消息无标的时回溯会话历史继承。
    但如果消息中包含未能识别的公司名，则不视为跟进问题（不继承历史标的）。
    房价/楼市类问题直接拒绝，防止继承上一个股票代码。

    ``evidence_required`` is the evidence gate's verdict on this message. When
    the gate will refuse an answer without data and the message names an
    instrument, it is a snapshot request whatever its wording: the word lists
    below were written for Chinese, so "How's NVDA this week?" matched none of
    them, nothing fetched data on a route without tools, and the gate refused.
    """
    # Real-estate questions must never be routed to the stock snapshot path
    if _is_realty_query(message):
        return False
    # Pre-built prompts from cmd_analyze contain specific footer text — bypass snapshot
    if ("Please provide a comprehensive analysis of" in message
            or "请对以上" in message and "进行综合分析" in message):
        return False
    # Inline check (avoids circular import with aria_cli.py)
    _coding_kw = ("write", "generate", "create", "script", "code", "plot",
                  "backtest", "策略", "代码", "回测", "编写", "生成", "k线",
                  "k-line", "kline", "python", "dashboard", "写一个", "写代码")
    _is_coding = any(k in message.lower() for k in _coding_kw)
    if _is_coding or _is_stock_chart_analysis_request(message) or _is_visual_market_artifact_request(message):
        return False
    if evidence_required and _extract_market_symbol(message):
        return True
    low = message.lower()
    # Unambiguously market-specific wording.  These carry enough intent on their
    # own to inherit a symbol from an earlier turn ("行情呢" / "现价多少").
    _strong_market_words = (
        "行情", "股价", "涨跌", "涨幅", "现价", "大盘", "收盘", "开盘",
        "market", "quote", "stock price",
    )
    # Generic words that are market-related only in a market conversation.
    # "分析"/"趋势"/"现在" are ordinary Chinese and appear constantly in product
    # and engineering chat — "根据以上分析和建议开始完善" is not a quote request.
    _weak_market_words = (
        "市场", "价格", "今天", "现在", "最新", "分析", "走势", "趋势", "price",
    )
    has_strong = any(k in low for k in _strong_market_words)
    has_weak = any(k in low for k in _weak_market_words)
    if not (has_strong or has_weak):
        return False
    if _extract_market_symbol(message):
        return True
    # Only inherit history if the message doesn't name an unresolvable company
    if _has_unresolved_company_mention(message):
        return False
    # No symbol in the message → inheriting one from history is only justified
    # by explicit market wording, never by a generic word like "分析".
    if not has_strong:
        return False
    return bool(history and _extract_symbol_from_history(history))


def _format_compact_market_cap(value, currency: str = "USD") -> str:
    try:
        cap = float(value)
        if cap <= 0:
            return "—"
        if cap >= 1e12:
            return f"{currency} {cap / 1e12:.2f}T"
        if cap >= 1e9:
            return f"{currency} {cap / 1e9:.1f}B"
        if cap >= 1e6:
            return f"{currency} {cap / 1e6:.0f}M"
        return f"{currency} {cap:,.0f}"
    except Exception:
        return "—"


def _market_snapshot_trend(price, high, low, change_pct) -> str:
    try:
        p = float(price)
        h = float(high) if high not in (None, "", "—") else None
        l = float(low) if low not in (None, "", "—") else None
        c = float(change_pct) if change_pct not in (None, "", "—") else 0.0
        parts = []
        if h is not None and l is not None and h > l:
            pos = int((p - l) / (h - l) * 100)
            if pos >= 75:
                parts.append("near day high")
            elif pos <= 25:
                parts.append("near day low")
            else:
                parts.append("mid-range")
        if abs(c) >= 2:
            parts.append("strong up" if c > 0 else "strong down")
        elif c != 0:
            parts.append("up" if c > 0 else "down")
        return ", ".join(parts) or "flat"
    except Exception:
        return "—"
