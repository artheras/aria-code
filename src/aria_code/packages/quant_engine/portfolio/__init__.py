"""Portfolio optimization engine."""

from .optimizer import PortfolioOptimizer

__all__ = ["PortfolioOptimizer"]

# Optional explicit backend modules never override this distribution.
from .._namespace import extend as _extend_namespace
_extend_namespace(__path__, "portfolio")
