"""CLI bootstrap utilities shared by legacy and packaged entrypoints."""

from __future__ import annotations

import os
import socket
import ssl
import sys
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


_LOOPBACK = ("localhost", "127.0.0.1", "::1")


def ensure_loopback_bypasses_proxy() -> None:
    """With a proxy set, keep local services (Ollama, LM Studio, the API) off it.

    HTTP clients honour HTTP(S)_PROXY, so a machine with a system proxy and no
    NO_PROXY would send requests for localhost:11434 to the proxy and fail.
    """
    if not any(os.environ.get(name) for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                                                 "ALL_PROXY", "all_proxy")):
        return
    current = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    entries = [item.strip() for item in current.split(",") if item.strip()]
    missing = [host for host in _LOOPBACK if host not in entries]
    if missing:
        value = ",".join(entries + missing)
        os.environ["NO_PROXY"] = value
        os.environ["no_proxy"] = value


def prepare_network() -> None:
    """Proxy settings every entry point needs before its first request."""
    disable_broken_proxy()
    ensure_loopback_bypasses_proxy()


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


def use_macos_ca_bundle() -> bool:
    """Use macOS's verified CA bundle when Python has no default CA file.

    Some standalone Python runtimes have no configured OpenSSL CA path, while
    curl and the system browser can verify the same HTTPS service. This keeps
    verification enabled and respects an explicit SSL_CERT_FILE override.
    """
    if sys.platform != "darwin" or os.environ.get("SSL_CERT_FILE"):
        return False
    if ssl.get_default_verify_paths().cafile:
        return False
    bundle = Path("/etc/ssl/cert.pem")
    if not bundle.is_file():
        return False
    os.environ["SSL_CERT_FILE"] = str(bundle)
    return True


def initialize_cli_environment() -> None:
    os.environ.setdefault("TQDM_DISABLE", "1")
    load_aria_env()
    prepare_network()
    if not use_system_trust_store():
        use_macos_ca_bundle()


# The model used when nothing has been configured yet.
#
# This exists so there is ONE place that answers "which model by default".
# Call sites used to spell their own fallback — `config.get("model",
# "qwen2.5:7b")`, ten times over — which quietly made Ollama the answer
# whenever a config key was missing, no matter what the user had chosen.
# Provider configuration is the user's to make (that is what `/model` and
# `.ariarc` are for); the code's job is to have a sane default and then get
# out of the way.
DEFAULT_MODEL = os.getenv("ARIA_DEFAULT_MODEL", "google/gemini-3.5-flash")
# A stable model (Google: available until at least May 2027), not a short-term
# one: an install that is rarely updated should not lose its default within
# months. gemini-2.5-pro, the previous default, retires in October 2026.


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
        # prefixed id like "google/gemini-3.5-flash" always wins.
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
        # Approval by risk level (safety/risk.py): "manual" asks as before;
        # "risk" runs actions at or below auto_approve_level (0-2) without a
        # prompt. L4 always asks, even after "always allow".
        "approval_mode": "manual",
        "auto_approve_level": 1,
        # Independent review of a turn's changes by a fresh-context reviewer
        # before it ends (runtime/review.py). Costs one extra model call.
        "review_gate": False,
        # The runtime's DONE/INCOMPLETE report under coding answers.
        "delivery_report": True,
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
