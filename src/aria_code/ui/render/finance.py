"""Compatibility stub for legacy terminal finance renderers.

Finance tools and vertical formatting have moved to ``artheras/financial-service``
over MCP. This module preserves the exported symbol names so existing imports
remain valid without carrying 1,700+ lines of vertical terminal renderers.
"""
from __future__ import annotations

import json
from typing import Any


def _emit(text: str, *, console: Any = None, has_rich: bool = True) -> None:
    if console is not None and has_rich:
        console.print(text)
    else:
        print(text)


def render_finance_result(
    tool_name: str,
    result: dict,
    *,
    console: Any = None,
    has_rich: bool = True,
    bot_mode: bool = False,
) -> None:
    """Minimal generic fallback printer for finance tool payloads."""
    if not isinstance(result, dict):
        _emit(str(result), console=console, has_rich=has_rich)
        return
    if not result.get("success", True):
        err = result.get("error") or f"{tool_name} failed"
        _emit(str(err), console=console, has_rich=has_rich)
        return
    summary = result.get("summary") or result.get("symbol") or tool_name
    _emit(
        f"[{tool_name}] {summary}" if isinstance(summary, str) else json.dumps(result, ensure_ascii=False)[:400],
        console=console,
        has_rich=has_rich,
    )


def _legacy_render(r: Any = None, *args: Any, console: Any = None, has_rich: bool = True, **kwargs: Any) -> None:
    if isinstance(r, dict) and not r.get("success", True) and r.get("error"):
        _emit(str(r["error"]), console=console, has_rich=has_rich)
    elif isinstance(r, dict):
        _emit(json.dumps(r, ensure_ascii=False)[:400], console=console, has_rich=has_rich)


render_macro_result = _legacy_render
render_cb_rates = _legacy_render
render_econ_calendar = _legacy_render
render_options_chain = _legacy_render
render_quality_scores = _legacy_render
render_ichimoku = _legacy_render
render_fear_greed = _legacy_render
render_funding_rates = _legacy_render
render_peer_comparison = _legacy_render
render_house_price = _legacy_render
render_reits_list = _legacy_render
render_rental_yield = _legacy_render
render_property_val = _legacy_render
render_multi_city = _legacy_render
render_asset_score = _legacy_render
render_corr_matrix = _legacy_render
render_portfolio_bt = _legacy_render
render_sql_result = _legacy_render
render_alerts = _legacy_render
