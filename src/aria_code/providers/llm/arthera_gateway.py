"""Google models through the Arthera gateway, using the session you signed in with.

`/login` gave you an identity and nothing else: to reach a Google model you
still needed a Gemini API key from AI Studio, or gcloud and a billed Google
Cloud project of your own. That is setup this product exists to spare people,
and every layer of it is a place to get stuck — a missing dependency, gcloud
not on PATH, a suspended billing account.

The server holds a project and a service account with access to Vertex, and
exposes it in the OpenAI shape. So this provider is the ordinary
OpenAI-compatible one pointed at that endpoint, authorised with the Firebase
token `/login` already stored. There is nothing for the user to configure.

The token is read at call time rather than captured once, because it lasts an
hour and `/login` refreshes it in the same config file.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from .base import ProviderConfig
from .openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.arthera.finance"
DEFAULT_MODEL = "google/gemini-3.5-flash"


def _config_path() -> Path:
    return Path(os.getenv("ARIA_CONFIG_DIR", Path.home() / ".arthera")) / "config.json"


def session_token() -> Optional[str]:
    """The Firebase token `/login` stored, or None when signed out."""
    try:
        config = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = config.get("auth_token")
    return str(token) if token else None


def gateway_base_url() -> str:
    """Where the gateway lives. Overridable for staging and self-hosting."""
    api = os.getenv("ARIA_API_URL", "").strip() or DEFAULT_API_URL
    return f"{api.rstrip('/')}/api/v2/llm/v1"


class ArtheraGatewayProvider(OpenAICompatProvider):
    """Google models billed to the Arthera project, for a signed-in user."""

    provider_name = "arthera"
    DEFAULT_BASE_URL = ""          # resolved per instance; it is deployment state
    DEFAULT_MODEL = DEFAULT_MODEL

    def __init__(self, config: ProviderConfig):
        if not config.base_url:
            config.base_url = gateway_base_url()
        # The bearer is the session, not a key the user pasted. Reading it
        # here keeps a signed-out user from presenting an empty credential
        # and getting a 401 they cannot act on.
        if not config.api_key:
            config.api_key = session_token() or ""
        super().__init__(config)

    @staticmethod
    def available() -> bool:
        """True when there is a session to authorise calls with."""
        return bool(session_token())
