"""Request parameters that differ between OpenAI's own API and the servers that imitate it.

OpenAI's newer models reject `max_tokens` ("use `max_completion_tokens`") and its
reasoning models (o-series, gpt-5 and later) accept only the default temperature.
Servers that merely speak the same protocol (DeepSeek, Groq, Ollama, vLLM, ...)
still expect `max_tokens` and take a temperature, so this changes nothing for them.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse

# o1, o3-mini, gpt-5, gpt-5.4-mini, gpt-6.1-sol ... : reasoning models that fix the temperature.
_FIXED_TEMPERATURE = re.compile(r"^(o\d|gpt-(?:[5-9]|\d{2,}))", re.IGNORECASE)


def is_openai_api(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host == "openai.com" or host.endswith(".openai.com")


def sampling_params(url: str, model: str, temperature: Optional[float], max_tokens: Optional[int]) -> Dict[str, Any]:
    """`temperature` and the output-length limit, under the names this server expects."""
    params: Dict[str, Any] = {}
    official = is_openai_api(url)
    if temperature is not None and not (official and _FIXED_TEMPERATURE.match(model or "")):
        params["temperature"] = temperature
    if max_tokens is not None:
        params["max_completion_tokens" if official else "max_tokens"] = max_tokens
    return params
