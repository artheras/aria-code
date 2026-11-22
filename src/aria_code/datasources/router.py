"""Compatibility stub for legacy market data router.

Financial data sources have moved to ``artheras/financial-service`` over MCP.
This stub retains ``DataRouter``, ``get_router``, ``register_datasource``, and
``_SOURCE_REGISTRY`` so legacy callers degrade gracefully without local source
adapters.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Type

from .base import BaseDataSource, FundamentalsResult, HistoryResult, QuoteResult

logger = logging.getLogger(__name__)

_SOURCE_REGISTRY: Dict[str, Type[BaseDataSource]] = {}


def register_datasource(name: str, cls: Type[BaseDataSource]) -> None:
    """Register a custom datasource adapter."""
    _SOURCE_REGISTRY[name.lower()] = cls


class DataRouter:
    """Minimal empty router; live market data lives in artheras/financial-service."""

    def quote(self, symbol: str) -> Optional[QuoteResult]:
        return None

    def history(self, symbol: str, days: int = 120, interval: str = "1d") -> Optional[HistoryResult]:
        return None

    def fundamentals(self, symbol: str) -> Optional[FundamentalsResult]:
        return None

    def list_sources(self) -> List[Dict[str, Any]]:
        result = []
        for name, cls in _SOURCE_REGISTRY.items():
            src = cls()
            result.append({
                "name": name,
                "markets": cls.markets,
                "needs_key": cls.requires_key,
                "configured": src.is_configured(),
            })
        return result


_router: Optional[DataRouter] = None


def get_router() -> DataRouter:
    global _router
    if _router is None:
        _router = DataRouter()
    return _router
