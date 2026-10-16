"""The server an install talks to, and the old default that stranded it on localhost.

``default_config()`` used to save ``api_url = http://localhost:8000``. That
looked like a default but was written into every user's config.json on first
run, where it became indistinguishable from a choice. Anyone who installed
from GitHub then had every backend call, and Google sign-in, aimed at a
developer backend on their own machine: a refused connection, or on a machine
that happened to run one, a page reading ``{"detail":"Not Found"}``. Fixing the
default in code cannot reach a config that already holds the old value, so the
old default is recognised here and replaced once, written back and announced,
the same way a retired model is (see model_retirement).

What counts as a choice and is left alone:

* ``ARTHERA_API_URL`` in the environment, which now also overrides a saved value
  (in memory only), so a developer with a real local backend sets it once;
* any ``api_url`` other than the exact old default, such as a staging server;
* ``api_url_pinned: true``, an escape hatch for someone who wants localhost.

``local_mode`` is not touched: choosing a local model is a different decision
from where the backend lives.
"""

from __future__ import annotations

import os

PRODUCTION_API_URL = "https://api.arthera.finance"
LEGACY_DEFAULT_API_URL = "http://localhost:8000"


def _norm(url: object) -> str:
    return str(url or "").strip().rstrip("/").lower()


def migrate_config(config: dict) -> list[tuple[str, str, str]]:
    """Move the old localhost default to production in place; return (key, old, new)."""
    env = os.getenv("ARTHERA_API_URL", "").strip()
    if env:
        # An explicit environment choice wins over the file for this run only;
        # nothing is rewritten, so unsetting the variable restores the file.
        config["api_url"] = env.rstrip("/")
        return []
    if config.get("api_url_pinned"):
        return []
    old = config.get("api_url")
    if _norm(old) != LEGACY_DEFAULT_API_URL:
        return []
    config["api_url"] = PRODUCTION_API_URL
    return [("api_url", str(old), PRODUCTION_API_URL)]


__all__ = ["LEGACY_DEFAULT_API_URL", "PRODUCTION_API_URL", "migrate_config"]
