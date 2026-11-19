"""Compatibility stub for legacy local finance tools.

Financial tools and backtesting have moved to ``artheras/financial-service``
over MCP. General-purpose web search lives in :mod:`aria_code.tools.web_search`
and continues to register via :func:`register_local_finance_tools`.
"""

from __future__ import annotations

from typing import Any, Dict, List

from aria_code.tools.web_search import (
    WEB_SEARCH_SCHEMA,
    _resolve_search_key,
    _web_search,
    register_web_search_tool,
)

LOCAL_FINANCE_TOOL_REGISTRY: Dict[str, tuple[Any, str]] = {
    "web_search": (
        _web_search,
        "Web search via Brave Search API or DuckDuckGo fallback; set BRAVE_SEARCH_API_KEY for higher quota",
    ),
}

LOCAL_FINANCE_TOOL_SCHEMAS: List[Dict[str, Any]] = [WEB_SEARCH_SCHEMA]


def _get_predictions(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
    return {
        "success": False,
        "error": "Financial prediction tools moved to artheras/financial-service (arthera-finance MCP).",
    }


def register_local_finance_tools(
    tool_registry: Dict,
    schema_registry: List,
) -> int:
    """Register retained general tools (currently ``web_search``)."""
    return register_web_search_tool(tool_registry, schema_registry)
