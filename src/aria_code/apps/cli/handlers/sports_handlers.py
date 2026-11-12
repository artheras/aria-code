"""Sports context prefetch for the chat path.

The market equivalent already lives in handlers/market_handlers.py; this one
was still inline in aria_cli and borrowed from its globals by stream_ollama,
so the fallback path (SDK, daemon) raised NameError on any sports query.

Kept deliberately quiet: a prefetch is an optimisation, and a missing or
failing football client must degrade to "no extra context" rather than break
the turn.
"""

from __future__ import annotations


def try_prefetch_sports_data(message: str) -> str:
    """Live sports context for a query, or "" when unavailable."""
    try:
        from aria_code.clients.football_data_client import get_sports_context_for_query

        return get_sports_context_for_query(message) or ""
    except Exception:
        return ""
