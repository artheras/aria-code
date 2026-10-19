"""
providers/llm/openai_compat.py — OpenAI 兼容协议通用 Provider
==============================================================
DeepSeek / OpenAI / Groq / Together / LM Studio / vLLM / llama.cpp
全部走同一套 /v1/chat/completions SSE 协议。

各 provider 只需继承并声明 DEFAULT_BASE_URL 和 DEFAULT_MODEL。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, AsyncIterator, Dict, List, Optional

from .base import BaseLLMProvider, Message, ProviderConfig

logger = logging.getLogger(__name__)


def chat_completions_url(base_url: str) -> str:
    """Join an OpenAI-compatible base URL without duplicating its version."""
    base = base_url.rstrip("/")
    if re.search(r"/v\d+(?:beta)?$", base):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


class OpenAICompatProvider(BaseLLMProvider):
    """
    通用 OpenAI 兼容 Provider。
    子类覆盖 DEFAULT_BASE_URL / DEFAULT_MODEL / provider_name。
    """

    provider_name    = "openai_compat"
    supports_tools   = True
    supports_thinking = False
    local            = False

    DEFAULT_BASE_URL = "https://api.openai.com"
    DEFAULT_MODEL    = "gpt-4o-mini"

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.base_url = (config.base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.model    = config.model or self.DEFAULT_MODEL
        self.api_key  = config.api_key or ""

    async def is_available(self) -> bool:
        return bool(self.api_key)

    async def stream(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        cancel_event=None,
    ) -> AsyncIterator[Dict[str, Any]]:
        import aiohttp

        temp     = temperature if temperature is not None else self.config.temperature
        n_tokens = max_tokens  if max_tokens  is not None else self.config.max_tokens

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }
        from aria_code.providers.openai_params import sampling_params

        payload: Dict[str, Any] = {
            "model":       self.model,
            "messages":    [{"role": m.role, "content": m.content} for m in messages],
            **sampling_params(self.base_url, self.model, temp, n_tokens),
            "stream":      True,
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": t} for t in tools
            ]
            payload["tool_choice"] = "auto"

        # Some compatible endpoints already include their API version
        # (Moonshot /v1, ZhiPu /v4). Avoid generating /v1/v1 or /v4/v1.
        url = chat_completions_url(self.base_url)
        # aiohttp 不自动读系统代理，需显式传入
        proxy = (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
                 or os.getenv("HTTP_PROXY")  or os.getenv("http_proxy"))

        try:
            async with aiohttp.ClientSession(trust_env=True) as sess:
                async with sess.post(
                    url, json=payload, headers=headers,
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=self.config.timeout)
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        yield {"type": "error",
                               "message": f"HTTP {resp.status}: {body[:300]}"}
                        return

                    pending_tool: Dict = {}
                    async for raw in resp.content:
                        if cancel_event and cancel_event.is_set():
                            return

                        line = raw.decode("utf-8", errors="ignore").strip()
                        if not line or line == "data: [DONE]":
                            continue
                        if not line.startswith("data: "):
                            continue

                        try:
                            data = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue

                        # 处理 thinking tokens（DeepSeek-R1 等）
                        reasoning = (
                            ((data.get("choices") or [{}])[0]
                             .get("delta") or {})
                            .get("reasoning_content")
                        )
                        if reasoning:
                            yield {"type": "thinking", "text": reasoning}

                        # 普通 token
                        delta = ((data.get("choices") or [{}])[0].get("delta") or {})
                        token = delta.get("content") or ""
                        if token:
                            yield {"type": "token", "text": token}

                        # 工具调用（SSE 增量拼接）
                        for tc in delta.get("tool_calls") or []:
                            fn   = tc.get("function") or {}
                            idx  = tc.get("index", 0)
                            name = fn.get("name", "")
                            args_chunk = fn.get("arguments", "")

                            if name:
                                if pending_tool:
                                    # emit previous tool
                                    try:
                                        args = json.loads(pending_tool["args"])
                                    except Exception:
                                        args = {"_raw": pending_tool["args"]}
                                    yield {"type": "tool_call",
                                           "name": pending_tool["name"],
                                           "arguments": args}
                                pending_tool = {"name": name, "args": "", "idx": idx}
                            if args_chunk:
                                pending_tool["args"] = pending_tool.get("args", "") + args_chunk

                        # finish reason
                        finish = ((data.get("choices") or [{}])[0].get("finish_reason"))
                        if finish in ("stop", "tool_calls", "length"):
                            if pending_tool:
                                try:
                                    args = json.loads(pending_tool["args"])
                                except Exception:
                                    args = {"_raw": pending_tool["args"]}
                                yield {"type": "tool_call",
                                       "name": pending_tool["name"],
                                       "arguments": args}
                                pending_tool = {}
                            yield {"type": "done"}
                            return

        except aiohttp.ClientConnectorError as e:
            yield {"type": "error", "message": f"连接 {self.base_url} 失败: {e}"}
        except Exception as e:
            yield {"type": "error", "message": f"Provider 错误: {e}"}


# ── 具体 Provider 子类（只需声明几个属性）────────────────────────────────────

class DeepSeekProvider(OpenAICompatProvider):
    provider_name    = "deepseek"
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_MODEL    = "deepseek-chat"
    supports_thinking = True   # deepseek-reasoner 支持

    def __init__(self, config: ProviderConfig):
        if not config.api_key:
            config.api_key = os.getenv("DEEPSEEK_API_KEY", "")
        super().__init__(config)
        # 思考模型别名
        if self.model in ("deepseek-reasoner", "deepseek-r1"):
            self.supports_thinking = True


class OpenAIProvider(OpenAICompatProvider):
    provider_name    = "openai"
    DEFAULT_BASE_URL = "https://api.openai.com"
    DEFAULT_MODEL    = "gpt-4o-mini"

    def __init__(self, config: ProviderConfig):
        if not config.api_key:
            config.api_key = os.getenv("OPENAI_API_KEY", "")
        super().__init__(config)


class GroqProvider(OpenAICompatProvider):
    provider_name    = "groq"
    DEFAULT_BASE_URL = "https://api.groq.com/openai"
    DEFAULT_MODEL    = "llama-3.3-70b-versatile"

    def __init__(self, config: ProviderConfig):
        if not config.api_key:
            config.api_key = os.getenv("GROQ_API_KEY", "")
        super().__init__(config)


class GoogleProvider(OpenAICompatProvider):
    """Gemini through Google's OpenAI-compatible endpoint.

    The registry previously had no Google entry at all, so Gemini — the default
    model in the shipped config — could not be reached through the provider
    chain and never appeared in fallback.  Vertex AI (service-account / ADC) is
    handled separately by apps/cli/providers/vertexai_stream.py; this covers the
    API-key path.
    """

    provider_name    = "google"
    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
    DEFAULT_MODEL    = "gemini-3.8-flash"

    def __init__(self, config: ProviderConfig):
        if not config.api_key:
            config.api_key = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
        super().__init__(config)


class TogetherProvider(OpenAICompatProvider):
    provider_name    = "together"
    DEFAULT_BASE_URL = "https://api.together.xyz"
    DEFAULT_MODEL    = "meta-llama/Llama-3.3-70B-Instruct-Turbo"

    def __init__(self, config: ProviderConfig):
        if not config.api_key:
            config.api_key = os.getenv("TOGETHER_API_KEY", "")
        super().__init__(config)


class DashScopeProvider(OpenAICompatProvider):
    """阿里云通义（OpenAI 兼容端点）"""
    provider_name    = "dashscope"
    DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode"
    DEFAULT_MODEL    = "qwen-plus"

    def __init__(self, config: ProviderConfig):
        if not config.api_key:
            config.api_key = os.getenv("DASHSCOPE_API_KEY", "")
        super().__init__(config)


class LMStudioProvider(OpenAICompatProvider):
    """LM Studio 本地服务"""
    provider_name    = "lmstudio"
    DEFAULT_BASE_URL = "http://localhost:1234"
    DEFAULT_MODEL    = "loaded-model"
    local            = True

    async def is_available(self) -> bool:
        import urllib.request
        try:
            urllib.request.urlopen(
                f"{self.base_url}/v1/models", timeout=2
            ).close()
            return True
        except Exception:
            return False


# ── 国内可访问 Provider（OpenAI 兼容协议）────────────────────────────────────

class SiliconFlowProvider(OpenAICompatProvider):
    """硅基流动 — 中国大陆可直连，支持 DeepSeek-V3/R1、Qwen 等主流模型"""
    provider_name    = "siliconflow"
    DEFAULT_BASE_URL = "https://api.siliconflow.cn"
    DEFAULT_MODEL    = "deepseek-ai/DeepSeek-V3"
    supports_thinking = True   # DeepSeek-R1 在此运行

    def __init__(self, config: ProviderConfig):
        if not config.api_key:
            config.api_key = os.getenv("SILICONFLOW_API_KEY", "")
        super().__init__(config)


class MoonshotProvider(OpenAICompatProvider):
    """Moonshot / Kimi — 中国大陆可直连"""
    provider_name    = "moonshot"
    DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
    DEFAULT_MODEL    = "moonshot-v1-8k"

    def __init__(self, config: ProviderConfig):
        if not config.api_key:
            config.api_key = os.getenv("MOONSHOT_API_KEY", "")
        super().__init__(config)


class ZhiPuProvider(OpenAICompatProvider):
    """智谱 GLM — 中国大陆可直连"""
    provider_name    = "zhipu"
    DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
    DEFAULT_MODEL    = "glm-4-flash"

    def __init__(self, config: ProviderConfig):
        if not config.api_key:
            config.api_key = os.getenv("ZHIPUAI_API_KEY", "")
        super().__init__(config)
