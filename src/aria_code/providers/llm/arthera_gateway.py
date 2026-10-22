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

DEFAULT_API_URL = "https://api.arthera.xyz"
DEFAULT_MODEL = "google/gemini-3.8-flash"


def _config_path() -> Path:
    """Where /login saved the session: the same file, by the same rule.

    This used to be ~/.arthera/config.json, or ARIA_CONFIG_DIR. /login writes
    through aria_home(), which is ~/.arthera only when that legacy folder
    already exists and ~/.aria-code otherwise, and is overridden by ARIA_HOME.
    So on a new install — the people signing in to reach a model without a
    key — the session was saved in one folder and looked for in another, and
    signing in never gave them a model. Asking aria_home() is what the 38
    files that once hardcoded ~/.arthera were moved over to for this reason.
    """
    from aria_code.packages.aria_core.paths import aria_home

    return aria_home() / "config.json"


def session_token() -> Optional[str]:
    """The Firebase token `/login` stored, or None when signed out."""
    try:
        config = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    from aria_code.account_session import account_id, get_account_token
    if account_id(config):
        return get_account_token(config)
    from aria_code.apps.cli.session_refresh import refresh_config_file
    return refresh_config_file(_config_path())


def gateway_base_url() -> str:
    """Where the gateway lives. Overridable for staging and self-hosting."""
    api = os.getenv("ARIA_API_URL", "").strip() or DEFAULT_API_URL
    from aria_code.service_endpoints import current_url
    return f"{current_url(api)}/api/v2/llm/v1"


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
