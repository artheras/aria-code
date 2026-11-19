"""AnalysisCommandsMixin — market analysis and indicator commands."""

from __future__ import annotations

from aria_code.apps.cli.i18n import ui_text

from aria_code.apps.cli.commands.market import parse_analysis_args
from aria_code.apps.cli.commands.market_context import context_has_price


import logging
from typing import Any, Dict, Optional

def _get__HAS_BROKERS():
    from .._optional import HAS_BROKERS as val
    return val
def build_analyze_context(*args, **kwargs):
    from aria_cli import build_analyze_context as fn
    return fn(*args, **kwargs)
def _get_broker_registry(*args, **kwargs):
    from .._optional import get_registry as fn
    return fn(*args, **kwargs)
def build_analyze_prompt(*args, **kwargs):
    from aria_cli import build_analyze_prompt as fn
    return fn(*args, **kwargs)
def _is_ashare_symbol(*args, **kwargs):
    from aria_code.apps.cli.helpers import _is_ashare_symbol as fn
    return fn(*args, **kwargs)
def _get__HAS_MDC():
    from .._optional import HAS_MDC as val
    return val
def _ashare_code_to_name(*args, **kwargs):
    from aria_cli import _ashare_code_to_name as fn
    return fn(*args, **kwargs)
def _get__HAS_LOCAL_FINANCE():
    from aria_cli import _HAS_LOCAL_FINANCE as val
    return val
def _get_mdc(*args, **kwargs):
    from .._optional import get_mdc as fn
    return fn(*args, **kwargs)

# A real logger. This was a `def logger(*args, **kwargs)` forwarding to
# aria_cli, so every `logger.debug(...)` raised AttributeError — and all but
# one of them sit in an except block, where the failure replaced whatever
# error was being reported.
logger = logging.getLogger(__name__)


class AnalysisCommandsMixin:
    """Mixin: market analysis and technical indicator commands."""

    async def cmd_analyze(self, args: str):
        """Deep analysis: fetch real quote + TA + fundamentals, then ask LLM."""
        parsed = parse_analysis_args(args)
        symbol = parsed.symbol
        is_cn = _is_ashare_symbol(symbol)
        response_lang = parsed.lang or ("zh" if is_cn else "en")

        if self.context.has_rich:
            with self.context.console.status(f"[dim]{ui_text(self, f'正在获取 {symbol} 数据', f'Fetching {symbol} data')}...[/dim]", spinner="dots"):
                ctx = await self._build_analyze_context(symbol, is_cn)
        else:
            print(f"Fetching data for {symbol}...")
            ctx = await self._build_analyze_context(symbol, is_cn)

        if parsed.focus == "volume":
            if response_lang == "en":
                ctx += (
                    "\n\n### User Focus\n"
                    "- Focus on volume, price-volume confirmation, and volume versus recent average. "
                    "If volume is unavailable, state that the source did not return it and do not invent values."
                )
            else:
                ctx += (
                    "\n\n### 用户关注\n"
                    "- 重点分析成交量、量价关系、成交量相对近期均量的变化；"
                    "若成交量数据不可用，请直接说明来源未返回成交量，不要编造数值。"
                )
        elif parsed.focus:
            heading = "User Focus" if response_lang == "en" else "用户关注"
            label = "Focus on" if response_lang == "en" else "重点分析"
            ctx += f"\n\n### {heading}\n- {label}: {parsed.focus}"

        # The prompt carries the fetched data, so the evidence gate must not
        # refuse the answer for want of a tool call: "analyze Apple stock"
        # lands here, and on the backend route there are no tools to call.
        await self.terminal.send_message(build_analyze_prompt(symbol, ctx, is_cn, response_lang=response_lang),
                                         evidence_grounded=context_has_price(ctx))
        try:
            _resp = next((m["content"] for m in reversed(self.terminal.conversation)
                          if m.get("role") == "assistant" and m.get("content")), "")
            if _resp:
                self.terminal._record_prediction(symbol, _resp)
        except Exception:
            pass

    async def _build_analyze_context(self, symbol: str, is_cn: bool) -> str:
        """Fetch real market data and return a structured context string for the LLM."""
        return await build_analyze_context(
            symbol,
            is_cn,
            has_mdc=_get__HAS_MDC(),
            get_mdc=_get_mdc if _get__HAS_MDC() else None,
            ashare_name_lookup=_ashare_code_to_name,
            has_brokers=_get__HAS_BROKERS(),
            get_broker_registry=_get_broker_registry if _get__HAS_BROKERS() else None,
            logger=logger,
        )

    async def cmd_macro(self, args: str):
        """/macro [us|cn|rates|calendar] [indicator]  — 宏观经济数据（已迁移至 Arthera 后端服务）"""
        if self.context.has_rich:
            self.context.console.print("[dim]/macro 已迁移至 Arthera 后端服务，请通过后端 API 访问。[/dim]")
        return

    async def cmd_options(self, args: str):
        """/options（已迁移至 Arthera 后端服务）."""
        if self.context.has_rich:
            self.context.console.print("[dim]/options 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_quality(self, args: str):
        """/quality（已迁移至 Arthera 后端服务）."""
        if self.context.has_rich:
            self.context.console.print("[dim]/quality 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_ichimoku(self, args: str):
        """/ichimoku（已迁移至 Arthera 后端服务）."""
        if self.context.has_rich:
            self.context.console.print("[dim]/ichimoku 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_fear_greed(self, args: str):
        """/feargreed（已迁移至 Arthera 后端服务）."""
        if self.context.has_rich:
            self.context.console.print("[dim]/feargreed 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_funding(self, args: str):
        """/funding（已迁移至 Arthera 后端服务）."""
        if self.context.has_rich:
            self.context.console.print("[dim]/funding 已迁移至 Arthera 后端服务。[/dim]")
