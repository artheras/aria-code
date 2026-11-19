"""Stub module for legacy broker_handlers — broker queries moved to financial-service MCP."""

from __future__ import annotations

from typing import Any


def handle_broker_query(message: str, **kwargs: Any) -> dict[str, Any]:
    return {"handled": False, "success": False, "error": "broker_handlers_moved_to_financial_service"}
