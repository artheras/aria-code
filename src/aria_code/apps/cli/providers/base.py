"""LLM provider abstraction layer.

Defines the minimal Protocol that every provider must satisfy so the
agent loop can call any backend (Ollama, AriaSSE, DeepSeek, etc.)
without importing provider-specific code.

Usage
-----
Implement the protocol on any class or pass a coroutine that matches
``stream()``'s signature as a bare ``provider_fn`` callable.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional, Protocol, runtime_checkable


# ── Event types emitted by LLMProvider.stream() ──────────────────────────────

@dataclass(frozen=True)
class LLMToken:
    """A single text token from the model."""
    text: str


@dataclass(frozen=True)
class LLMThinking:
    """One thinking/reasoning token (extended-thinking models)."""
    content: str


@dataclass(frozen=True)
class LLMToolCall:
    """Model requested a tool call."""
    tool: str
    params: dict


@dataclass(frozen=True)
class LLMToolResult:
    """Provider reported a tool execution result summary."""
    tool: str
    summary: str


@dataclass(frozen=True)
class LLMStatus:
    """Provider emitted a streaming status update."""
    state: str
    message: str


@dataclass(frozen=True)
class LLMDone:
    """Stream finished. Carries the aggregated result."""
    response: str
    tool_calls_pending: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    provider: str = "unknown"
    success: bool = True
    cancelled: bool = False
    error: str = ""


# Union type for type-checkers
LLMEvent = LLMToken | LLMThinking | LLMToolCall | LLMToolResult | LLMStatus | LLMDone


def event_kind(event: object) -> str:
    """The event's type by name — use this, not isinstance, to dispatch events.

    These modules are importable under two roots: ``aria_code.apps.cli…`` and
    the bare ``apps.cli…`` that tests patch. Each root defines its own copy of
    every event class, and runtime_bridge builds providers from the bare root
    while the consumers checked against the packaged one. isinstance then
    failed for every event: a provider streamed "ready", the consumer saw
    nothing, and the turn ended as empty_response. That hit every chat turn
    through ConfiguredProvider's own events — OpenAI-compatible endpoints,
    LM Studio, the registry providers, and Gemini without ADC.
    """
    return type(event).__name__


def _resolve_ollama_stream():
    """Prefer the aria_cli rebound stream_ollama when available."""
    import sys

    # Console entry points import ``aria_cli``; direct execution
    # (``python aria_cli.py`` and the npm launcher) registers it as
    # ``__main__``. Both own the rebound function whose globals include the
    # CLI tool registry and cache helpers.
    #
    # ``aria_code.aria_cli`` is the same file reached through the package root
    # rather than as a bare top-level module — the import the SDK and daemon
    # use. Without it here they fell through to the raw extracted function,
    # whose globals lack the borrowed CLI helpers, and the first cache-eligible
    # turn died with ``NameError: name '_cache_key' is not defined``.
    for module_name in ("aria_cli", "aria_code.aria_cli", "__main__"):
        module = sys.modules.get(module_name)
        rebound = getattr(module, "stream_ollama", None) if module else None
        if callable(rebound):
            return rebound
    from aria_code.apps.cli.providers.llm.ollama_stream import stream_ollama

    return stream_ollama


async def _stream_callback_provider(invoke, *, done_provider: str) -> AsyncGenerator[LLMEvent, None]:
    """Convert a callback-based provider coroutine into a real async event stream."""

    queue: asyncio.Queue[LLMEvent] = asyncio.Queue()

    def _on_token(tok: str) -> None:
        queue.put_nowait(LLMToken(text=tok))

    def _on_thinking(content: str) -> None:
        queue.put_nowait(LLMThinking(content=content))

    def _on_tool_call(tool: str, params: dict) -> None:
        queue.put_nowait(LLMToolCall(tool=tool, params=params))

    def _on_tool_result(tool: str, summary: str) -> None:
        queue.put_nowait(LLMToolResult(tool=tool, summary=summary))

    def _on_status(state: str, message: str) -> None:
        queue.put_nowait(LLMStatus(state=state, message=message))

    task = asyncio.create_task(
        invoke(_on_token, _on_thinking, _on_tool_call, _on_tool_result, _on_status)
    )
    while not task.done() or not queue.empty():
        try:
            yield await asyncio.wait_for(queue.get(), timeout=0.05)
        except asyncio.TimeoutError:
            continue

    try:
        result = await task
    except Exception as exc:
        yield LLMDone(response="", provider=done_provider, success=False, error=str(exc))
        return

    yield LLMDone(
        response=result.get("response", ""),
        tool_calls_pending=result.get("tool_calls_pending", []),
        usage=result.get("usage", {}),
        provider=result.get("provider", done_provider),
        success=result.get("success", False),
        cancelled=result.get("cancelled", False),
        error=result.get("error", ""),
    )


# ── Protocol ─────────────────────────────────────────────────────────────────

@runtime_checkable
class LLMProvider(Protocol):
    """Minimal interface every LLM backend must implement.

    ``stream()`` is an async generator that yields ``LLMEvent`` objects.
    The final event is always ``LLMDone``; callers may break early on
    ``LLMDone`` or consume the full stream.

    Parameters
    ----------
    messages:
        Full conversation history (list of {"role": …, "content": …} dicts).
    tools:
        OpenAI-format function schema list; empty list disables tool calls.
    cancel_event:
        asyncio.Event that, when set, signals the provider to stop.
    """

    async def stream(
        self,
        messages: list,
        tools: list,
        *,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[LLMEvent, None]:
        ...


# ── Thin adapters (wrap existing callables as LLMProvider) ───────────────────

class OllamaProvider:
    """Wraps ``stream_ollama`` as an ``LLMProvider``.

    Import lazily to avoid circular dependencies — ``stream_ollama`` lives in
    the same providers package and rebinds globals from aria_cli at startup.
    """

    def __init__(
        self,
        ollama_url: str,
        model: str,
        *,
        system_override: Optional[str] = None,
        show_market_prefetch_status: bool = True,
    ) -> None:
        self.ollama_url = ollama_url
        self.model = model
        self.system_override = system_override
        self.show_market_prefetch_status = show_market_prefetch_status

    async def stream(
        self,
        messages: list,
        tools: list,
        *,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[LLMEvent, None]:
        stream_ollama = _resolve_ollama_stream()

        # Extract last user message as the prompt; the rest is history
        history = [m for m in messages if not (m.get("role") == "user" and m is messages[-1])]
        prompt = messages[-1].get("content", "") if messages else ""

        async def _invoke(on_token, on_thinking, on_tool_call, on_tool_result, _on_status):
            return await stream_ollama(
                self.ollama_url,
                prompt,
                history,
                model=self.model,
                on_token=on_token,
                on_thinking=on_thinking,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
                cancel_event=cancel_event,
                enable_tools=bool(tools),
                tool_schemas=list(tools or []),
                defer_tool_execution=True,
                system_override=self.system_override,
                show_market_prefetch_status=self.show_market_prefetch_status,
            )

        async for event in _stream_callback_provider(_invoke, done_provider="ollama"):
            yield event


class AriaSSEProvider:
    """Wraps ``stream_chat`` (Aria cloud SSE) as an ``LLMProvider``."""

    def __init__(
        self,
        api_url: str,
        model: str,
        *,
        auth_token: Optional[str] = None,
        thinking_mode: str = "auto",
        user_context: Optional[dict] = None,
        system_override: Optional[str] = None,
        project_context: str = "",
        use_react_gateway: bool = False,
        local_tools: bool = False,
    ) -> None:
        self.api_url = api_url
        self.model = model
        self.auth_token = auth_token
        self.thinking_mode = thinking_mode
        self.user_context = user_context or {}
        self.system_override = system_override
        self.project_context = project_context
        self.use_react_gateway = use_react_gateway
        self.local_tools = local_tools

    async def stream(
        self,
        messages: list,
        tools: list,
        *,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[LLMEvent, None]:
        from aria_code.apps.cli.providers.llm.sse_stream import stream_chat

        history = [m for m in messages if not (m.get("role") == "user" and m is messages[-1])]
        prompt = messages[-1].get("content", "") if messages else ""

        uctx = dict(self.user_context)
        if self.system_override:
            uctx["system_role_override"] = self.system_override

        async def _invoke(on_token, on_thinking, on_tool_call, on_tool_result, on_status):
            return await stream_chat(
                self.api_url,
                prompt,
                history,
                model=self.model,
                thinking_mode=self.thinking_mode,
                user_context=uctx or None,
                auth_token=self.auth_token,
                on_token=on_token,
                on_thinking=on_thinking,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
                on_status=on_status,
                cancel_event=cancel_event,
                project_context=self.project_context,
                use_react_gateway=self.use_react_gateway,
                tool_schemas=list(tools or []) if self.local_tools else None,
                local_tool_execution=self.local_tools,
            )

        async for event in _stream_callback_provider(_invoke, done_provider="aria_sse"):
            yield event


def _opt_in(config: dict, key: str, default: bool = True) -> bool:
    """Read a tri-state boolean setting where "unset" means *default*.

    ``config.get(key, default)`` is the wrong tool here and produced a real
    failure: a config holding ``"use_vertexai": null`` returns None — the
    stored value, not the default — so a Google model silently opted out of
    Vertex and went down the OpenAI-compatible path instead. Settings that are
    written as null by a config round-trip have to mean "not configured".
    """
    value = config.get(key)
    return default if value is None else bool(value)


def _gcloud(*args: str, timeout: float = 20) -> str:
    import subprocess

    result = subprocess.run(["gcloud", *args], capture_output=True, text=True, timeout=timeout,
                            stdin=subprocess.DEVNULL)
    return result.stdout.strip() if result.returncode == 0 else ""


def _adc_available() -> bool:
    """Application-default credentials exist (file, env, or running on Google Cloud)."""
    import os

    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("K_SERVICE") or os.getenv("GCE_METADATA_HOST"):
        return True
    if os.name == "nt":
        folder = os.getenv("CLOUDSDK_CONFIG") or os.path.join(os.getenv("APPDATA", ""), "gcloud")
    else:
        folder = os.getenv("CLOUDSDK_CONFIG") or os.path.join(os.path.expanduser("~"), ".config", "gcloud")
    return os.path.isfile(os.path.join(folder, "application_default_credentials.json"))


def google_cloud_available() -> bool:
    """Credentials Vertex AI can use: application-default credentials or a gcloud CLI."""
    import shutil

    return _adc_available() or bool(shutil.which("gcloud"))


def _google_api_key(config: dict) -> str:
    import os

    from aria_code.providers.llm.registry import _load_provider_cfg_from_file

    for value in (config.get("api_key"), config.get("gemini_key"), os.getenv("GEMINI_API_KEY"),
                  os.getenv("GOOGLE_API_KEY"), _load_provider_cfg_from_file("google").get("api_key")):
        if str(value or "").strip():
            return str(value).strip()
    return ""


def _gcloud_login_only(config: dict) -> bool:
    """No ADC and no Gemini key, but a gcloud CLI to borrow a token from."""
    import shutil

    return not _adc_available() and not _google_api_key(config) and bool(shutil.which("gcloud"))


def _gcloud_folder() -> str:
    import os

    if os.name == "nt":
        return os.getenv("CLOUDSDK_CONFIG") or os.path.join(os.getenv("APPDATA", ""), "gcloud")
    return os.getenv("CLOUDSDK_CONFIG") or os.path.join(os.path.expanduser("~"), ".config", "gcloud")


def _gcloud_default_project(folder: str) -> str:
    """The active gcloud configuration's core/project, read from its file."""
    import configparser
    import os

    try:
        with open(os.path.join(folder, "active_config"), encoding="utf-8") as handle:
            active = handle.read().strip() or "default"
    except OSError:
        active = "default"
    parser = configparser.ConfigParser()
    try:
        parser.read(os.path.join(folder, "configurations", f"config_{active}"), encoding="utf-8")
        return parser.get("core", "project", fallback="").strip()
    except configparser.Error:
        return ""


def google_readiness(config: dict) -> str:
    """"" when a Gemini request can be attempted, else what is missing.

    The banner said "Cloud model configured" for the default model on a
    machine with no Google credentials at all, and the first question then
    failed. This reads only the environment and gcloud's files — no
    subprocess — so it is cheap enough for startup.
    """
    import os
    import shutil

    if _google_api_key(config) or _adc_available():
        return ""
    if not shutil.which("gcloud"):
        return "no Google credentials"
    # Same order as vertex_openai_endpoint, so the banner names the problem
    # the first request will report.
    folder = _gcloud_folder()
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or str(config.get("gcp_project") or "")
               or _gcloud_default_project(folder))
    if not project or project == "(unset)":
        return "no Google Cloud project"
    if not any(os.path.isfile(os.path.join(folder, name)) for name in ("credentials.db", "access_tokens.db")):
        return "gcloud not signed in"
    return ""


def is_vertex_open_model(model: str) -> bool:
    """A model Vertex serves through its OpenAI-compatible endpoint, not generateContent.

    Gemma ("gemma-4-26b-a4b-it-maas", "gemma-3-27b-it") and other Model Garden
    managed models ("…-maas") are open models: the Gemini API does not serve
    them, and the native SDK path 404'd.
    """
    name = str(model or "").lower().split("/")[-1]
    return name.startswith("gemma") or name.endswith("-maas")


def _adc_access_token() -> str:
    """An access token from application-default credentials, or "" if there are none."""
    try:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(google.auth.transport.requests.Request())
        return str(credentials.token or "")
    except Exception:
        return ""


def vertex_openai_endpoint(config: dict) -> dict:
    """Vertex AI's OpenAI-compatible endpoint and a token from the gcloud login.

    Returns {} when gcloud is not installed (the caller reports the missing
    API key), {"error": …} when gcloud is there but the request cannot be
    made, otherwise {"token", "base_url", "project", "location"}.

    This path used to read the project from `gcloud config get-value project`
    alone; with no default project set that is empty, and every request went
    to ".../projects//locations/...". The region was fixed to us-central1,
    and both gcloud calls ran without a timeout on the event loop.
    """
    import os
    import shutil

    # Application-default credentials first (a CI runner signed in with
    # Workload Identity Federation, a service account, `gcloud auth
    # application-default login`), then the gcloud CLI's own login.
    adc_token = _adc_access_token() if _adc_available() else ""
    if not adc_token and not shutil.which("gcloud"):
        return {}
    try:
        project = (os.getenv("GOOGLE_CLOUD_PROJECT") or str(config.get("gcp_project") or "")).strip()
        if not project and shutil.which("gcloud"):
            project = _gcloud("config", "get-value", "project", timeout=10).strip()
        if not project or project == "(unset)":
            return {"error": "vertex_needs_project: set GOOGLE_CLOUD_PROJECT, /config set gcp_project=<id>, "
                             "or `gcloud config set project <id>`"}
        location = (os.getenv("GOOGLE_CLOUD_LOCATION") or str(config.get("gcp_location") or "") or "global").strip()
        token = adc_token or _gcloud("auth", "print-access-token")
    except Exception as exc:          # timeout, permissions: report, never hang the chat
        return {"error": f"vertex_gcloud_failed: {type(exc).__name__}"}
    if not token:
        return {"error": "vertex_not_logged_in: run `gcloud auth login`"}
    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    return {"token": token, "project": project, "location": location,
            "base_url": f"https://{host}/v1/projects/{project}/locations/{location}/endpoints/openapi"}


class ConfiguredProvider:
    """Adapter for the provider selected by ``/model provider/model``.

    Ollama keeps its dedicated CLI adapter because that path owns Aria's local
    intent and tool orchestration. Other local runtimes use the shared
    ``LocalLLMProvider`` OpenAI-compatible transport, while cloud APIs use the
    provider registry (including Anthropic's native protocol).
    """

    LOCAL_OPENAI_BACKENDS = {"lmstudio", "vllm", "llamacpp", "jan", "custom"}
    GENERIC_OPENAI_BACKENDS = {
        "google", "gemini", "xai", "grok", "mistral", "cohere",
        "perplexity", "baidu", "ernie", "qianfan", "bytedance",
        "doubao", "ark", "minimax", "stepfun", "01ai", "yi",
    }
    GENERIC_ENV_KEYS = {
        "google": "GOOGLE_API_KEY", "gemini": "GOOGLE_API_KEY",
        "xai": "XAI_API_KEY", "grok": "XAI_API_KEY",
        "mistral": "MISTRAL_API_KEY", "cohere": "COHERE_API_KEY",
        "perplexity": "PERPLEXITY_API_KEY", "baidu": "QIANFAN_ACCESS_KEY",
        "ernie": "QIANFAN_ACCESS_KEY", "qianfan": "QIANFAN_ACCESS_KEY",
        "bytedance": "ARK_API_KEY", "doubao": "ARK_API_KEY",
        "ark": "ARK_API_KEY", "minimax": "MINIMAX_API_KEY",
        "stepfun": "STEPFUN_API_KEY", "01ai": "ONEAI_API_KEY",
        "yi": "ONEAI_API_KEY",
    }
    GENERIC_BASE_URLS = {
        "google": "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
        "xai": "https://api.x.ai/v1", "grok": "https://api.x.ai/v1",
        "mistral": "https://api.mistral.ai/v1",
        "cohere": "https://api.cohere.ai/compatibility/v1",
        "perplexity": "https://api.perplexity.ai",
        "baidu": "https://qianfan.baidubce.com/v2",
        "ernie": "https://qianfan.baidubce.com/v2",
        "qianfan": "https://qianfan.baidubce.com/v2",
        "bytedance": "https://ark.cn-beijing.volces.com/api/v3",
        "doubao": "https://ark.cn-beijing.volces.com/api/v3",
        "ark": "https://ark.cn-beijing.volces.com/api/v3",
        "minimax": "https://api.minimax.chat/v1",
        "stepfun": "https://api.stepfun.com/v1",
        "01ai": "https://api.lingyiwanwu.com/v1",
        "yi": "https://api.lingyiwanwu.com/v1",
    }

    def __init__(
        self,
        config: dict,
        model: str,
        *,
        system_override: Optional[str] = None,
    ) -> None:
        self.config = dict(config or {})
        from aria_code.apps.cli.providers.chat_routing import (
            model_provider,
            normalize_provider_name,
        )

        # A provider-qualified model id names its own backend and wins over
        # local_provider — the default config pairs model="gemini-pro" with
        # local_provider="ollama", so deriving the backend from local_provider
        # alone sent every Gemini request to Ollama.
        declared = model_provider(model)
        self.backend = declared or normalize_provider_name(
            self.config.get("local_provider") or "ollama"
        )
        # Downstream APIs expect the bare model name; "google/gemini-2.5-pro"
        # would 404 against Google's endpoint and would be re-prefixed into
        # "google/google/gemini-2.5-pro" by the registry path below.
        self.model = model.split("/", 1)[1] if declared else model
        self.config["local_provider"] = self.backend
        self.system_override = system_override

    async def _stream_vertex_open_model(self, prepared: list, tools: list, cancel_event):
        """Gemma and other managed open models, through Vertex's OpenAI-compatible endpoint."""
        from aria_code.local_llm_provider import LocalLLMProvider

        vertex = await asyncio.to_thread(vertex_openai_endpoint, self.config)
        if not vertex or vertex.get("error"):
            yield LLMDone(response="", provider="vertexai", success=False,
                          error=(vertex or {}).get("error") or
                          "vertex_needs_credentials: run `gcloud auth application-default login` "
                          "or set GOOGLE_APPLICATION_CREDENTIALS")
            return
        cfg = dict(self.config)
        cfg.update({
            "model": f"google/{self.model}",
            "local_provider": "custom",
            "custom_endpoint": vertex["base_url"],
            "custom_model": f"google/{self.model}",
            "local_api_key": vertex["token"],
        })
        provider = LocalLLMProvider.from_config(cfg)
        async for event in provider.stream(prepared, tools=tools, cancel_event=cancel_event):
            kind = event.get("type")
            if kind == "token":
                yield LLMToken(text=str(event.get("text", "")))
            elif kind == "thinking":
                yield LLMThinking(content=str(event.get("text", "")))
            elif kind == "tool_call":
                yield LLMToolCall(tool=str(event.get("name", "")), params=dict(event.get("arguments") or {}))
            elif kind == "error":
                yield LLMDone(response="", provider="vertexai", success=False,
                              error=str(event.get("message") or "provider_error"))
                return
            elif kind == "done":
                yield LLMDone(response=str(event.get("text") or ""), usage=dict(event.get("usage") or {}),
                              provider="vertexai", success=True,
                              cancelled=str(event.get("stop_reason") or "") == "cancelled")
                return
        yield LLMDone(response="", provider="vertexai", success=False, error="empty_response")

    def _messages(self, messages: list) -> list:
        prepared = [dict(message) for message in messages]
        if not self.system_override:
            return prepared
        if prepared and prepared[0].get("role") == "system":
            prepared[0]["content"] = (
                self.system_override + "\n\n" + str(prepared[0].get("content", ""))
            )
        else:
            prepared.insert(0, {"role": "system", "content": self.system_override})
        return prepared

    async def stream(
        self,
        messages: list,
        tools: list,
        *,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[LLMEvent, None]:
        prepared = self._messages(messages)
        
        native_vertex = self.backend in ("vertexai", "vertex-ai", "google-genai") or (
            self.backend in ("google", "gemini") and _opt_in(self.config, "use_vertexai")
        )
        if self.backend in ("google", "gemini", "vertexai", "vertex-ai", "google-genai") \
                and is_vertex_open_model(self.model):
            async for evt in self._stream_vertex_open_model(prepared, tools, cancel_event):
                yield evt
            return
        if native_vertex and self.backend in ("google", "gemini") and _gcloud_login_only(self.config):
            # The SDK route accepts only application-default credentials. With
            # just a `gcloud auth login` it failed outright, so a logged-in
            # user could not reach Gemini; the OpenAI-compatible Vertex
            # endpoint below takes the gcloud login's token instead.
            native_vertex = False
        if native_vertex:
            from aria_code.apps.cli.providers.vertexai_stream import VertexAIProvider
            provider = VertexAIProvider(
                model=self.model,
                config=self.config,
                system_override=self.system_override,
            )
            async for evt in provider.stream(prepared, tools=tools, cancel_event=cancel_event):
                yield evt
            return

        if self.backend in self.LOCAL_OPENAI_BACKENDS | self.GENERIC_OPENAI_BACKENDS:
            from aria_code.local_llm_provider import LocalLLMProvider

            cfg = dict(self.config)
            cfg["model"] = self.model
            if self.backend in self.GENERIC_OPENAI_BACKENDS:
                import os
                from aria_code.providers.llm.registry import _load_provider_cfg_from_file

                file_cfg = _load_provider_cfg_from_file(self.backend)
                api_key = (
                    os.getenv(self.GENERIC_ENV_KEYS[self.backend], "")
                    or str(file_cfg.get("api_key") or "")
                )
                base_url = file_cfg.get("base_url") or self.GENERIC_BASE_URLS[self.backend]
                model_name = self.model

                if not api_key and self.backend in ("google", "gemini"):
                    # No Gemini API key: use Vertex AI with the gcloud login.
                    vertex = await asyncio.to_thread(vertex_openai_endpoint, self.config)
                    if vertex.get("error"):
                        yield LLMDone(response="", provider="vertexai", success=False, error=vertex["error"])
                        return
                    if vertex:
                        api_key, base_url = vertex["token"], vertex["base_url"]
                        if not model_name.startswith("google/"):
                            model_name = f"google/{model_name}"

                if not api_key:
                    yield LLMDone(
                        response="", provider=self.backend, success=False,
                        error=f"missing_api_key:{self.backend}",
                    )
                    return
                cfg.update({
                    "local_provider": "custom",
                    "custom_endpoint": base_url,
                    "custom_model": model_name,
                    "local_api_key": api_key,
                })
            provider = LocalLLMProvider.from_config(cfg)
            source = self.backend
            event_stream = provider.stream(prepared, tools=tools, cancel_event=cancel_event)
        else:
            from aria_code.providers.llm.base import Message
            from aria_code.providers.llm.registry import get_provider

            try:
                provider = get_provider(f"{self.backend}/{self.model}")
            except Exception as exc:
                yield LLMDone(
                    response="", provider=self.backend, success=False,
                    error=f"{self.backend}: {exc}",
                )
                return
            source = self.backend
            # The registry providers send role and content only; native tool
            # history would reach them as "tool" messages with no call id.
            from aria_code.providers.tool_messages import flatten_tool_turns

            registry_messages = [
                Message(
                    role=str(message.get("role", "user")),
                    content=str(message.get("content", "")),
                    name=message.get("name"),
                    tool_call_id=message.get("tool_call_id"),
                )
                for message in flatten_tool_turns(prepared)
            ]
            # providers.llm accepts bare function schemas, while the CLI owns
            # OpenAI envelopes. Normalize once at this adapter boundary.
            registry_tools = [
                item.get("function", item)
                if isinstance(item, dict) and item.get("type") == "function"
                else item
                for item in tools
            ]
            event_stream = provider.stream(
                registry_messages, tools=registry_tools, cancel_event=cancel_event
            )

        async for event in event_stream:
            kind = event.get("type")
            if kind == "token":
                yield LLMToken(text=str(event.get("text", "")))
            elif kind == "thinking":
                yield LLMThinking(content=str(event.get("text", "")))
            elif kind == "tool_call":
                yield LLMToolCall(
                    tool=str(event.get("name", "")),
                    params=dict(event.get("arguments") or {}),
                )
            elif kind == "error":
                yield LLMDone(
                    response="",
                    provider=source,
                    success=False,
                    error=str(event.get("message") or "provider_error"),
                )
                return
            elif kind == "done":
                stop_reason = str(event.get("stop_reason") or "")
                yield LLMDone(
                    response=str(event.get("text") or ""),
                    usage=dict(event.get("usage") or {}),
                    provider=source,
                    success=True,
                    cancelled=stop_reason == "cancelled",
                )
                return

        yield LLMDone(
            response="", provider=source, success=False, error="empty_response"
        )
