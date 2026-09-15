"""CLI bootstrap utilities shared by legacy and packaged entrypoints."""

from __future__ import annotations

import os
import socket
import urllib.parse
from pathlib import Path

from aria_code.apps.cli.config_paths import resolve_paths


def load_aria_env(env_file: Path | None = None) -> None:
    """Load persisted CLI environment values without overriding the process."""
    target = env_file or (Path.home() / ".aria" / ".env")
    if not target.exists():
        return
    try:
        for line in target.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and value:
                os.environ.setdefault(key, value)
    except Exception:
        return


def disable_broken_proxy(timeout: float = 1.5) -> None:
    """Unset proxy variables when the configured proxy cannot be reached."""
    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    if not proxy:
        return
    try:
        parsed = urllib.parse.urlparse(proxy if "://" in proxy else f"http://{proxy}")
        host = parsed.hostname
        port = parsed.port or 80
        if not host:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        alive = sock.connect_ex((host, port)) == 0
        sock.close()
    except Exception:
        alive = False
    if not alive:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            os.environ.pop(name, None)


def use_system_trust_store() -> bool:
    """Verify TLS against the OS trust store instead of certifi's bundle.

    Corporate and local TLS-intercepting proxies present their own CA. That CA
    is installed in the system keychain — which is why ``curl`` works on such a
    machine — but Python verifies against certifi, which has never heard of it,
    so every HTTPS call fails with CERTIFICATE_VERIFY_FAILED.

    This was not hypothetical: on a machine behind an intercepting proxy,
    ``oauth2.googleapis.com`` failed 5 times out of 5 from Python and returned
    404 from curl, so roughly a third of Vertex turns died fetching an OAuth
    token. Trusting the OS store fixed it without disabling verification —
    which is the tempting wrong fix, and would accept any certificate at all.

    Returns whether the injection happened, and never raises: an environment
    without ``truststore`` keeps certifi's behaviour exactly as before.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
        return True
    except Exception:
        return False


def initialize_cli_environment() -> None:
    os.environ.setdefault("TQDM_DISABLE", "1")
    load_aria_env()
    disable_broken_proxy()
    use_system_trust_store()


# The model used when nothing has been configured yet.
#
# This exists so there is ONE place that answers "which model by default".
# Call sites used to spell their own fallback — `config.get("model",
# "qwen2.5:7b")`, ten times over — which quietly made Ollama the answer
# whenever a config key was missing, no matter what the user had chosen.
# Provider configuration is the user's to make (that is what `/model` and
# `.ariarc` are for); the code's job is to have a sane default and then get
# out of the way.
DEFAULT_MODEL = os.getenv("ARIA_DEFAULT_MODEL", "google/gemini-3.8-flash")


def default_config() -> dict:
    return {
        "api_url": os.getenv(
            "ARTHERA_API_URL",
            "http://localhost:8000",
        ),
        "local_url": "http://localhost:8000",
        "ollama_url": os.getenv("OLLAMA_URL", "http://localhost:11434"),
        # Which backend serves a bare, unprefixed model name. Only consulted
        # for names that do not name their own provider ("qwen2.5:7b"); a
        # prefixed id like "google/gemini-2.5-pro" always wins.
        "local_provider": os.getenv("ARIA_LLM_PROVIDER", "ollama"),
        "provider_fallback": "configured",
        "model": DEFAULT_MODEL,
        "thinking_mode": "auto",
        "watchlist": ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"],
        "auth_token": None,
        "user_id": None,
        "last_session_id": None,
        "auto_save_sessions": True,
        "auto_compact_context": True,
        "auto_compact_threshold": 0.78,
        "command_policy": "safe",
        "permission_mode": "workspace-write",
        "network_enabled": True,
        "data_sharing": False,
        "feedback_upload": False,
        "write_policy": "desktop_only",
        "lsp_autocheck": False,
        "input_style": "panel",
        "input_theme": "auto",
        "response_footer": "compact",
        "report_agent_timeout": 40.0,
        "report_synthesis_timeout": 20.0,
        "local_mode": False,
        "conversation_history": [],
        "ui_lang": "",
    }


def runtime_paths():
    return resolve_paths()
