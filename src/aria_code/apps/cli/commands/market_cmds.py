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
import json
import asyncio
import datetime
import time
import shlex
from typing import Dict, Any, Optional

def print_quote_result(*args, **kwargs):
    from aria_cli import print_quote_result as fn
    return fn(*args, **kwargs)
# execute_aria_tool lives in apps/cli/tool_executor.py and needs nothing from
# aria_cli — the cache it reads is owned there now, so the hop was pointless.
from aria_code.apps.cli.tool_executor import execute_aria_tool
def _is_ashare_symbol(*args, **kwargs):
    from aria_code.apps.cli.helpers import _is_ashare_symbol as fn
    return fn(*args, **kwargs)
def _prompt_str(*args, **kwargs):
    from aria_cli import _prompt_str as fn
    return fn(*args, **kwargs)
def _render_reits_list(*args, **kwargs):
    from aria_cli import _render_reits_list as fn
    return fn(*args, **kwargs)
def _render_multi_city(*args, **kwargs):
    from aria_cli import _render_multi_city as fn
    return fn(*args, **kwargs)
def _get_LOCAL_TOOLS():
    # Owned by apps/cli/tool_registry.py; aria_cli fills it in place.
    from ..tool_registry import LOCAL_TOOLS as val
    return val
def _render_property_val(*args, **kwargs):
    from aria_cli import _render_property_val as fn
    return fn(*args, **kwargs)
def _get_mdc(*args, **kwargs):
    from .._optional import get_mdc as fn
    return fn(*args, **kwargs)
def _load_data_keys(*args, **kwargs):
    from aria_code.apps.cli.provider_keys import load_data_keys as fn
    return fn(*args, **kwargs)
def _render_asset_score(*args, **kwargs):
    from aria_cli import _render_asset_score as fn
    return fn(*args, **kwargs)
def _render_rental_yield(*args, **kwargs):
    from aria_cli import _render_rental_yield as fn
    return fn(*args, **kwargs)
def _render_house_price(*args, **kwargs):
    from aria_cli import _render_house_price as fn
    return fn(*args, **kwargs)
def format_quote_output(*args, **kwargs):
    from aria_cli import format_quote_output as fn
    return fn(*args, **kwargs)
def _prompt_float(*args, **kwargs):
    from aria_cli import _prompt_float as fn
    return fn(*args, **kwargs)
def _ashare_code_to_name(*args, **kwargs):
    from aria_cli import _ashare_code_to_name as fn
    return fn(*args, **kwargs)
def _get__HAS_MDC():
    from .._optional import HAS_MDC as val
    return val
def parse_symbols(*args, **kwargs):
    from aria_cli import parse_symbols as fn
    return fn(*args, **kwargs)

import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional


import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional

# A real logger. This was a `def logger(*args, **kwargs)` forwarding to
# aria_cli, so every `logger.debug(...)` raised AttributeError — and all but
# one of them sit in an except block, where the failure replaced whatever
# error was being reported.
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
        """股票筛选: CN → screen_ashare; US → yfinance 大盘成分筛选."""
        criteria = args.strip() or ""
        low = criteria.lower()

        # CN market detection
        _cn_kw = ("a股", "沪深", "创业板", "科创板", "港股", "cn", "ashare", "沪市", "深市")
        _is_cn = any(k in low for k in _cn_kw) or any(c.isdigit() for c in criteria[:6])

        if _is_cn:
            params: Dict[str, Any] = {}
            for tok in args.split():
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    params[k.strip()] = v.strip()
            if "screen_ashare" in _get_LOCAL_TOOLS():
                await self._run_local_tool("screen_ashare", params, "A股选股筛选")
            else:
                await self.terminal.send_message(f"帮我筛选A股股票，条件：{criteria or '市值>50亿，非ST，流动性好'}")
            return

        # US / global: yfinance-based screening on a reference pool
        import asyncio as _asyncio
        _loop = _asyncio.get_event_loop()

        _US_POOL = [
            "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","BRK-B","JPM","V",
            "UNH","XOM","JNJ","WMT","MA","PG","LLY","HD","CVX","MRK",
            "ABBV","PEP","KO","AVGO","COST","BAC","TMO","MCD","ACN","ADBE",
            "CRM","NFLX","AMD","TXN","QCOM","INTC","CSCO","WFC","PM","VZ",
            "RTX","HON","AMGN","LIN","DHR","UNP","CAT","SBUX","GS","BA",
        ]

        # Map common text criteria to filter presets
        _growth_kw  = ("growth", "成长", "高增速", "tech", "科技", "ai", "人工智能")
        _value_kw   = ("value", "价值", "低估", "dividend", "分红")
        _momentum_kw= ("momentum", "动量", "趋势", "breakout", "突破")
        _is_growth    = any(k in low for k in _growth_kw)
        _is_value     = any(k in low for k in _value_kw)
        _is_momentum  = any(k in low for k in _momentum_kw)

        def _fetch_pool():
            try:
                import yfinance as _yf
                tickers = _yf.Tickers(" ".join(_US_POOL))
                rows = []
                for sym in _US_POOL:
                    try:
                        info = tickers.tickers[sym].fast_info
                        price     = getattr(info, "last_price", None) or 0
                        mktcap    = getattr(info, "market_cap", None) or 0
                        pe        = None   # filled below: fast_info has no P/E
                        yr_return = getattr(info, "year_change", None)
                        rows.append({
                            "symbol": sym, "price": price,
                            "mktcap": mktcap, "pe": pe,
                            "yr_return": yr_return,
                        })
                    except Exception:
                        pass
                # fast_info has no pe_ratio, so P/E was blank for every row and
                # the "value" screen (P/E 5-20) never matched anything. info has
                # trailingPE; fetched in parallel, one request per symbol.
                from concurrent.futures import ThreadPoolExecutor

                def _pe(row):
                    try:
                        value = tickers.tickers[row["symbol"]].info.get("trailingPE")
                        row["pe"] = float(value) if value else None
                    except Exception:
                        row["pe"] = None
                with ThreadPoolExecutor(max_workers=10) as pool:
                    list(pool.map(_pe, rows))
                return rows
            except Exception as _e:
                logger.debug("screen US fetch error: %s", _e)
                return []

        if self.context.has_rich:
            _status_msg = "[dim]" + ui_text(self, f"筛选 {len(_US_POOL)} 只美股 ({criteria or '按市值'})…",
                                            f"Screening {len(_US_POOL)} US stocks ({criteria or 'by market cap'})…") + "[/dim]"
            with self.context.console.status(_status_msg, spinner="dots"):
                rows = await _loop.run_in_executor(None, _fetch_pool)
        else:
            print("  " + ui_text(self, "筛选美股中…", "Screening US stocks…"))
            rows = await _loop.run_in_executor(None, _fetch_pool)

        if not rows:
            await self.terminal.send_message(
                f"Screen US stocks matching: {criteria or 'large-cap'}. "
                "Show top 10 with price, P/E, market cap, 1-year return."
            )
            return

        # Apply simple filters
        if _is_growth:
            rows = [r for r in rows if (r.get("yr_return") or 0) > 0.15]
        elif _is_value:
            rows = [r for r in rows if r.get("pe") and 5 < r["pe"] < 20]
        elif _is_momentum:
            rows = sorted(rows, key=lambda r: r.get("yr_return") or 0, reverse=True)
        else:
            rows = sorted(rows, key=lambda r: r.get("mktcap") or 0, reverse=True)

        rows = rows[:15]

        if not rows:
            msg = "[yellow]" + ui_text(self, f"当前条件 '{criteria}' 无匹配标的（池: {len(_US_POOL)} 只）",
                                       f"Nothing matches '{criteria}' (pool: {len(_US_POOL)} stocks)") + "[/yellow]"
            self.context.console.print(msg) if self.context.has_rich else print(msg.replace("[yellow]","").replace("[/yellow]",""))
            return

        if self.context.has_rich:
            from rich.table import Table as _Tbl
            t = _Tbl(title=ui_text(self, f"美股筛选  {criteria or 'large-cap'}  共 {len(rows)} 只",
                                   f"US screen  {criteria or 'large-cap'}  {len(rows)} stocks"),
                     show_header=True, box=None, padding=(0, 1))
            t.add_column(ui_text(self, "代码", "Symbol"), style="bold", width=8)
            t.add_column(ui_text(self, "价格", "Price"), justify="right")
            t.add_column(ui_text(self, "市值(B$)", "Mkt cap ($B)"), justify="right", style="dim")
            t.add_column(ui_text(self, "市盈率", "P/E (ttm)"), justify="right", style="dim")
            t.add_column(ui_text(self, "年涨跌%", "1y %"), justify="right")
            for r in rows:
                yr  = r.get("yr_return")
                yr_s = f"{yr*100:+.1f}%" if yr is not None else "—"
                yr_color = "green" if (yr or 0) >= 0 else "red"
                pe_s = f"{r['pe']:.1f}" if r.get("pe") and r["pe"] == r["pe"] else "—"
                mc_s = f"{r['mktcap']/1e9:.0f}" if (r.get("mktcap") or 0) > 0 else "—"
                t.add_row(
                    r["symbol"],
                    f"{r['price']:.2f}" if r.get("price") else "—",
                    mc_s, pe_s,
                    f"[{yr_color}]{yr_s}[/{yr_color}]",
                )
            self.context.console.print(t)
            self.context.console.print("  [dim]" + ui_text(self, f"来源: yfinance · 池: {len(_US_POOL)} 只大市值美股",
                                                          f"Source: yfinance · pool: {len(_US_POOL)} large-cap US stocks")
                                       + "[/dim]")
        else:
            print(f"  美股筛选  {criteria}")
            for r in rows:
                yr = r.get("yr_return")
                yr_s = f"{yr*100:+.1f}%" if yr is not None else "—"
                print(f"  {r['symbol']:<8} ${r.get('price',0):.2f}  {yr_s}")

    async def cmd_news(self, args: str):
        """Fetch latest financial news for a topic or symbol.

        Usage: /news [topic|symbol] [--limit N]
        Examples:
          /news AAPL
          /news earnings --limit 10
          /news crypto --limit 3
        """
        parts = args.split()
        limit = 5
        topic_parts = []
        i = 0
        while i < len(parts):
            if parts[i] == "--limit" and i + 1 < len(parts):
                try:
                    limit = max(1, min(20, int(parts[i + 1])))
                    i += 2
                    continue
                except ValueError:
                    pass
            topic_parts.append(parts[i])
            i += 1
        topic = " ".join(topic_parts) or "market"

        self.context.console.print(f"[dim]Fetching {limit} news items for '{topic}'...[/dim]" if self.context.has_rich
                      else f"Fetching news for {topic}...")

        # Try backend first, then local tools (Finnhub / NewsAPI / AKShare fallback chain)
        result = await execute_aria_tool(self.terminal.api_url, "analyze_news", {
            "query": topic, "limit": limit,
        })
        if not result.get("success") and "analyze_news" in _get_LOCAL_TOOLS():
            # Local fallback: uses Finnhub → NewsAPI → AKShare depending on configured keys
            local_fn = _get_LOCAL_TOOLS()["analyze_news"][0]
            result = await asyncio.get_event_loop().run_in_executor(
                None, local_fn, {"query": topic, "symbol": topic, "limit": limit}
            )
        if result.get("success"):
            data = result.get("data", {})
            if isinstance(data, dict):
                articles = data.get("articles", data.get("news", []))
                sentiment = data.get("sentiment", data.get("overall_sentiment", ""))
            elif isinstance(data, list):
                articles = data
                sentiment = ""
            else:
                articles = []
                sentiment = ""
            if not (isinstance(articles, list) and articles):
                articles = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_public_news_fallback, topic, limit
                )
                if articles:
                    sentiment = "public RSS fallback"
            if isinstance(articles, list) and articles:
                if self.context.has_rich:
                    self.context.console.print()
                    if sentiment:
                        sent_color = "green" if "positive" in sentiment.lower() or "bullish" in sentiment.lower() else (
                            "red" if "negative" in sentiment.lower() or "bearish" in sentiment.lower() else "yellow"
                        )
                        self.context.console.print(f"  Sentiment: [{sent_color}]{sentiment}[/{sent_color}]")
                        self.context.console.print()
                for idx, a in enumerate(articles[:limit], 1):
                    if isinstance(a, dict):
                        title = a.get("title", "Untitled")
                        source = a.get("source", a.get("publisher", ""))
                        url_item = a.get("url", a.get("link", ""))
                        pub_date = a.get("published_at", a.get("date", a.get("publishedAt", "")))
                        if pub_date:
                            pub_date = _news_date(pub_date)
                    else:
                        title = str(a)
                        source = pub_date = url_item = ""
                    if self.context.has_rich:
                        self.context.console.print(f"  [bold]{idx}.[/bold] {title}")
                        meta_parts = [p for p in [source, pub_date] if p]
                        if meta_parts:
                            self.context.console.print(f"     [dim]{' · '.join(meta_parts)}[/dim]")
                    else:
                        meta = f" ({source})" if source else ""
                        print(f"  {idx}. {title}{meta}")
                if self.context.has_rich:
                    self.context.console.print()
            else:
                # Empty articles — show helpful config guidance
                _data_keys = _load_data_keys()
                if self.context.has_rich:
                    self.context.console.print()
                    self.context.console.print(f"  [dim]未找到 '{topic}' 的相关新闻。[/dim]")
                    if not _data_keys.get("finnhub") and not _data_keys.get("newsapi"):
                        self.context.console.print("  [dim]配置数据服务 key 可获取更多新闻来源：[/dim]")
                        self.context.console.print("  [dim]  /apikey set finnhub <key>   →  https://finnhub.io/register[/dim]")
                        self.context.console.print("  [dim]  /apikey set newsapi <key>   →  https://newsapi.org/register[/dim]")
                    self.context.console.print()
        else:
            articles = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_public_news_fallback, topic, limit
            )
            if isinstance(articles, list) and articles:
                if self.context.has_rich:
                    self.context.console.print()
                    self.context.console.print("  [dim]新闻 API 不可用，已使用公共 RSS fallback。[/dim]")
                    self.context.console.print()
                for idx, a in enumerate(articles[:limit], 1):
                    title = a.get("title", "Untitled") if isinstance(a, dict) else str(a)
                    source = a.get("source", "") if isinstance(a, dict) else ""
                    pub_date = a.get("published_at", "") if isinstance(a, dict) else ""
                    if pub_date:
                        pub_date = _news_date(pub_date)
                    if self.context.has_rich:
                        self.context.console.print(f"  [bold]{idx}.[/bold] {title}")
                        meta_parts = [p for p in [source, pub_date] if p]
                        if meta_parts:
                            self.context.console.print(f"     [dim]{' · '.join(meta_parts)}[/dim]")
                    else:
                        meta = f" ({source})" if source else ""
                        print(f"  {idx}. {title}{meta}")
                if self.context.has_rich:
                    self.context.console.print()
                return

            # Backend + all local fallbacks unavailable — show actionable config guide
            err = result.get("error", "")
            _data_keys = _load_data_keys()
            _has_finnhub = bool(_data_keys.get("finnhub"))
            _has_newsapi = bool(_data_keys.get("newsapi"))
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print("  [yellow]⚠ 新闻服务不可用[/yellow]")
                if not _has_finnhub and not _has_newsapi:
                    self.context.console.print("  [dim]配置以下任意一个数据服务 key 即可获取新闻：[/dim]")
                    self.context.console.print("  [dim]  Finnhub  (免费60次/分) → /apikey set finnhub <key>   注册: https://finnhub.io/register[/dim]")
                    self.context.console.print("  [dim]  NewsAPI  (免费100次/天) → /apikey set newsapi <key>   注册: https://newsapi.org/register[/dim]")
                else:
                    self.context.console.print(f"  [dim]错误: {err[:120] if err else '获取失败'}[/dim]")
                self.context.console.print(f"  [dim]或使用: /web {topic} latest news — 通过 Brave 搜索[/dim]")
                self.context.console.print()
            else:
                print("  News unavailable. Configure: /apikey set finnhub <key>")

    async def cmd_quote(self, args: str):
        symbols = parse_symbols(args, self.terminal.config.get("watchlist", ["AAPL"]))

        # 优先使用 MarketDataClient（真实实时数据，代理绕过）
        if _get__HAS_MDC():
            mdc = _get_mdc()
            if self.context.has_rich:
                self.context.console.print()
            for symbol in symbols:
                if self.context.has_rich:
                    with self.context.console.status(f"[dim]{symbol}...[/dim]", spinner="dots"):
                        loop = asyncio.get_event_loop()
                        r = await loop.run_in_executor(None, mdc.quote, symbol)
                else:
                    r = mdc.quote(symbol)

                if r.get("success"):
                    name    = r.get("name", symbol)
                    # Supplement Chinese name for A-shares where yfinance returns ASCII
                    if _is_ashare_symbol(symbol) and (not name or name == symbol or name.replace(" ","").isascii()):
                        _cn = _ashare_code_to_name(symbol)
                        if _cn:
                            name = _cn
                    print_quote_result(console=self.context.console, has_rich=self.context.has_rich, symbol=symbol, quote=r, name=name)
                else:
                    print_quote_result(console=self.context.console, has_rich=self.context.has_rich, symbol=symbol, quote=r)
            if self.context.has_rich:
                self.context.console.print()
            return

        # Fallback：原有 Aria 工具
        for symbol in symbols:
            if self.context.has_rich:
                with self.context.console.status(f"[dim]Fetching {symbol}...[/dim]", spinner="dots"):
                    result = await execute_aria_tool(self.terminal.api_url, "get_market_data", {
                        "symbol": symbol, "market": "US", "period": "1mo"
                    })
            else:
                print(f"Fetching {symbol}...")
                result = await execute_aria_tool(self.terminal.api_url, "get_market_data", {
                    "symbol": symbol, "market": "US", "period": "1mo"
                })
            if not result:
                print_error(self.context, f"{symbol}: 数据服务不可用（API未运行）", "tool")
                continue
            if result.get("success") and result.get("data"):
                output = format_quote_output(result)
                self.context.console.print(output)
            else:
                print_error(self.context, f"Failed: {result.get('error', 'No data')}")

    async def cmd_screen_cn(self, args: str):
        """A股选股筛选器 (local, akshare)."""
        params: Dict[str, Any] = {}
        for tok in args.split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                params[k.strip()] = v.strip()
        tool_name = "screen_ashare"
        if tool_name in _get_LOCAL_TOOLS():
            await self._run_local_tool(tool_name, params, "A股选股筛选")
        else:
            await self.terminal.send_message(f"帮我筛选A股股票，条件：{args or '市值>50亿，非ST，流动性好'}")

    async def cmd_limitup(self, args: str):
        """A股涨停板池.  Usage: /limitup [YYYY-MM-DD] [code_filter]"""
        import re as _re_lu
        arg = args.strip()

        # Detect if arg looks like a stock code (6 digits) vs a date
        _is_code  = bool(_re_lu.match(r'^[036]\d{5}$', arg))
        _is_date  = bool(_re_lu.match(r'^\d{4}-\d{2}-\d{2}$', arg))
        _code_filter = arg if _is_code else None
        _date_arg    = arg if _is_date else ""
        params = {"date": _date_arg} if _date_arg else {}

        tool_name = "get_limit_up_pool"
        if tool_name in _get_LOCAL_TOOLS():
            await self._run_local_tool(tool_name, params, "涨停板池")
        else:
            # Direct akshare fallback — avoids "A股" keyword triggering market snapshot routing
            try:
                import akshare as ak
                from datetime import date as _dt
                _date_str = (_date_arg.replace("-", "") if _date_arg
                             else _dt.today().strftime("%Y%m%d"))
                _df = ak.stock_zt_pool_em(date=_date_str)
                if _df is not None and not _df.empty:
                    if _code_filter:
                        _col = next((c for c in _df.columns if "代码" in str(c) or c == "code"), None)
                        if _col:
                            _df = _df[_df[_col].astype(str) == _code_filter]
                    _count = len(_df)
                    if self.context.has_rich:
                        from rich.table import Table
                        _date_label = _date_arg or _dt.today().isoformat()
                        tbl = Table(title=f"涨停板池 · {_date_label} · {_count}只", show_header=True, header_style="bold")
                        _col_map = {"代码": "代码", "名称": "名称", "涨停统计": "涨停统计",
                                    "连续涨停": "连板", "首次封板时间": "首封", "涨停类型": "类型"}
                        _show_cols = [c for c in _df.columns if c in _col_map]
                        for c in _show_cols:
                            tbl.add_column(_col_map.get(c, c), no_wrap=True)
                        for _, row in _df.head(30).iterrows():
                            tbl.add_row(*[str(row[c]) for c in _show_cols])
                        self.context.console.print(tbl)
                    else:
                        print(f"涨停板池 {_count}只")
                        for _, row in _df.head(20).iterrows():
                            print(f"  {row.get('代码','')} {row.get('名称','')}")
                    return
            except Exception as _e:
                pass
            if self.context.has_rich:
                self.context.console.print("[yellow]akshare 暂不可用，涨停板池无法获取[/yellow]")
            else:
                print("akshare unavailable, cannot fetch limit-up pool")

    async def cmd_north(self, args: str):
        """北向资金净流入."""
        params = {"days": int(args.strip())} if args.strip().isdigit() else {"days": 10}
        tool_name = "get_northbound_flow"
        if tool_name in _get_LOCAL_TOOLS():
            await self._run_local_tool(tool_name, params, "北向资金")
        else:
            await self.terminal.send_message("查询最近10天北向资金（沪深港通）净买入情况")
