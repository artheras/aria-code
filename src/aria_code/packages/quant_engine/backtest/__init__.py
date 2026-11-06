"""Backtest engine for evaluating AI trading signals."""

from .core import Order, Position, Portfolio
from .engine import BacktestEngine

__all__ = ["Order", "Position", "Portfolio", "BacktestEngine"]

# Optional explicit backend modules never override this distribution.
from .._namespace import extend as _extend_namespace
_extend_namespace(__path__, "backtest")
