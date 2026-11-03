"""
providers/llm/ollama.py — Ollama 本地 LLM Provider
====================================================
连接本地运行的 Ollama 服务，支持工具调用（native + text-based fallback）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from .base import BaseLLMProvider, Message, ProviderConfig

logger = logging.getLogger(__name__)


class OllamaProvider(BaseLLMProvider):

    provider_name    = "ollama"
    supports_tools   = True
    supports_thinking = False
    local            = True

    DEFAULT_URL = "http://localhost:11434"

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        from aria_code.providers.ollama_models import ollama_base_url
        self.base_url = ollama_base_url({"ollama_base_url": config.base_url})
        self.model    = config.model or "qwen2.5:7b"

    async def is_available(self) -> bool:
        """探测 Ollama 服务是否在线"""
        import urllib.request
        for url in [self.base_url,
                    self.base_url.replace("localhost", "127.0.0.1")]:
            try:
                urllib.request.urlopen(f"{url}/api/tags", timeout=2).close()
                return True
            except Exception:
                continue
        return False

    async def list_models(self) -> List[str]:
        """返回已安装的模型列表"""
        import urllib.request
        import json as _json
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=3) as r:
                data = _json.loads(r.read())
                return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    async def stream(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        cancel_event=None,
    ) -> AsyncIterator[Dict[str, Any]]:
        import aiohttp

        from aria_code.providers.ollama_models import resolve_model_async
        try:
            model = await resolve_model_async(self.base_url, self.model)
        except ValueError as exc:
            yield {"type": "error", "message": str(exc)}
            return

        temp     = temperature if temperature is not None else self.config.temperature
        n_tokens = max_tokens  if max_tokens  is not None else self.config.max_tokens

        payload = {
            "model":   model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream":  True,
            "options": {"temperature": temp, "num_predict": n_tokens, "num_ctx": 32768},
        }
        if tools:
            # 只有支持 native tool call 的模型才注入（其他走 text-based 解析）
            payload["tools"] = tools

        url = f"{self.base_url}/api/chat"
        try:
            async with aiohttp.ClientSession(trust_env=True) as sess:
                async with sess.post(
                    url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.config.timeout)
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        yield {"type": "error",
                               "message": f"Ollama HTTP {resp.status}: {body[:200]}"}
                        return

                    async for raw in resp.content:
                        if cancel_event and cancel_event.is_set():
                            return
                        line = raw.decode("utf-8", errors="ignore").strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # Native tool calls
                        tc_list = (data.get("message") or {}).get("tool_calls") or []
                        for tc in tc_list:
                            fn = tc.get("function") or {}
                            args = fn.get("arguments", {})
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except json.JSONDecodeError:
                                    args = {"_raw": args}
                            yield {"type": "tool_call",
                                   "name": fn.get("name", ""),
                                   "arguments": args}

                        # Text token
                        token = (data.get("message") or {}).get("content", "")
                        if token:
                            yield {"type": "token", "text": token}

                        if data.get("done"):
                            yield {"type": "done"}
                            return

        except aiohttp.ClientConnectorError as e:
            yield {"type": "error",
                   "message": f"Ollama 连接失败 ({self.base_url}): {e}"}
        except Exception as e:
            yield {"type": "error", "message": f"Ollama 错误: {e}"}
