"""
local_llm_provider.py — Unified async streaming interface for local LLM backends.

Supported backends
------------------
  ollama    → http://localhost:11434  (Ollama /api/chat)
  lmstudio  → http://localhost:1234   (LM Studio /v1/chat/completions)
  vllm      → http://localhost:8000   (vLLM  /v1/chat/completions)
  llamacpp  → http://localhost:8080   (llama.cpp server /v1/chat/completions)
  jan       → http://localhost:1337   (Jan /v1/chat/completions)
  openai    → https://api.openai.com  (OpenAI-compatible proxy)

All non-Ollama backends speak the OpenAI /v1/chat/completions SSE format.

Usage::

    provider = LocalLLMProvider.from_config(config)
    async for event in provider.stream(messages, tools=schemas):
        if event["type"] == "token":
            print(event["text"], end="", flush=True)
        elif event["type"] == "tool_call":
            handle_tool(event["name"], event["arguments"])
        elif event["type"] == "done":
            break
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model resolution helpers
# ---------------------------------------------------------------------------

# Preferred model-name prefixes, highest priority first.
_PREFERRED_PREFIXES = [
    "aria-",            # native Aria models (future)
    "qwen2.5:",         # full qwen2.5 series
    "qwen2.5-coder:",   # coder variant (great for finance code tasks)
    "qwen2.5",          # any qwen2.5 variant without explicit tag
    "qwen",             # any qwen
    "deepseek",         # DeepSeek family
    "gpt-oss",          # locally-hosted GPT-compatible
    "llama",            # Meta Llama family
    "mistral",          # Mistral family
    "gemma",            # Google Gemma family
    "phi",              # Microsoft Phi
    "command",          # Cohere Command
]

_model_cache: Dict[str, str] = {}   # base_url → resolved model name
_cache_ts: Dict[str, float] = {}
_CACHE_TTL = 60.0                   # seconds


def resolve_model_sync(base_url: str, requested: str) -> str:
    """
    Synchronously resolve the best available Ollama model.

    Resolution order:
      1. Exact match with *requested*
      2. Same family prefix (e.g. "aria-sonata" → any model starting with "aria-")
      3. Priority-prefix list (_PREFERRED_PREFIXES)
      4. First model returned by /api/tags
      5. *requested* as-is (let Ollama surface the real error)
    """
    import time
    key = f"{base_url}::{requested}"
    now = time.time()
    if key in _model_cache and now - _cache_ts.get(key, 0) < _CACHE_TTL:
        return _model_cache[key]

    tags_url = base_url.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(tags_url, timeout=3) as r:
            data = json.loads(r.read())
        available: List[str] = [m["name"] for m in data.get("models", [])]
    except Exception:
        return requested  # Ollama unreachable — pass through

    if not available:
        return requested

    resolved = _pick_model(available, requested)
    _model_cache[key] = resolved
    _cache_ts[key] = now
    return resolved


async def resolve_model_async(base_url: str, requested: str) -> str:
    """Async variant of resolve_model_sync (uses aiohttp if available)."""
    import time
    key = f"{base_url}::{requested}"
    now = time.time()
    if key in _model_cache and now - _cache_ts.get(key, 0) < _CACHE_TTL:
        return _model_cache[key]

    tags_url = base_url.rstrip("/") + "/api/tags"
    try:
        if _HAS_AIOHTTP:
            async with aiohttp.ClientSession(trust_env=True) as s:
                async with s.get(tags_url, timeout=aiohttp.ClientTimeout(total=3)) as r:
                    data = await r.json()
        else:
            with urllib.request.urlopen(tags_url, timeout=3) as r:
                data = json.loads(r.read())
        available: List[str] = [m["name"] for m in data.get("models", [])]
    except Exception:
        return requested

    if not available:
        return requested

    resolved = _pick_model(available, requested)
    _model_cache[key] = resolved
    _cache_ts[key] = now
    return resolved


def _pick_model(available: List[str], requested: str) -> str:
    """Choose the best model from *available* given *requested*."""
    # 1. Exact match
    if requested in available:
        return requested

    # 2. Same family (strip tag, match prefix)
    family = requested.split(":")[0]
    hit = next((m for m in available if m.startswith(family)), None)
    if hit:
        return hit

    # 3. Priority-prefix list
    for prefix in _PREFERRED_PREFIXES:
        hit = next((m for m in available if m.startswith(prefix)), None)
        if hit:
            return hit

    # 4. First available
    return available[0]


def list_ollama_models(base_url: str) -> List[str]:
    """Return all model names from Ollama /api/tags (sync)."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=3) as r:
            return [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception:
        return []

try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

from aria_code.model_capability import (
    ModelCapability,
    get_model_capability,
    build_ollama_tool_payload,
    build_tool_system_prompt,
    parse_tool_calls_from_response,
)


# ---------------------------------------------------------------------------
# Backend definitions
# ---------------------------------------------------------------------------

BACKEND_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "ollama":      {"default_url": "http://localhost:11434",                     "api_path": "/api/chat",            "protocol": "ollama"},
    "lmstudio":    {"default_url": "http://localhost:1234",                      "api_path": "/v1/chat/completions", "protocol": "openai"},
    "vllm":        {"default_url": "http://localhost:8000",                      "api_path": "/v1/chat/completions", "protocol": "openai"},
    "llamacpp":    {"default_url": "http://localhost:8080",                      "api_path": "/v1/chat/completions", "protocol": "openai"},
    "jan":         {"default_url": "http://localhost:1337",                      "api_path": "/v1/chat/completions", "protocol": "openai"},
    "openai":      {"default_url": "https://api.openai.com",                     "api_path": "/v1/chat/completions", "protocol": "openai"},
    # Cloud providers (API key required)
    "deepseek":    {"default_url": "https://api.deepseek.com",                   "api_path": "/v1/chat/completions", "protocol": "openai"},
    "groq":        {"default_url": "https://api.groq.com/openai",                "api_path": "/v1/chat/completions", "protocol": "openai"},
    "anthropic":   {"default_url": "https://api.anthropic.com",                  "api_path": "/v1/messages",         "protocol": "anthropic"},
    "together":    {"default_url": "https://api.together.xyz",                   "api_path": "/v1/chat/completions", "protocol": "openai"},
    "siliconflow": {"default_url": "https://api.siliconflow.cn",                 "api_path": "/v1/chat/completions", "protocol": "openai"},
    "moonshot":    {"default_url": "https://api.moonshot.cn/v1",                 "api_path": "/chat/completions",    "protocol": "openai"},
    # Custom user-defined OpenAI-compatible endpoint
    "custom":      {"default_url": "",                                           "api_path": "/chat/completions",    "protocol": "openai"},
}

LOCAL_BACKENDS = {"ollama", "lmstudio", "vllm", "llamacpp", "jan", "custom"}


def openai_models_url(base_url: str) -> str:
    """Return the models endpoint for a versioned or unversioned base URL."""
    base = base_url.rstrip("/")
    if re.search(r"/v\d+(?:beta)?(?:/openai)?$", base):
        return f"{base}/models"
    return f"{base}/v1/models"


@dataclass
class LocalLLMProvider:
    backend: str = "ollama"          # one of BACKEND_DEFAULTS keys
    base_url: str = ""               # override; blank → use BACKEND_DEFAULTS default
    model: str = "qwen2.5-coder:7b"
    api_key: str = ""                # needed for openai-compatible remotes
    timeout: int = 300

    # Derived from model_capability on first use
    _capability: Optional[ModelCapability] = field(default=None, repr=False)

    # ── constructor ────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "LocalLLMProvider":
        """Build from aria-code config dict."""
        backend = config.get("local_provider", "ollama").lower()
        info    = BACKEND_DEFAULTS.get(backend, BACKEND_DEFAULTS["ollama"])
        requested_model = config.get("model", "qwen2.5-coder:1.5b")

        # Custom endpoint: user-specified base_url via /config set custom_endpoint=...
        if backend == "custom":
            url     = config.get("custom_endpoint", "") or info["default_url"]
            model   = config.get("custom_model", requested_model)
            api_key = config.get("local_api_key", os.getenv("LOCAL_LLM_API_KEY", ""))
            return cls(backend=backend, base_url=url, model=model, api_key=api_key)

        # Cloud provider backends: read API key from providers.json or env var
        _cloud_env_map = {
            "deepseek":    "DEEPSEEK_API_KEY",
            "openai":      "OPENAI_API_KEY",
            "anthropic":   "ANTHROPIC_API_KEY",
            "groq":        "GROQ_API_KEY",
            "together":    "TOGETHER_API_KEY",
            "siliconflow": "SILICONFLOW_API_KEY",
            "moonshot":    "MOONSHOT_API_KEY",
        }
        if backend in _cloud_env_map:
            env_var = _cloud_env_map[backend]
            api_key = (os.getenv(env_var, "")
                       or config.get("local_api_key", "")
                       or os.getenv("LOCAL_LLM_API_KEY", ""))
            url = config.get("custom_endpoint") or info["default_url"]
            return cls(backend=backend, base_url=url, model=requested_model, api_key=api_key)

        # ``local_url`` belongs to Aria's local application server and used to
        # accidentally route LM Studio/llama.cpp requests to port 8000.  LLM
        # endpoints have provider-specific keys, with ``llm_base_url`` as the
        # shared explicit override.
        provider_urls = config.get("provider_urls", {})
        if not isinstance(provider_urls, dict):
            provider_urls = {}
        if backend == "ollama":
            url = config.get("ollama_url") or info["default_url"]
        else:
            url = (
                config.get(f"{backend}_url")
                or provider_urls.get(backend)
                or config.get("llm_base_url")
                or info["default_url"]
            )
        api_key = config.get("local_api_key", os.getenv("LOCAL_LLM_API_KEY", ""))

        # Resolve model at construction time so later callers always get a
        # valid model name (Ollama-only: other backends manage their own catalog).
        if backend == "ollama":
            model = resolve_model_sync(url, requested_model)
        else:
            model = requested_model

        return cls(backend=backend, base_url=url, model=model, api_key=api_key)

    # ── capability ─────────────────────────────────────────────────────────

    @property
    def capability(self) -> ModelCapability:
        if self._capability is None:
            self._capability = get_model_capability(self.model)
        return self._capability

    # ── health check ───────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Quick synchronous check that the backend is reachable."""
        info    = BACKEND_DEFAULTS.get(self.backend, BACKEND_DEFAULTS["ollama"])
        url     = (self.base_url or info["default_url"]).rstrip("/")
        probe = (
            f"{url}/api/tags" if self.backend == "ollama"
            else openai_models_url(url)
        )
        try:
            with urllib.request.urlopen(probe, timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    def list_models(self) -> List[str]:
        """Return available model names from backend."""
        info = BACKEND_DEFAULTS.get(self.backend, BACKEND_DEFAULTS["ollama"])
        url  = (self.base_url or info["default_url"]).rstrip("/")
        try:
            if self.backend == "ollama":
                with urllib.request.urlopen(f"{url}/api/tags", timeout=3) as r:
                    data = json.loads(r.read())
                return [m["name"] for m in data.get("models", [])]
            else:
                req = urllib.request.Request(openai_models_url(url))
                if self.api_key:
                    req.add_header("Authorization", f"Bearer {self.api_key}")
                with urllib.request.urlopen(req, timeout=3) as r:
                    data = json.loads(r.read())
                return [m["id"] for m in data.get("data", [])]
        except Exception:
            return []

    # ── streaming core ─────────────────────────────────────────────────────

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict]] = None,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        cancel_event=None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Unified async generator.  Yields event dicts::

            {"type": "token",     "text": str}
            {"type": "thinking",  "text": str}        # for reasoning models
            {"type": "tool_call", "name": str, "arguments": dict, "id": str}
            {"type": "done",      "usage": dict, "stop_reason": str}
            {"type": "error",     "message": str}
        """
        if not _HAS_AIOHTTP:
            yield {"type": "error", "message": "aiohttp not installed: pip install aiohttp"}
            return

        info     = BACKEND_DEFAULTS.get(self.backend, BACKEND_DEFAULTS["ollama"])
        url      = (self.base_url or info["default_url"]).rstrip("/") + info["api_path"]
        protocol = info["protocol"]
        cap      = self.capability

        temp  = temperature if temperature is not None else cap.temperature
        # Gemini 2.5 counts its thinking against the output cap, so 4096 left a
        # long answer — a structured review, a whole file — cut off mid-JSON.
        # Gemini allows 65k; other backends keep 4096 (DeepSeek, for one,
        # rejects anything above 8192).
        mtoks = max_tokens if max_tokens is not None else (32768 if "gemini" in self.model.lower() else 4096)

        # Inject tool system prompt for text-only models
        if tools and not cap.tool_calls:
            tool_sys = build_tool_system_prompt(tools, self.model)
            if tool_sys:
                sys_msgs = [m for m in messages if m.get("role") == "system"]
                if sys_msgs:
                    messages[0]["content"] = messages[0]["content"] + tool_sys
                else:
                    messages = [{"role": "system", "content": tool_sys.strip()}] + messages

        if protocol == "ollama":
            async for ev in self._stream_ollama(url, messages, tools, temp, mtoks, cap, cancel_event):
                yield ev
        else:
            async for ev in self._stream_openai(url, messages, tools, temp, mtoks, cap, cancel_event):
                yield ev

    # ── Ollama protocol ────────────────────────────────────────────────────

    async def _stream_ollama(self, url, messages, tools, temp, max_tokens, cap, cancel_event):
        # Re-resolve the model async before every call — handles the case where
        # the model was changed or wasn't available at construction time.
        base_url = url.rsplit("/api/chat", 1)[0]
        model = await resolve_model_async(base_url, self.model)
        if model != self.model:
            self.model = model   # keep in sync for future calls
            self._capability = None  # reset capability cache for new model

        native_tools = build_ollama_tool_payload(tools or [], self.model) if tools else None
        payload: Dict[str, Any] = {
            "model":    self.model,
            "messages": messages,
            "stream":   True,
            "options": {
                "num_ctx":       cap.context_window,
                "temperature":   temp,
                "top_p":         cap.top_p,
                "repeat_penalty": 1.15,
                "repeat_last_n":  128,
                "num_predict":    max_tokens,
            },
        }
        if native_tools:
            payload["tools"] = native_tools

        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        full_text = ""

        try:
            async with aiohttp.ClientSession(trust_env=True) as sess:
                async with sess.post(url, json=payload,
                                     timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        yield {"type": "error", "message": f"Ollama HTTP {resp.status}: {body[:200]}"}
                        return

                    async for line in resp.content:
                        if cancel_event and cancel_event.is_set():
                            yield {"type": "done", "usage": usage, "stop_reason": "cancelled"}
                            return

                        text = line.decode("utf-8", errors="ignore").strip()
                        if not text:
                            continue
                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError:
                            continue

                        msg = data.get("message", {})

                        # Native tool calls
                        if msg.get("tool_calls"):
                            for tc in msg["tool_calls"]:
                                fn   = tc.get("function", {})
                                name = fn.get("name", "")
                                args = fn.get("arguments", {})
                                if isinstance(args, str):
                                    try:
                                        args = json.loads(args)
                                    except Exception:
                                        args = {}
                                if name:
                                    yield {"type": "tool_call", "name": name,
                                           "arguments": args, "id": tc.get("id", "")}

                        if data.get("done"):
                            usage["prompt_tokens"]     += data.get("prompt_eval_count", 0)
                            usage["completion_tokens"] += data.get("eval_count", 0)
                            break

                        token = msg.get("content", "")
                        if token:
                            full_text += token
                            # Suppress tokens that are part of <tool_call> tags
                            if not full_text.lstrip().startswith("<tool_call"):
                                yield {"type": "token", "text": token}

        except Exception as exc:
            yield {"type": "error", "message": f"Ollama stream error: {exc or type(exc).__name__}"}
            return

        # Fallback: parse text-based tool calls
        text_calls = parse_tool_calls_from_response(full_text, model_name=self.model)
        for tc in text_calls:
            yield {"type": "tool_call", "name": tc["tool"],
                   "arguments": tc["params"], "id": ""}

        yield {"type": "done", "usage": usage, "stop_reason": "stop"}

    # ── OpenAI-compatible protocol ─────────────────────────────────────────

    RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
    MAX_ATTEMPTS = 5

    async def _stream_openai(self, url, messages, tools, temp, max_tokens, cap, cancel_event):
        """One OpenAI-compatible turn, retried on rate limits and dropped connections.

        A 429 ("Resource exhausted" on Vertex AI) or a dropped connection
        ended the whole agent turn at once, though both usually clear in
        seconds. Retries back off exponentially (honouring Retry-After) and
        happen only before anything reached the caller, so no token or tool
        call is ever delivered twice.
        """
        import random

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            delivered = False
            retry: Optional[Dict[str, Any]] = None
            attempt_stream = self._stream_openai_once(url, messages, tools, temp, max_tokens, cap, cancel_event)
            try:
                async for event in attempt_stream:
                    if (event.get("type") == "error" and event.get("transient") and not delivered
                            and attempt < self.MAX_ATTEMPTS):
                        retry = event
                        break
                    if event.get("type") in ("token", "thinking", "tool_call"):
                        delivered = True
                    yield event
            finally:
                await attempt_stream.aclose()
            if retry is None or (cancel_event and cancel_event.is_set()):
                return
            delay = retry.get("retry_after") or min(30.0, 2.0 ** attempt) * (1 + random.random() * 0.25)
            logger.info("OpenAI-compatible request failed (%s); retry %d in %.1fs",
                        retry.get("message", "")[:80], attempt, delay)
            await asyncio.sleep(delay)

    async def _stream_openai_once(self, url, messages, tools, temp, max_tokens, cap, cancel_event):
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        from aria_code.providers.tool_messages import to_openai_tool_protocol

        from aria_code.providers.openai_params import sampling_params

        payload: Dict[str, Any] = {
            "model":       self.model,
            "messages":    to_openai_tool_protocol(messages),
            "stream":      True,
            **sampling_params(url, self.model, temp, max_tokens),
        }
        if tools and cap.tool_calls:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        full_text = ""
        tool_call_accumulator: Dict[int, Dict] = {}   # index → partial call

        try:
            async with aiohttp.ClientSession(trust_env=True, headers=headers) as sess:
                async with sess.post(url, json=payload,
                                     timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        try:
                            retry_after = float(resp.headers.get("Retry-After", "") or 0) or None
                        except ValueError:
                            retry_after = None
                        yield {"type": "error", "message": f"HTTP {resp.status}: {body[:200]}",
                               "transient": resp.status in self.RETRY_STATUS, "retry_after": retry_after}
                        return

                    async for line in resp.content:
                        if cancel_event and cancel_event.is_set():
                            yield {"type": "done", "usage": usage, "stop_reason": "cancelled"}
                            return

                        raw = line.decode("utf-8", errors="ignore").strip()
                        if not raw or raw == "data: [DONE]":
                            continue
                        if raw.startswith("data: "):
                            raw = raw[6:]
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        # Usage from final chunk
                        if data.get("usage"):
                            u = data["usage"]
                            usage["prompt_tokens"]     = u.get("prompt_tokens", 0)
                            usage["completion_tokens"] = u.get("completion_tokens", 0)

                        choices = data.get("choices", [])
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})

                        # Token
                        content = delta.get("content", "")
                        if content:
                            full_text += content
                            yield {"type": "token", "text": content}

                        # Tool calls (streamed fragments)
                        for tc_delta in (delta.get("tool_calls") or []):
                            idx  = tc_delta.get("index", 0)
                            if idx not in tool_call_accumulator:
                                tool_call_accumulator[idx] = {
                                    "id": "", "name": "", "arguments": ""}
                            acc = tool_call_accumulator[idx]
                            acc["id"]        += tc_delta.get("id", "")
                            fn = tc_delta.get("function", {})
                            acc["name"]      += fn.get("name", "")
                            acc["arguments"] += fn.get("arguments", "")

                        fin_reason = choices[0].get("finish_reason")
                        if fin_reason in ("stop", "tool_calls", "length"):
                            break

        except Exception as exc:
            transient = isinstance(exc, (aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError,
                                         aiohttp.ClientPayloadError, ConnectionResetError))
            yield {"type": "error", "message": f"OpenAI-compat stream error: {exc or type(exc).__name__}",
                   "transient": transient}
            return

        # Emit accumulated tool calls
        for acc in tool_call_accumulator.values():
            args_str = acc.get("arguments", "{}")
            try:
                args = json.loads(args_str)
            except Exception:
                args = {}
            if acc.get("name"):
                yield {"type": "tool_call", "name": acc["name"],
                       "arguments": args, "id": acc.get("id", "")}

        # Fallback text-based tool call parsing (for non-native models)
        if not tool_call_accumulator:
            text_calls = parse_tool_calls_from_response(full_text, model_name=self.model)
            for tc in text_calls:
                yield {"type": "tool_call", "name": tc["tool"],
                       "arguments": tc["params"], "id": ""}

        yield {"type": "done", "usage": usage, "stop_reason": "stop"}


# ---------------------------------------------------------------------------
# Quick availability probe (synchronous, for startup checks)
# ---------------------------------------------------------------------------

def probe_all_backends() -> Dict[str, bool]:
    """Return {backend: is_reachable} for all known backends."""
    results = {}
    for name, info in BACKEND_DEFAULTS.items():
        url   = info["default_url"]
        probe = f"{url}/api/tags" if name == "ollama" else f"{url}/v1/models"
        try:
            with urllib.request.urlopen(probe, timeout=1) as r:
                results[name] = r.status == 200
        except Exception:
            results[name] = False
    return results


def probe_local_backends() -> Dict[str, bool]:
    """Probe only desktop/local runtimes, never paid cloud APIs."""
    return {
        name: LocalLLMProvider(backend=name).is_available()
        for name in LOCAL_BACKENDS
        if name != "custom"
    }
