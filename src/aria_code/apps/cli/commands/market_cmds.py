"""
MarketCommandsMixin — Market commands: quote, realty, football, screen, news, screen_cn, limitup, north.

Extracted from aria_cli.py. Methods' __globals__ are rebound to aria_cli's namespace
by _rebind_mixin_globals() called at module load time.
"""

from __future__ import annotations

from aria_code.apps.cli.i18n import ui_text

from ._ui import print_error
from typing import Optional, Tuple


_FOOTBALL_CONNECTORS = (
    "对阵", "对战", "对决", " vs ", " VS ", "vs", "VS", " v.s. ",
    " versus ", "跟", "和", "与", "对", "pk", "PK",
)

_FOOTBALL_STRONG_INTENT_TERMS = (
    "比分", "比赛预测", "比赛", "对阵", "交手", "胜负", "几比几",
    "进球", "足球", "国家队", "世界杯", "欧洲杯", "欧冠", "英超",
    "西甲", "德甲", "意甲", "法甲", "中超", "美职联",
    "打败", "战胜", "击败", "打平", "晋级", "出线", "夺冠", "踢",
    "score", "match", "football", "soccer", "beat",
)

_FOOTBALL_AMBIGUOUS_INTENT_TERMS = (
    "预测", "谁赢", "谁能赢", "谁会赢", "结果预测",
    "predict", "prediction", "win",
)

_MARKET_CONTEXT_TERMS = (
    "股票", "股价", "成交量", "市值", "行情", "k线", "K线", "图表",
    "技术指标", "均线", "支撑", "阻力", "涨跌", "涨幅", "跌幅",
    "财报", "财务", "估值", "营收", "利润", "持仓", "风险", "基金",
    "ETF", "etf", "期权", "债券", "外汇", "期货", "RSI", "MACD",
    "买入", "卖出", "做多", "做空", "quote", "stock", "share",
    "equity", "volume", "market cap", "earnings", "revenue", "price",
)


def _rss_items_from_xml(xml_text: str, limit: int = 5) -> list[dict]:
    """Parse simple RSS item fields without external dependencies."""
    import html as _html
    import xml.etree.ElementTree as _ET

    try:
        root = _ET.fromstring(xml_text)
    except Exception:
        return []
    items: list[dict] = []
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        pub_date = item.findtext("pubDate") or item.findtext("published") or ""
        source = item.findtext("source") or ""
        if not title:
            continue
        items.append({
            "title": _html.unescape(title.strip()),
            "url": link.strip(),
            "published_at": pub_date.strip(),
            "source": source.strip() or "RSS",
        })
        if len(items) >= limit:
            break
    return items


def _fetch_public_news_fallback(topic: str, limit: int = 5) -> list[dict]:
    """Fetch public RSS news without API keys.

    Yahoo Finance works well for tickers; Google News RSS covers private
    companies such as SpaceX. This is a best-effort fallback, not a guaranteed
    research source.
    """
    import re as _re
    import urllib.parse as _parse
    import urllib.request as _request

    topic = (topic or "market").strip()
    urls: list[str] = []
    if _re.match(r"^[A-Z]{1,6}(?:[.-][A-Z]{1,3})?$", topic):
        urls.append(
            "https://feeds.finance.yahoo.com/rss/2.0/headline?"
            f"s={_parse.quote(topic)}&region=US&lang=en-US"
        )
    query = f"{topic} latest news when:14d"
    urls.append(
        "https://news.google.com/rss/search?"
        f"q={_parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    headers = {"User-Agent": "Mozilla/5.0 AriaCode/4.1"}
    for url in urls:
        try:
            req = _request.Request(url, headers=headers)
            with _request.urlopen(req, timeout=8) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            items = _rss_items_from_xml(text, limit=limit)
            if items:
                return items
        except Exception:
            continue
    return []


def _is_known_football_name(name: str) -> bool:
    """Return True only when a fragment resolves to a known football team/country."""
    n = (name or "").strip()
    if not n:
        return False
    try:
        from aria_code.clients.football_data_client import _CN_TEAM_MAP, _FIFA_RATINGS
    except Exception:
        return False
    if n in _CN_TEAM_MAP:
        return True
    nl = n.lower()
    for cn, en in _CN_TEAM_MAP.items():
        if n == cn or nl == str(en).lower():
            return True
    for en_key, data in _FIFA_RATINGS.items():
        if nl == str(en_key).lower() or n == str(data.get("name", "")):
            return True
    return False


def _is_probable_football_query(text: str, pair: Optional[Tuple[str, str]] = None) -> bool:
    """Guard the NL football route so finance queries do not enter Poisson mode."""
    raw = text or ""
    if not raw.strip() or raw.strip().startswith("/"):
        return False
    if any(term in raw for term in _MARKET_CONTEXT_TERMS):
        return False
    pair = pair or _parse_nl_team_pair(raw)
    if not pair:
        return False
    known_pair = _is_known_football_name(pair[0]) and _is_known_football_name(pair[1])
    if any(term in raw for term in _FOOTBALL_STRONG_INTENT_TERMS):
        return True
    if any(term in raw for term in _FOOTBALL_AMBIGUOUS_INTENT_TERMS):
        return known_pair
    if not any(conn.lower() in raw.lower() for conn in _FOOTBALL_CONNECTORS):
        return False
    return known_pair


def _parse_nl_team_pair(text: str) -> Optional[Tuple[str, str]]:
    """
    Extract (home_cn, away_cn) from a natural-language football query.

    Handles patterns like:
      "葡萄牙和刚果比赛比分预测"
      "巴西跟阿根廷谁赢"
      "英格兰对阵法国"
      "Germany vs France prediction"    ← English also supported
    Returns None if two teams cannot be confidently identified.
    """
    try:
        from aria_code.clients.football_data_client import _CN_TEAM_MAP, _FIFA_RATINGS
    except Exception:
        return None

    # Build reverse map: english_lower → cn_name (from _CN_TEAM_MAP values)
    _EN_TO_CN: dict = {}
    for cn, en in _CN_TEAM_MAP.items():
        _EN_TO_CN.setdefault(en.lower(), cn)
    # Also add direct FIFA rating keys → cn name
    for en_key, data in _FIFA_RATINGS.items():
        cn_name = data.get("name", "")
        if cn_name and en_key.lower() not in _EN_TO_CN:
            _EN_TO_CN[en_key.lower()] = cn_name

    # Ordered connectors — longer ones first to avoid partial matches
    _CONNECTORS = _FOOTBALL_CONNECTORS
    # Words to strip from team-name fragments
    _STRIP_WORDS = (
        "预测", "分析", "比赛", "比分", "胜率", "结果", "谁赢", "谁会赢",
        "今天", "今日", "明天", "的", "了", "吗", "呢",
        "prediction", "match", "game", "preview", "who wins", "predict",
        "football", "soccer", "score",
    )

    def _clean(s: str) -> str:
        s = s.strip("？！，。、《》（）[]【】:：'\"-— \t")
        for w in sorted(_STRIP_WORDS, key=len, reverse=True):
            s = s.replace(w, "").strip()
        return s.strip()

    def _resolve(name: str) -> Optional[str]:
        """Resolve a name (CN or EN) to its canonical Chinese name."""
        name = name.strip()
        if not name:
            return None
        # Direct CN lookup
        if name in _CN_TEAM_MAP:
            return name
        # English → CN
        nl = name.lower()
        if nl in _EN_TO_CN:
            return _EN_TO_CN[nl]
        # Partial English match
        for en_key, cn_n in _EN_TO_CN.items():
            if nl in en_key or en_key in nl:
                return cn_n
        # Partial CN match
        for cn in _CN_TEAM_MAP:
            if name in cn or cn in name:
                return cn
        # Return as-is if it looks like a real name (≥2 chars)
        return name if len(name) >= 2 else None

    # ── Approach 1: split on connector ───────────────────────────────────────
    for conn in _CONNECTORS:
        if conn.lower() in text.lower():
            idx = text.lower().index(conn.lower())
            left  = _resolve(_clean(text[:idx]))
            right = _resolve(_clean(text[idx + len(conn):]))
            if left and right and left != right:
                return left, right

    # ── Approach 2: scan for all known Chinese team names in text order ──────
    found: list = []
    for cn in _CN_TEAM_MAP:
        if cn in text:
            found.append((text.index(cn), cn))
    # Also scan English names (word-boundary, case-insensitive)
    import re as _re
    tl = text.lower()
    for en_key, cn_n in _EN_TO_CN.items():
        if len(en_key) < 3:
            continue
        m = _re.search(r'\b' + _re.escape(en_key) + r'\b', tl)
        if m:
            found.append((m.start(), cn_n))
    found.sort()
    # Remove duplicates keeping earlier occurrence
    seen_en: set = set()
    unique: list = []
    for pos, cn in found:
        en = _CN_TEAM_MAP.get(cn, cn)
        if en not in seen_en:
            seen_en.add(en)
            unique.append((pos, cn))
    if len(unique) >= 2:
        return unique[0][1], unique[1][1]

    return None


import logging
logger = logging.getLogger(__name__)


def _news_date(raw: str) -> str:
    """A feed's publish time as 2026-10-05.

    The first ten characters were kept, which suits ISO dates and cut an RSS
    date mid-word: "Mon, 05 Oct 2026 12:00:00 GMT" showed as "Mon, 05 Oc".
    """
    from datetime import datetime
    from email.utils import parsedate_to_datetime

    text = str(raw or "").strip()
    for parse in (lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")), parsedate_to_datetime):
        try:
            return parse(text).strftime("%Y-%m-%d")
        except (TypeError, ValueError, IndexError):
            continue
    return text[:10]


class MarketCommandsMixin:
    """Mixin: Market commands: quote, realty, football, screen, news, screen_cn, limitup, north."""

    async def cmd_realty(self, args: str):
        """Realty vertical moved to external backend/MCP."""
        if self.context.has_rich:
            self.context.console.print("[dim]/realty 已迁移至外部服务。[/dim]")

    async def cmd_football(self, args: str):
        """Sports vertical moved to external backend/MCP."""
        if self.context.has_rich:
            self.context.console.print("[dim]/football 已迁移至外部服务。[/dim]")

    async def cmd_screen(self, args: str):
        """Stock screening moved to Arthera backend service."""
        if self.context.has_rich:
            self.context.console.print("[dim]/screen 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_news(self, args: str):
        """Financial news moved to Arthera backend service."""
        if self.context.has_rich:
            self.context.console.print("[dim]/news 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_quote(self, args: str):
        """Market quotes moved to Arthera backend service."""
        if self.context.has_rich:
            self.context.console.print("[dim]/quote 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_screen_cn(self, args: str):
        """A-share screening moved to Arthera backend service."""
        if self.context.has_rich:
            self.context.console.print("[dim]/screen-cn 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_limitup(self, args: str):
        """A-share limit-up pool moved to Arthera backend service."""
        if self.context.has_rich:
            self.context.console.print("[dim]/limitup 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_north(self, args: str):
        """Northbound flow moved to Arthera backend service."""
        if self.context.has_rich:
            self.context.console.print("[dim]/north 已迁移至 Arthera 后端服务。[/dim]")
