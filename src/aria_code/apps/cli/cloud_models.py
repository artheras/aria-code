"""The models each cloud provider offers, read from the provider itself.

`/model` used to list Aria's own catalogue and, for a cloud provider, only the
model already selected: a hand-kept list is out of date within weeks (Google
retired Gemini 3.5 Flash a month after this catalogue called it stable). So for
every provider with a key, the model list endpoint is asked what it serves now.

The answer is cached for a few hours. An expired cache is shown straight away
and refreshed behind it, so opening the picker never waits on a slow provider;
only a provider with no cache at all is waited for, and only for a few seconds.
Offline, or when a provider is down, the cache (however old) is used, and a
provider with neither simply has no section.

Keys are used for the request and never written to the cache.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional

CACHE_TTL_S = 6 * 3600
FETCH_TIMEOUT_S = 4.0
MAX_PER_PROVIDER = 60

#: provider -> (heading, kind, models URL). The URL for Google carries the key
#: as a header, not in the query string.
PROVIDERS: Dict[str, tuple] = {
    "google": ("Google Gemini", "google", "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200"),
    "openai": ("OpenAI", "openai", "https://api.openai.com/v1/models"),
    "anthropic": ("Anthropic Claude", "anthropic", "https://api.anthropic.com/v1/models?limit=100"),
    "deepseek": ("DeepSeek", "openai", "https://api.deepseek.com/models"),
    "groq": ("Groq", "openai", "https://api.groq.com/openai/v1/models"),
    "together": ("Together AI", "openai", "https://api.together.xyz/v1/models"),
    "dashscope": ("Alibaba DashScope", "openai", "https://dashscope.aliyuncs.com/compatible-mode/v1/models"),
    "siliconflow": ("SiliconFlow", "openai", "https://api.siliconflow.cn/v1/models"),
    "moonshot": ("Moonshot", "openai", "https://api.moonshot.cn/v1/models"),
    "zhipu": ("Zhipu GLM", "openai", "https://open.bigmodel.cn/api/paas/v4/models"),
}

#: Not chat models: embeddings, speech, images, video, music, moderation, live audio.
_NOT_CHAT = re.compile(
    r"embed|whisper|tts|transcribe|speech|audio|realtime|dall-e|image|imagen|veo|lyria|"
    r"omni-|nano-banana|moderation|rerank|guard|-live|robotics|computer-use|"
    r"search-preview|davinci|babbage|aqa|text-bison|vision-preview",
    re.IGNORECASE,
)

_refreshing: set = set()
_lock = threading.Lock()


def cache_dir() -> Path:
    from aria_code.packages.aria_core.paths import aria_home

    return Path(aria_home()) / "cloud_models"


def _http_json(url: str, headers: Dict[str, str], timeout: float) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "aria-code", "Accept": "application/json", **headers})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https endpoints above
        return json.load(response)


def _natural_key(text: str):
    """Sort key where 3.10 follows 3.9 and a higher version sorts later."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text.lower())]


def _ctx_label(tokens) -> str:
    try:
        n = int(tokens)
    except (TypeError, ValueError):
        return ""
    if n >= 900_000:
        return f"ctx {round(n / 1_048_576)}M"
    return f"ctx {n // 1000}K" if n >= 1000 else ""


def parse(provider: str, payload: object) -> List[dict]:
    """The chat models in a provider's list response, newest first: [{id, name, detail}]."""
    kind = PROVIDERS[provider][1]
    rows: List[dict] = []
    if kind == "google":
        for item in (payload or {}).get("models", []) if isinstance(payload, dict) else []:
            methods = item.get("supportedGenerationMethods") or []
            ident = str(item.get("name", "")).removeprefix("models/")
            if "generateContent" not in methods or not ident or _NOT_CHAT.search(ident):
                continue
            detail = " · ".join(x for x in (_ctx_label(item.get("inputTokenLimit")),) if x)
            rows.append({"id": ident, "name": item.get("displayName") or ident, "detail": detail})
    elif kind == "anthropic":
        for item in (payload or {}).get("data", []) if isinstance(payload, dict) else []:
            ident = str(item.get("id", ""))
            if ident:
                rows.append({"id": ident, "name": item.get("display_name") or ident, "detail": ""})
    else:
        items = payload.get("data", []) if isinstance(payload, dict) else payload
        for item in items if isinstance(items, list) else []:
            ident = str(item.get("id", "")) if isinstance(item, dict) else ""
            if ident and not _NOT_CHAT.search(ident):
                rows.append({"id": ident, "name": ident, "detail": ""})
    rows.sort(key=lambda r: _natural_key(r["id"]), reverse=True)
    return rows[:MAX_PER_PROVIDER]


def fetch(provider: str, key: str, timeout: float = FETCH_TIMEOUT_S) -> Optional[List[dict]]:
    """Ask the provider; None when it cannot be reached or refuses the key."""
    _heading, kind, url = PROVIDERS[provider]
    if kind == "google":
        headers = {"x-goog-api-key": key}
    elif kind == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        headers = {"Authorization": f"Bearer {key}"}
    try:
        return parse(provider, _http_json(url, headers, timeout))
    except (OSError, urllib.error.URLError, ValueError):
        return None


def _read_cache(provider: str) -> Optional[dict]:
    try:
        data = json.loads((cache_dir() / f"{provider}.json").read_text(encoding="utf-8"))
        if isinstance(data.get("models"), list):
            return data
    except (OSError, ValueError):
        pass
    return None


def _write_cache(provider: str, models: List[dict], now: float) -> None:
    try:
        directory = cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{provider}.json").write_text(json.dumps({"at": now, "models": models}), encoding="utf-8")
    except OSError:
        pass


def _refresh_later(provider: str, key: str) -> None:
    with _lock:
        if provider in _refreshing:
            return
        _refreshing.add(provider)

    def run() -> None:
        try:
            models = fetch(provider, key)
            if models:
                _write_cache(provider, models, time.time())
        finally:
            with _lock:
                _refreshing.discard(provider)

    threading.Thread(target=run, daemon=True, name=f"aria-models-{provider}").start()


def models_for(provider: str, key: str, *, offline: bool = False, now: Optional[float] = None) -> List[dict]:
    """The provider's models: fresh cache, else stale cache (refreshed behind), else a live request."""
    stamp = time.time() if now is None else now
    cached = _read_cache(provider)
    if cached and (offline or stamp - float(cached.get("at", 0)) < CACHE_TTL_S):
        return cached["models"]
    if cached:
        _refresh_later(provider, key)
        return cached["models"]
    if offline:
        return []
    models = fetch(provider, key)
    if models:
        _write_cache(provider, models, stamp)
    return models or []


def collect(key_for: Callable[[str], str], *, offline: Optional[bool] = None) -> Dict[str, List[dict]]:
    """Models for every provider that has a key, looked up in parallel: {provider: [models]}."""
    if offline is None:
        offline = os.environ.get("ARIA_OFFLINE", "").lower() in {"1", "true", "yes", "on"}
    keyed = {p: key for p in PROVIDERS if (key := key_for(p))}
    if not keyed:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(keyed))) as pool:
        futures = {p: pool.submit(models_for, p, key, offline=offline) for p, key in keyed.items()}
        out = {}
        for provider, future in futures.items():
            try:
                models = future.result(timeout=FETCH_TIMEOUT_S + 2)
            except Exception:
                models = []
            if models:
                out[provider] = models
    return out
