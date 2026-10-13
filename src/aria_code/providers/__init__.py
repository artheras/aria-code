"""
providers/ — Aria Code 统一 LLM Provider 层
============================================
用户在 ~/.aria/providers.yaml 等用户级文件里声明提供商（项目文件不能声明），
CLI 自动按优先级路由，任何一级失败自动降级到下一级。

支持的提供商:
  本地:  ollama, lmstudio, vllm, llamacpp, jan
  云端:  deepseek, openai, anthropic, groq, together, dashscope

快速使用:
    from providers.llm.registry import get_provider, stream_cloud_fallback
    provider = get_provider("deepseek")
    async for chunk in provider.stream(messages):
        print(chunk["text"], end="", flush=True)
"""

from .llm.registry import (
    get_provider,
    list_available_providers,
    stream_cloud_fallback,
    register_provider,
)

__all__ = [
    "get_provider",
    "list_available_providers",
    "stream_cloud_fallback",
    "register_provider",
]
