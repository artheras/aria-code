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
def _football_fixtures(*args, **kwargs):
    from aria_cli import _football_fixtures as fn
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
def _football_team(*args, **kwargs):
    from aria_cli import _football_team as fn
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
def _football_h2h(*args, **kwargs):
    from aria_cli import _football_h2h as fn
    return fn(*args, **kwargs)
def _football_standings(*args, **kwargs):
    from aria_cli import _football_standings as fn
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
        """
        /realty market [city1] [city2]  — 城市房价指数
        /realty reit [code]             — REIT 列表或单只分析
        /realty valuation               — 物业估值计算器（交互式）
        /realty rent                    — 租金收益率计算（交互式）
        /realty compare [cities...]     — 多城市对比
        /realty score                   — 资产区位评分（交互式）
        /realty us                      — 美国住房数据
        """
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        parts = args.strip().split() if args.strip() else []
        sub = parts[0].lower() if parts else "market"

        try:
            from aria_code.tools.realty_data_tools import (
                get_house_price_index, get_re_investment,
                get_reits_list, get_reit_analysis, get_multi_city_comparison,
                calc_rental_yield, property_valuation, asset_location_score,
                get_us_housing_data,
            )
        except ImportError as e:
            if self.context.has_rich:
                self.context.console.print(f"[red]realty_data_tools 未加载: {e}[/red]")
            return

        if sub == "market":
            city1 = parts[1] if len(parts) > 1 else "北京"
            city2 = parts[2] if len(parts) > 2 else ("上海" if city1 != "上海" else "北京")
            import functools as _functools
            if self.context.has_rich:
                with self.context.console.status(f"[dim]{ui_text(self, f'获取 {city1}/{city2} 房价指数', f'Fetching {city1}/{city2} house price indices')}...[/dim]", spinner="dots"):
                    r = await loop.run_in_executor(
                        None, _functools.partial(get_house_price_index, city1, city2)
                    )
            else:
                r = get_house_price_index(city1, city2)
            _render_house_price(r)
            # Also show investment data
            if self.context.has_rich:
                with self.context.console.status(f"[dim]{ui_text(self, '获取房地产投资数据', 'Fetching real-estate investment data')}...[/dim]", spinner="dots"):
                    ri = await loop.run_in_executor(None, get_re_investment)
            else:
                ri = get_re_investment()
            if ri.get("success") and ri.get("latest"):
                lt = ri["latest"]
                if self.context.has_rich:
                    self.context.console.print(f"\n  [dim]房地产开发投资[/dim]  {lt.get('日期','')}  "
                                  f"最新值 [bold]{lt.get('最新值','')}[/bold]  "
                                  f"涨跌 {lt.get('涨跌幅','')}  "
                                  f"近1年 {lt.get('近1年涨跌幅','')}")

        elif sub == "reit":
            code = parts[1] if len(parts) > 1 else None
            if code:
                if self.context.has_rich:
                    with self.context.console.status(f"[dim]{ui_text(self, f'分析 {code} REIT', f'Analyzing REIT {code}')}...[/dim]", spinner="dots"):
                        r = await loop.run_in_executor(None, get_reit_analysis, code)
                else:
                    r = get_reit_analysis(code)
                if r.get("success"):
                    if self.context.has_rich:
                        self.context.console.print(f"\n  [bold cyan]{r.get('code','')}[/bold cyan] "
                                      f"[dim]{r.get('name','')}[/dim]")
                        self.context.console.print(f"  现价 [bold]{r.get('price','')}[/bold]  "
                                      f"涨跌 {r.get('chg_pct','')}%")
                        if r.get("return_1y") is not None:
                            rc = "green" if r["return_1y"] > 0 else "red"
                            self.context.console.print(f"  近1年收益: [{rc}]{r['return_1y']:+.2f}%[/{rc}]")
                        if r.get("volatility_annual"):
                            self.context.console.print(f"  年化波动率: {r['volatility_annual']:.2f}%")
                else:
                    self.context.console.print(f"[red]{r.get('error','分析失败')}[/red]") if self.context.has_rich else None
            else:
                if self.context.has_rich:
                    with self.context.console.status(f"[dim]{ui_text(self, '获取 REIT 列表', 'Fetching the REIT list')}...[/dim]", spinner="dots"):
                        r = await loop.run_in_executor(None, get_reits_list)
                else:
                    r = get_reits_list()
                _render_reits_list(r)

        elif sub == "compare":
            cities = parts[1:] if len(parts) > 1 else None
            if self.context.has_rich:
                with self.context.console.status(f"[dim]{ui_text(self, '对比多城市房价', 'Comparing city house prices')}...[/dim]", spinner="dots"):
                    r = await loop.run_in_executor(None, get_multi_city_comparison, cities)
            else:
                r = get_multi_city_comparison(cities)
            _render_multi_city(r)

        elif sub in ("rent", "rental"):
            if self.context.has_rich:
                self.context.console.print("[bold]💰 租金收益率计算器[/bold]  [dim](输入 0 跳过可选项)[/dim]")
            price_wan  = _prompt_float("购入价格(万元): ", 200.0)
            monthly_rent = _prompt_float("月租金(元): ", 5000.0)
            annual_costs = _prompt_float("年维护成本(元)[可选]: ", 0.0)
            loan_ratio = _prompt_float("贷款成数 0-1 (如0.7=七成)[可选]: ", 0.0)
            p = {"purchase_price": price_wan, "monthly_rent": monthly_rent,
                 "annual_costs": annual_costs, "loan_ratio": loan_ratio}
            r = calc_rental_yield(p)
            _render_rental_yield(r)

        elif sub in ("valuation", "val"):
            if self.context.has_rich:
                self.context.console.print("[bold]🏢 物业估值计算器[/bold]")
            area = _prompt_float("建筑面积(㎡): ", 100.0)
            monthly_rent = _prompt_float("月租金(元): ", 5000.0)
            tier = _prompt_str("区位层级 (tier1/tier2/tier3): ", "tier2")
            p = {"area_sqm": area, "monthly_rent": monthly_rent, "location_tier": tier}
            r = property_valuation(p)
            _render_property_val(r)

        elif sub == "score":
            if self.context.has_rich:
                self.context.console.print("[bold]📍 资产区位评分[/bold]")
            city  = _prompt_str("城市: ", "上海")
            area  = _prompt_float("建筑面积(㎡): ", 100.0)
            floor_n = int(_prompt_float("楼层: ", 1.0))
            traffic = _prompt_str("客流量 (high/medium/low): ", "medium")
            fire_ok = _prompt_str("允许明火? (y/n): ", "n").lower() in ("y","yes","是")
            reno_ok = _prompt_str("允许改造? (y/n): ", "y").lower() in ("y","yes","是")
            p = {"city": city, "area_sqm": area, "floor": floor_n,
                 "foot_traffic": traffic, "open_fire_allowed": fire_ok,
                 "renovation_allowed": reno_ok}
            r = asset_location_score(p)
            _render_asset_score(r)

        elif sub == "us":
            if self.context.has_rich:
                with self.context.console.status(f"[dim]{ui_text(self, '获取美国住房数据', 'Fetching US housing data')}...[/dim]", spinner="dots"):
                    r = await loop.run_in_executor(None, get_us_housing_data)
            else:
                r = get_us_housing_data()
            if not r.get("success"):
                if self.context.has_rich: self.context.console.print(f"[red]{r.get('error')}[/red]")
                return
            if self.context.has_rich:
                from rich.table import Table as _T
                from rich import box as _box
                tb = _T(title="[bold]🏠 美国住房市场数据[/bold]", box=_box.ROUNDED)
                tb.add_column("指标", style="dim"); tb.add_column("最新值"); tb.add_column("日期", style="dim")
                for key, val in r.get("data", {}).items():
                    lt = val.get("latest", {})
                    v = lt.get("value")
                    tb.add_row(val.get("label", key), str(v) if v else "—", str(lt.get("date",""))[:7])
                self.context.console.print(tb)
                for line in r.get("assessment", []):
                    self.context.console.print(f"  [dim]▸ {line}[/dim]")

        else:
            if self.context.has_rich:
                self.context.console.print("[dim]用法: /realty [market|reit|compare|rent|valuation|score|us][/dim]")
                self.context.console.print("[dim]示例: /realty market 北京 上海[/dim]")
                self.context.console.print("[dim]      /realty reit 508603[/dim]")
                self.context.console.print("[dim]      /realty rent  (交互式租金计算)[/dim]")
                self.context.console.print("[dim]      /realty compare 北京 上海 成都 杭州[/dim]")

    async def cmd_football(self, args: str):
        """
        足球赛事分析和预测

        子命令:
          /football standings <联赛>              联赛积分榜
          /football fixtures  <联赛> [days]       近期赛程（默认7天）
          /football predict   <主队> vs <客队> [联赛]  比赛预测
          /football team      <球队名> [联赛]      球队近期状态
          /football h2h       <队1> vs <队2> [联赛]   历史交锋

        联赛代码: pl/epl/英超  bl/德甲  ll/西甲  sa/意甲  fl1/法甲  cl/欧冠
        示例:
          /football standings pl
          /football predict Arsenal vs Chelsea pl
          /football team Manchester City pl
          /football fixtures cl 14
        """
        from rich.table import Table
        from rich import box as rich_box
        from rich.panel import Panel

        parts = args.strip().split()
        if not parts:
            self.context.console.print(Panel(
                "[bold]足球分析命令[/bold]\n\n"
                "  [cyan]/football standings pl[/cyan]              英超积分榜\n"
                "  [cyan]/football fixtures cl 14[/cyan]            欧冠未来14天赛程\n"
                "  [cyan]/football predict Arsenal vs Chelsea[/cyan] 预测比赛结果\n"
                "  [cyan]/football team Bayern Munich bl[/cyan]     球队近期状态\n"
                "  [cyan]/football h2h Barcelona vs Real Madrid[/cyan] 历史交锋\n\n"
                "[dim]联赛: pl/英超  bl/德甲  ll/西甲  sa/意甲  fl1/法甲  cl/欧冠[/dim]\n"
                "[dim]需要设置 FOOTBALL_DATA_API_KEY（football-data.org 免费注册）[/dim]",
                title="[bold]⚽ Football Analyst[/bold]",
                border_style="green",
            ))
            return

        sub = parts[0].lower()

        # ── standings ──────────────────────────────────────────────────────────
        if sub == "standings":
            league = parts[1] if len(parts) > 1 else "pl"
            await self._run_in_executor(_football_standings, league)

        # ── fixtures ──────────────────────────────────────────────────────────
        elif sub in ("fixtures", "schedule", "赛程"):
            league  = parts[1] if len(parts) > 1 else "pl"
            days    = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 7
            await self._run_in_executor(_football_fixtures, league, days)

        # ── predict ───────────────────────────────────────────────────────────
        elif sub in ("predict", "预测", "prediction"):
            raw = " ".join(parts[1:])
            if " vs " in raw.lower():
                idx     = raw.lower().index(" vs ")
                home    = raw[:idx].strip()
                rest    = raw[idx + 4:].strip()
                away_parts = rest.split()
                # last token might be league code (including wc/世界杯)
                from aria_code.clients.football_data_client import LEAGUE_IDS, TOURNAMENT_CODES
                _all_codes = {**LEAGUE_IDS, **{k: v for k, v in TOURNAMENT_CODES.items()}}
                if away_parts and away_parts[-1].lower().replace(" ", "") in _all_codes:
                    league = away_parts[-1]
                    away   = " ".join(away_parts[:-1])
                elif away_parts and away_parts[-1].lower() in ("wc", "worldcup", "世界杯", "ca", "ec"):
                    league = away_parts[-1]
                    away   = " ".join(away_parts[:-1])
                else:
                    league = "pl"
                    away   = rest
                await self._football_predict(home, away, league)
            else:
                self.context.console.print("[red]用法: /football predict <主队> vs <客队> [联赛/wc][/red]")

        # ── team ──────────────────────────────────────────────────────────────
        elif sub in ("team", "球队"):
            rest  = " ".join(parts[1:])
            from aria_code.clients.football_data_client import LEAGUE_IDS
            tokens = rest.split()
            if tokens and tokens[-1].lower() in LEAGUE_IDS:
                league = tokens[-1]
                team   = " ".join(tokens[:-1])
            else:
                league = "pl"
                team   = rest
            await self._run_in_executor(_football_team, team, league)

        # ── h2h ───────────────────────────────────────────────────────────────
        elif sub in ("h2h", "历史", "对决"):
            raw = " ".join(parts[1:])
            if " vs " in raw.lower():
                idx  = raw.lower().index(" vs ")
                t1   = raw[:idx].strip()
                rest = raw[idx + 4:].strip()
                from aria_code.clients.football_data_client import LEAGUE_IDS
                tokens = rest.split()
                if tokens and tokens[-1].lower() in LEAGUE_IDS:
                    league = tokens[-1]
                    t2     = " ".join(tokens[:-1])
                else:
                    league = "pl"
                    t2     = rest
                await self._run_in_executor(_football_h2h, t1, t2, league)
            else:
                self.context.console.print("[red]用法: /football h2h <队1> vs <队2> [联赛][/red]")

        else:
            # NL intent: /football 预测加拿大跟波黑... or /football 分析...
            full_args = args.strip()
            _has_cn = any('一' <= c <= '鿿' for c in full_args)
            _has_kw = any(k in full_args.lower() for k in (
                "predict", "preview", "analyze", "analysis", "who wins",
                "预测", "分析", "谁赢", "比分", "胜率", "谁先", "开球",
            ))
            if _has_cn or _has_kw:
                # ── Step 1: Parse two team names from NL text ─────────────────
                _nl_pair = _parse_nl_team_pair(full_args)
                if _nl_pair:
                    _h_cn, _a_cn = _nl_pair
                    # Determine league: national teams → wc, club → pl default
                    try:
                        from aria_code.clients.football_data_client import _CN_TEAM_MAP, _find_fifa_rating
                        _h_en = _CN_TEAM_MAP.get(_h_cn, _h_cn)
                        _a_en = _CN_TEAM_MAP.get(_a_cn, _a_cn)
                        _is_nat = bool(_find_fifa_rating(_h_en) or _find_fifa_rating(_a_en))
                    except Exception:
                        _is_nat = True
                    _nl_league = "wc" if _is_nat else "pl"
                    await self._football_predict(_h_cn, _a_cn, _nl_league)
                    return

                # ── Step 2: Fall back to get_sports_context_for_query ─────────
                try:
                    from aria_code.clients.football_data_client import get_sports_context_for_query
                    _sports_ctx = get_sports_context_for_query(full_args)
                except Exception:
                    _sports_ctx = ""
                if _sports_ctx:
                    _has_quant = "量化预测" in _sports_ctx
                    _title = "⚽ 赛事预测" if _has_quant else "⚽ 赛事数据"
                    self.context.console.print(Panel(
                        _sports_ctx,
                        title=f"[bold]{_title}[/bold]",
                        border_style="cyan" if _has_quant else "blue",
                    ))
                else:
                    self.context.console.print(
                        "[yellow]⚽ 未能解析队名。[/yellow]\n"
                        "支持格式：\n"
                        "  [cyan]/football predict 葡萄牙 vs 刚果 wc[/cyan]\n"
                        "  [cyan]/football 葡萄牙和刚果比赛[/cyan]  （自动识别）"
                    )
            else:
                self.context.console.print(f"[red]未知子命令: {sub}[/red]  使用 /football 查看帮助")

    async def _football_predict(self, home: str, away: str, league: str):
        """Run football match prediction with LLM analysis."""
        from rich.panel import Panel
        from rich.table import Table
        from rich import box as rich_box
        import types

        self.context.console.print(f"[#57606a]⚽ 分析 {home} vs {away} ({league.upper()})…[/#57606a]")

        # WC / national team prediction path
        _wc_leagues = {"wc", "worldcup", "世界杯", "world_cup", "ca", "ec", "afc"}
        _is_wc = league.lower().replace(" ", "") in _wc_leagues

        if _is_wc:
            try:
                from aria_code.clients.football_data_client import (
                    predict_wc_match,
                    _find_fifa_rating,
                    team_display_name,
                    football_prediction_quality,
                    football_quality_missing_labels,
                )
                raw = predict_wc_match(home, away, neutral_venue=True)
                _h_cn = team_display_name(raw.get("home_name_cn", home), "zh")
                _a_cn = team_display_name(raw.get("away_name_cn", away), "zh")
                # Build strength facts for display
                _h_rank = raw.get("home_ranking", "?")
                _a_rank = raw.get("away_ranking", "?")
                _h_elo  = raw.get("home_elo")
                _a_elo  = raw.get("away_elo")
                _h_atk  = raw.get("home_attack")
                _a_atk  = raw.get("away_attack")
                _h_def  = raw.get("home_defense")
                _a_def  = raw.get("away_defense")
                _h_form = raw.get("home_form", "")
                _a_form = raw.get("away_form", "")
                _cal    = raw.get("calibrated_matches", 0)

                def _rank_label(value):
                    return f"#{value}" if value not in (None, "", "?") else "排名缺失"

                _strength_facts = [
                    f"FIFA排名: {_h_cn} {_rank_label(_h_rank)} · {_a_cn} {_rank_label(_a_rank)}",
                ]
                if _h_elo and _a_elo:
                    _strength_facts.append(f"Elo评分: {_h_cn} {_h_elo:.0f} · {_a_cn} {_a_elo:.0f}")
                if _h_atk is not None:
                    _atk_h = f"{_h_atk:.2f}" if isinstance(_h_atk, float) else str(_h_atk)
                    _atk_a = f"{_a_atk:.2f}" if isinstance(_a_atk, float) else str(_a_atk)
                    _def_h = f"{_h_def:.2f}" if isinstance(_h_def, float) else str(_h_def)
                    _def_a = f"{_a_def:.2f}" if isinstance(_a_def, float) else str(_a_def)
                    _strength_facts.append(f"进攻强度: {_h_cn} {_atk_h} · {_a_cn} {_atk_a}")
                    _strength_facts.append(f"防守强度: {_h_cn} {_def_h} · {_a_cn} {_def_a}")
                _strength_facts.append(
                    f"数据基础: {_cal} 场已完赛 WC 数据校准" if _cal > 0
                    else "数据基础: FIFA排名 + Poisson引擎估算"
                )
                _quality = raw.get("data_quality") or football_prediction_quality(raw)
                if _quality.get("missing"):
                    _missing_labels = football_quality_missing_labels(_quality["missing"], "zh")
                    _strength_facts.append(
                        f"数据质量: {_quality.get('status', 'estimated')} · 缺失/估算 {', '.join(_missing_labels)}"
                    )

                pred = types.SimpleNamespace(
                    home_win          = raw["home_win"],
                    draw              = raw["draw"],
                    away_win          = raw["away_win"],
                    btts              = raw["btts"],
                    lambda_home       = raw["lambda_home"],
                    lambda_away       = raw["lambda_away"],
                    most_likely       = raw["top_scorelines"][0]["score"] if raw["top_scorelines"] else "1-0",
                    top_scores        = [{"score": s["score"], "prob": s["prob"]} for s in raw["top_scorelines"]],
                    implied_odds      = raw["implied_odds"],
                    key_factors       = _strength_facts,
                    home_form         = _h_form,
                    away_form         = _a_form,
                    home_elo          = _h_elo,
                    away_elo          = _a_elo,
                    data_quality       = _quality,
                    analysis          = "",
                    verdict           = (
                        f"[green]预测: {_h_cn} 获胜 ({raw['home_win']:.0%})[/green]" if raw["home_win"] > raw["away_win"] + 0.05
                        else f"[green]预测: {_a_cn} 获胜 ({raw['away_win']:.0%})[/green]" if raw["away_win"] > raw["home_win"] + 0.05
                        else f"[yellow]预测: 双方势均力敌，平局概率 {raw['draw']:.0%}[/yellow]"
                    ),
                    ht_lambda_home    = raw.get("ht_lambda_home", 0),
                    ht_lambda_away    = raw.get("ht_lambda_away", 0),
                    st_lambda_home    = raw.get("st_lambda_home", 0),
                    st_lambda_away    = raw.get("st_lambda_away", 0),
                    ht_home_win       = raw.get("ht_home_win", 0),
                    ht_draw           = raw.get("ht_draw", 0),
                    ht_away_win       = raw.get("ht_away_win", 0),
                    ht_top_scorelines = raw.get("ht_top_scorelines", []),
                    home_name_cn      = _h_cn,
                    away_name_cn      = _a_cn,
                )
            except Exception as exc:
                self.context.console.print(f"[red]WC 预测失败: {exc}[/red]")
                return
        else:
            try:
                from agents.sports.football_agent import FootballAgent

                agent = FootballAgent(llm_call=None)

                import asyncio
                pred = await agent.predict(home, away, league, with_llm=False)

                # Try LLM enhancement
                if hasattr(self, 'terminal') and self.terminal:
                    try:
                        _score_candidates = "、".join(
                            f"{s['score']}({s['prob']}%)"
                            for s in getattr(pred, "top_scores", [])[:5]
                        )
                        llm_prompt = (
                            f"你是专业足球分析师。简洁分析这场比赛（中文，不超过150字）:\n"
                            f"{home} vs {away}\n"
                            f"主队胜: {pred.home_win:.0%}  平: {pred.draw:.0%}  客队胜: {pred.away_win:.0%}\n"
                            f"预期进球: {pred.lambda_home:.1f} - {pred.lambda_away:.1f}\n"
                            f"最可能比分: {pred.most_likely}\n"
                            f"候选比分（按概率降序）: {_score_candidates}\n"
                            f"关键因素: {'; '.join(pred.key_factors)}\n"
                            "规则: 只引用上方概率和候选比分，不要编造射正率、历史交锋、近5场客场等具体数据。"
                        )
                        analysis_text = await asyncio.wait_for(
                            self.terminal._query_llm_async(llm_prompt),
                            timeout=30
                        )
                        if analysis_text:
                            pred.analysis = analysis_text
                    except Exception:
                        pass

            except Exception as exc:
                self.context.console.print(f"[red]预测失败: {exc}[/red]")
                return

        # ── 确定性分析文字：基于 Poisson 数字生成，不调用 LLM ────────────────
        # 避免 gpt-oss:120b-cloud 忽略 enable_tools=False 并乱用工具
        if not getattr(pred, "analysis", ""):
            _h_n = getattr(pred, "home_name_cn", home)
            _a_n = getattr(pred, "away_name_cn", away)
            _hw  = pred.home_win
            _dw  = pred.draw
            _aw  = pred.away_win
            _lh  = pred.lambda_home
            _la  = pred.lambda_away
            _ml  = pred.most_likely
            # Determine favorite
            if _hw > _aw + 0.08:
                _tend = f"{_h_n} 胜算更大（{_hw:.0%}），为本场热门"
            elif _aw > _hw + 0.08:
                _tend = f"{_a_n} 胜算更大（{_aw:.0%}），为本场热门"
            else:
                _tend = f"双方势均力敌，{_h_n} 胜/平/负概率分别为 {_hw:.0%}/{_dw:.0%}/{_aw:.0%}"
            # Expected goals narrative
            _total = _lh + _la
            if _total < 2.0:
                _goal_desc = "预计进球偏少，防守型比赛"
            elif _total < 3.0:
                _goal_desc = "进球适中，攻防均衡"
            else:
                _goal_desc = "进球较多，进攻型对决"
            pred.analysis = (
                f"{_tend}。"
                f"Poisson 模型预期进球 {_lh:.1f}–{_la:.1f}（共 {_total:.1f}），"
                f"{_goal_desc}，最可能比分为 {_ml}。"
            )

        # ── display ──────────────────────────────────────────────────────────
        from rich.columns import Columns
        from rich.text import Text
        try:
            from aria_code.clients.football_data_client import team_display_name as _football_display_name
        except Exception:
            def _football_display_name(value, locale="zh"):
                return str(value or "-")

        # Probability bars
        _home_display = _football_display_name(getattr(pred, "home_name_cn", home), "zh")
        _away_display = _football_display_name(getattr(pred, "away_name_cn", away), "zh")

        def pct_bar(val: float, width: int = 12) -> str:
            filled = int(val * width)
            return "█" * filled + "░" * (width - filled)

        hw_color = "green" if pred.home_win > pred.away_win else "dim"
        aw_color = "green" if pred.away_win > pred.home_win else "dim"
        dw_color = "yellow" if pred.draw > 0.28 else "dim"

        prob_table = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 1))
        prob_table.add_column("", style="bold", width=16)
        prob_table.add_column("", width=14)
        prob_table.add_column("", width=6)
        prob_table.add_column("", width=8)

        prob_table.add_row(
            f"[{hw_color}]{_home_display}[/{hw_color}]",
            f"[{hw_color}]{pct_bar(pred.home_win)}[/{hw_color}]",
            f"[{hw_color}]{pred.home_win:.0%}[/{hw_color}]",
            f"[dim]赔率 {pred.implied_odds['home']}[/dim]",
        )
        prob_table.add_row(
            f"[{dw_color}]平局[/{dw_color}]",
            f"[{dw_color}]{pct_bar(pred.draw)}[/{dw_color}]",
            f"[{dw_color}]{pred.draw:.0%}[/{dw_color}]",
            f"[dim]赔率 {pred.implied_odds['draw']}[/dim]",
        )
        prob_table.add_row(
            f"[{aw_color}]{_away_display}[/{aw_color}]",
            f"[{aw_color}]{pct_bar(pred.away_win)}[/{aw_color}]",
            f"[{aw_color}]{pred.away_win:.0%}[/{aw_color}]",
            f"[dim]赔率 {pred.implied_odds['away']}[/dim]",
        )

        title = f"⚽ {_home_display} vs {_away_display}  [{league.upper()}]"
        self.context.console.print(Panel(prob_table, title=f"[bold #3fb950]{title}[/bold #3fb950]", border_style="#3fb950"))

        self.context.console.print(f"  [#57606a]预期进球: {_home_display} {pred.lambda_home:.2f} / {_away_display} {pred.lambda_away:.2f}"
                      f"  │  双方均进球: {pred.btts:.0%}[/#57606a]")

        # Top scorelines — show up to 8, colour-coded by outcome
        _h_name = _home_display
        _a_name = _away_display
        if getattr(pred, "top_scores", None):
            score_table = Table(box=rich_box.SIMPLE, show_header=True, padding=(0, 2))
            score_table.add_column("可能比分", style="bold", width=12, no_wrap=True)
            score_table.add_column("概率", justify="right", width=8, no_wrap=True)
            score_table.add_column("结果", width=16, no_wrap=True)
            for s in pred.top_scores[:8]:
                _sc = s["score"]
                _pr = s["prob"]
                try:
                    _hg, _ag = (_sc.split("-") + ["0"])[:2]
                    _hg, _ag = int(_hg.strip()), int(_ag.strip())
                except Exception:
                    _hg, _ag = 0, 0
                if _hg > _ag:
                    _label = f"[#3fb950]{_h_name}胜[/#3fb950]"
                    _sc_fmt = f"[green]{_sc}[/green]"
                elif _ag > _hg:
                    _label = f"[#f85149]{_a_name}胜[/#f85149]"
                    _sc_fmt = f"[red]{_sc}[/red]"
                else:
                    _label = "[yellow]平局[/yellow]"
                    _sc_fmt = f"[yellow]{_sc}[/yellow]"
                score_table.add_row(_sc_fmt, f"{_pr}%", _label)
            self.context.console.print(score_table)

        # Half-time / second-half breakdown
        if getattr(pred, "ht_lambda_home", 0) > 0:
            _h_lbl = getattr(pred, "home_name_cn", home)
            _h_lbl = _football_display_name(_h_lbl, "zh")
            _a_lbl = _football_display_name(getattr(pred, "away_name_cn", away), "zh")
            _ht_best = pred.ht_top_scorelines[0]["score"] if pred.ht_top_scorelines else "0-0"
            _ht_best_p = pred.ht_top_scorelines[0]["prob"] if pred.ht_top_scorelines else 0
            _st_best_lh = getattr(pred, "st_lambda_home", 0)
            _st_best_la = getattr(pred, "st_lambda_away", 0)
            self.context.console.print()
            self.context.console.print(
                f"  [bold]上半场[/bold]  预期进球 {_h_lbl} [cyan]{pred.ht_lambda_home:.2f}[/cyan] / "
                f"{_a_lbl} [cyan]{pred.ht_lambda_away:.2f}[/cyan]"
                f"  │  最可能: [bold]{_ht_best}[/bold] ({_ht_best_p}%)"
            )
            self.context.console.print(
                f"  [#57606a]上半场胜/平/负: {pred.ht_home_win:.0%} / {pred.ht_draw:.0%} / {pred.ht_away_win:.0%}[/#57606a]"
            )
            _ht_scores_str = "  ".join(
                f"[cyan]{s['score']}[/cyan] {s['prob']}%" for s in pred.ht_top_scorelines[:4]
            )
            if _ht_scores_str:
                self.context.console.print(f"  [#57606a]比分分布: {_ht_scores_str}[/#57606a]")
            self.context.console.print(
                f"  [bold]下半场[/bold]  预期进球 {_h_lbl} [green]{_st_best_lh:.2f}[/green] / "
                f"{_a_lbl} [green]{_st_best_la:.2f}[/green]"
                f"  [#57606a](全场 − 上半场)[/#57606a]"
            )
            self.context.console.print()

        # ── 实力对比 & 近期表现 ───────────────────────────────────────────────
        _h_name_d = _home_display
        _a_name_d = _away_display
        _hform = getattr(pred, "home_form", "")
        _aform = getattr(pred, "away_form", "")

        if pred.key_factors:
            self.context.console.print("\n  [bold]实力对比[/bold]")
            for f_ in pred.key_factors:
                self.context.console.print(f"  [#57606a]  • {f_}[/#57606a]")

        # Form strings (W/D/L) from live API — only show when available
        if _hform and _hform not in ("?????", ""):
            def _form_colored(s: str) -> str:
                out = []
                for c in s.upper():
                    if c == "W": out.append("[green]W[/green]")
                    elif c == "D": out.append("[yellow]D[/yellow]")
                    elif c == "L": out.append("[red]L[/red]")
                    else: out.append(c)
                return "".join(out)
            self.context.console.print("\n  [bold]近期表现[/bold]")
            self.context.console.print(f"  {_h_name_d}  {_form_colored(_hform)}")
            if _aform and _aform not in ("?????", ""):
                self.context.console.print(f"  {_a_name_d}  {_form_colored(_aform)}")

        if pred.analysis:
            self.context.console.print(Panel(
                pred.analysis,
                title="[bold]量化分析[/bold]",
                border_style="#8c959f",
                padding=(0, 2),
            ))

        self.context.console.print(f"\n  [bold green]{pred.verdict}[/bold green]")
        self.context.console.print("  [#6e7781]提示：准确比分概率通常较分散，请按候选区间参考，不构成投注建议。[/#6e7781]\n")

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
