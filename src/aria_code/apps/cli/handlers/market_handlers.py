"""Stub module for legacy market_handlers — live market snapshots moved to financial-service MCP."""

from __future__ import annotations

from typing import Any

_TA_SESSION_CACHE: dict[str, Any] = {}
_LEVEL_HISTORY_CACHE: dict[str, Any] = {}


def _try_prefetch_market_data(message: str, history: list | None = None) -> str:
    return ""


def _fetch_snapshot_row_for_symbol(symbol: str, mdc: Any = None) -> dict[str, Any]:
    return {"symbol": symbol, "success": False, "error": "market_handlers_moved_to_financial_service"}


def _try_handle_multi_market_snapshot(message: str, symbols: list[str]) -> dict[str, Any]:
    return {"handled": False, "success": False, "error": "market_handlers_moved_to_financial_service"}


def _try_handle_market_snapshot_analysis(
    message: str,
    history: list | None = None,
    *,
    evidence_required: bool = False,
) -> dict[str, Any]:
    return {"handled": False, "success": False, "error": "market_handlers_moved_to_financial_service"}


def _try_handle_market_overview(message: str) -> dict[str, Any]:
    return {"handled": False, "success": False, "error": "market_handlers_moved_to_financial_service"}
