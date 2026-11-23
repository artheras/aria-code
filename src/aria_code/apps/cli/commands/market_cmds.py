"""
MarketCommandsMixin — Market commands: quote, realty, football, screen, news, screen_cn, limitup, north.

Extracted from aria_cli.py. Methods' __globals__ are rebound to aria_cli's namespace
by _rebind_mixin_globals() called at module load time.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


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


def _is_known_football_name(name: str) -> bool:
    """Football vertical moved to external service."""
    return False


def _is_probable_football_query(text: str, pair: Optional[Tuple[str, str]] = None) -> bool:
    """Football vertical moved to external service."""
    return False


def _parse_nl_team_pair(text: str) -> Optional[Tuple[str, str]]:
    """Football vertical moved to external service."""
    return None


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
