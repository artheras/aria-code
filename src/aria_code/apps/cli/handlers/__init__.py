"""apps/cli/handlers — deterministic pre-LLM response handlers extracted from aria_cli.py."""
from .broker_handlers import handle_broker_query

__all__ = [
    "handle_broker_query",
]
