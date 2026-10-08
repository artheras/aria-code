"""Provider routing + fallback DECISIONS for the chat loop (pure, testable).

Extracted from ``aria_cli.send_message`` as the keystone for the documented
runtime next step ("route the whole CLI tool loop through run_agent"). The
*decision* of which provider a round uses, and whether to fall back, is pure
logic; pulling it out of the streaming machinery lets it be unit-tested and
reused as a ``provider_fn`` selector without touching the live REPL path.

Routing rules (mirrors send_message):
  • backend_chat                     → Aria SSE backend
  • local_provider=ollama            → Ollama (local or Ollama Cloud)
  • any other explicit provider      → configured local/API provider
"""

from __future__ import annotations

from typing import Callable, Optional


PROVIDER_ALIASES = {
    "lm-studio": "lmstudio",
    "llama.cpp": "llamacpp",
    "llama-cpp": "llamacpp",
    "claude": "anthropic",
    "chatgpt": "openai",
    "openai-chatgpt": "openai",
    "gemini": "google",
    "grok": "xai",
}


def normalize_provider_name(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    return PROVIDER_ALIASES.get(normalized, normalized)


def is_cloud_model(model: str) -> bool:
    """Cloud models are provider-prefixed, e.g. ``openai/gpt-4.5``, ``anthropic/…``."""
    return "/" in (model or "")


def is_ollama_model(model: str) -> bool:
    """Ollama models have no ``/`` (``gpt-oss:120b-cloud``, ``deepseek-r1:14b``)."""
    return "/" not in (model or "")


def force_backend(config: dict, api_url: Optional[str]) -> bool:
    """backend_chat=True routes ALL chat through the self-hosted backend (which
    proxies to its own Ollama + collects training data), requiring an api_url."""
    return bool(config.get("backend_chat")) and bool(api_url)


# Prefixes that genuinely name a provider.  Checked explicitly because a slash
# in a model id does not always mean a provider: Ollama serves namespaced
# community models such as "hf.co/someone/qwen", whose first segment is a host,
# not a backend to route to.
KNOWN_MODEL_PROVIDERS = frozenset({
    "ollama", "openai", "anthropic", "google", "xai", "deepseek", "groq",
    "together", "dashscope", "lmstudio", "siliconflow", "moonshot", "zhipu",
    "mistral", "cohere", "perplexity", "baidu", "ernie", "qianfan",
    "bytedance", "doubao", "ark", "minimax", "stepfun", "01ai", "yi",
    "vertexai", "vertex-ai", "google-genai",
})


def model_provider(model: str) -> str:
    """Provider named by the model id itself, or "" when it names none.

    Returns "" for bare Ollama names ("qwen2.5:7b") and for namespaced Ollama
    models whose prefix is not a known provider ("hf.co/someone/qwen").
    """
    if not is_cloud_model(model):
        return ""
    candidate = normalize_provider_name(model.split("/", 1)[0])
    return candidate if candidate in KNOWN_MODEL_PROVIDERS else ""


def first_round_route(model: str, config: dict, api_url: Optional[str]) -> str:
    """Return ``ollama`` | ``configured`` | ``cloud`` for the first round.

    A provider-qualified model id ("google/gemini-2.5-pro") names its own
    provider, and that wins over ``local_provider``.  It used to lose: the
    default config ships ``model="gemini-pro"`` together with
    ``local_provider="ollama"``, so every request for the flagship default
    model was routed to Ollama instead — the status line reading "ollama"
    beside a Gemini model name was reporting this honestly.

    ``local_provider`` still decides for bare Ollama-style names
    ("qwen2.5:7b", "gpt-oss:120b-cloud"), which is what it is there for.
    """
    if force_backend(config, api_url):
        return "cloud"
    provider = model_provider(model)
    if not provider:
        provider = normalize_provider_name(config.get("local_provider") or "") or "ollama"
    return "ollama" if provider == "ollama" else "configured"


def model_receives_local_tools(model: str, config: dict, api_url: Optional[str]) -> bool:
    """Legacy chat has no local tools; client execution requires an opt-in
    protocol acknowledgement. Direct inference carries the local schemas."""
    return first_round_route(model, config, api_url) != "cloud" or bool(config.get("backend_local_tools"))


def is_placeholder_response(
    response: str,
    token_count: int,
    stub_detector: Optional[Callable[[str], bool]] = None,
) -> bool:
    """Detect empty or explicitly recognized stubs, never infer from length.

    ``token_count`` remains in the signature for existing callers, but an SSE
    final event can contain a valid answer without any streamed token events.
    """
    resp = response or ""
    if not resp.strip():
        return True
    if stub_detector is not None and stub_detector(resp):
        return True
    # A short answer ("OK") or a long answer in one SSE final event is valid.
    # Token counts describe transport chunking, not answer quality.
    return False


def should_fallback(route: str, result: dict, *, is_placeholder: bool) -> bool:
    """Whether to run the local/cloud fallback chain after the primary round.

    Keyed on the *route* (not the model name), so a forced-backend round that
    genuinely succeeded does NOT fall back — which is the bug-free version of the
    old ``_should_fallback`` that keyed on ``is_ollama_model`` and could discard a
    good backend answer (causing a re-run / hang).
    """
    if route in ("skip", "configured"):
        return route == "skip"
    if not result.get("success") and not result.get("cancelled"):
        return True
    return is_placeholder
