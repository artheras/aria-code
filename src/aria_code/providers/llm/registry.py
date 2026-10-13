"""
providers/llm/registry.py — LLM Provider 注册中心
==================================================
• 从 ~/.aria/providers.yaml 等用户级文件加载配置（不读项目里的 .aria.json）
• 按优先级自动路由：本地 Ollama → DeepSeek → OpenAI → Anthropic → Groq
• 提供 stream_cloud_fallback() 供 aria_cli.py 调用

用户配置示例 (~/.aria/providers.yaml):
    llm:
      default: ollama/qwen2.5:7b
      fallback:
        - deepseek/deepseek-chat
        - openai/gpt-4o-mini
        - anthropic/claude-3-5-haiku-latest
      code_tasks: ollama/qwen2.5-coder:7b
      heavy_analysis: anthropic/claude-3-5-sonnet-20241022
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple, Type

import yaml

from .base import BaseLLMProvider, Message, ProviderConfig
from .ollama import OllamaProvider
from .openai_compat import (
    DeepSeekProvider, OpenAIProvider, GroqProvider, GoogleProvider,
    TogetherProvider, DashScopeProvider, LMStudioProvider,
    SiliconFlowProvider, MoonshotProvider, ZhiPuProvider,
)
from .anthropic import AnthropicProvider
from .custom import CustomProviderSpec, build_custom_provider, parse_custom_providers
from aria_code.packages.aria_services.provider_health import (
    GLOBAL_PROVIDER_HEALTH,
    ProviderHealthRegistry,
    classify_provider_error,
)
from aria_code.packages.aria_core.paths import aria_home

logger = logging.getLogger(__name__)

# ── Provider 目录：name → class ──────────────────────────────────────────────
_PROVIDER_CLASSES: Dict[str, Type[BaseLLMProvider]] = {
    "ollama":       OllamaProvider,
    "deepseek":     DeepSeekProvider,
    "openai":       OpenAIProvider,
    "anthropic":    AnthropicProvider,
    # Gemini is the default model in the shipped config but had no registry
    # entry, so it could not be reached through the provider chain at all.
    "google":       GoogleProvider,
    "gemini":       GoogleProvider,
    "groq":         GroqProvider,
    "together":     TogetherProvider,
    "dashscope":    DashScopeProvider,
    "lmstudio":     LMStudioProvider,
    # 国内可访问
    "siliconflow":  SiliconFlowProvider,
    "moonshot":     MoonshotProvider,
    "zhipu":        ZhiPuProvider,
}

# ── 默认 fallback 优先级（无用户配置时）────────────────────────────────────
# 国内环境优先走 DeepSeek / SiliconFlow / DashScope，再尝试 OpenAI / Groq
_DEFAULT_FALLBACK_CHAIN = [
    ("ollama",       None,                    None),
    ("deepseek",     "DEEPSEEK_API_KEY",      "deepseek-chat"),
    ("siliconflow",  "SILICONFLOW_API_KEY",   "deepseek-ai/DeepSeek-V3"),
    ("dashscope",    "DASHSCOPE_API_KEY",     "qwen-plus"),
    ("moonshot",     "MOONSHOT_API_KEY",      "moonshot-v1-8k"),
    ("zhipu",        "ZHIPUAI_API_KEY",       "glm-4-flash"),
    ("google",       "GEMINI_API_KEY",        "gemini-3.5-flash"),
    ("openai",       "OPENAI_API_KEY",        "gpt-4o-mini"),
    ("anthropic",    "ANTHROPIC_API_KEY",     "claude-3-5-haiku-latest"),
    ("groq",         "GROQ_API_KEY",          "llama-3.3-70b-versatile"),
]

# ── 用户可注册自定义 provider ─────────────────────────────────────────────────
def register_provider(name: str, cls: Type[BaseLLMProvider]) -> None:
    """注册自定义 provider 类（供插件/用户扩展使用）"""
    _PROVIDER_CLASSES[name.lower()] = cls
    logger.info(f"✓ 注册自定义 provider: {name}")


# ── 配置加载 ──────────────────────────────────────────────────────────────────
_CONFIG_PATHS = [
    # ~/.arthera/providers.json is the primary path used by the aria-code CLI (/apikey command)
    aria_home() / "providers.json",
    # Legacy / alternative paths
    Path.home() / ".aria" / "providers.yaml",
    Path.home() / ".aria" / "providers.json",
    # No project-relative ./.aria.json or ./.aria.yaml: every reader of this
    # list chooses where conversations go or which credentials accompany them
    # (model_providers base_url + env_key, llm.<name>.base_url, the fallback
    # chain), and a cloned repository must not be able to decide that. The Go
    # runtime (go/runtime/provider_declared.go) applies the same rule.
]


_CUSTOM_SPECS_CACHE: Optional[Dict[str, "CustomProviderSpec"]] = None


def _custom_provider_specs() -> Dict[str, "CustomProviderSpec"]:
    """解析一次并缓存。配置在进程生命周期内不变，重复解析只会重复刷日志。"""
    global _CUSTOM_SPECS_CACHE
    if _CUSTOM_SPECS_CACHE is None:
        try:
            _CUSTOM_SPECS_CACHE = parse_custom_providers(_load_raw_user_config())
        except Exception as exc:
            logger.warning("解析 model_providers 失败，忽略自定义 provider: %s", exc)
            _CUSTOM_SPECS_CACHE = {}
    return _CUSTOM_SPECS_CACHE


def reload_custom_providers() -> Dict[str, "CustomProviderSpec"]:
    """清空缓存并重新解析（供 /config reload 之类的命令调用）。"""
    global _CUSTOM_SPECS_CACHE
    _CUSTOM_SPECS_CACHE = None
    return _custom_provider_specs()


def _load_raw_user_config() -> Dict:
    """读取用户配置的**顶层**结构。

    注意跟 _load_user_config() 的区别：那个函数返回的是 `llm` 子节（历史行为，
    调用方依赖它直接拿到 default/fallback），而 model_providers 与 llm 平级，
    必须从顶层取，否则永远读不到。
    """
    for path in _CONFIG_PATHS:
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) if path.suffix in (".yaml", ".yml") \
                       else __import__("json").load(fh)
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.debug("加载配置 %s 失败: %s", path, exc)
    return {}

def _load_user_config() -> Dict:
    for p in _CONFIG_PATHS:
        if p.exists():
            try:
                with open(p, encoding="utf-8") as f:
                    data = yaml.safe_load(f) if p.suffix in (".yaml",".yml") \
                           else __import__("json").load(f)
                    return data.get("llm", data) if isinstance(data, dict) else {}
            except Exception as e:
                logger.debug(f"加载配置 {p} 失败: {e}")
    return {}


def _load_provider_cfg_from_file(name: str) -> Dict[str, str]:
    """
    从 ~/.arthera/providers.json 的 llm 节读取指定 provider 的 api_key / base_url。
    这是 /apikey set 命令写入的位置；ProviderConfig.from_env() 只读环境变量，
    此函数补足文件侧的配置，让两者合并后才能正确工作。
    """
    import json as _json
    primary = aria_home() / "providers.json"
    for p in [primary] + _CONFIG_PATHS:
        if not p.exists():
            continue
        try:
            raw  = _json.loads(p.read_text(encoding="utf-8")) if p.suffix == ".json" \
                   else yaml.safe_load(p.read_text(encoding="utf-8"))
            llm  = raw.get("llm", raw) if isinstance(raw, dict) else {}
            entry = llm.get(name.lower(), {})
            if entry:
                return {k: v for k, v in entry.items() if v}
        except Exception:
            pass
    return {}


def _parse_provider_spec(spec: str) -> Tuple[str, Optional[str]]:
    """
    解析 'deepseek/deepseek-chat' → ('deepseek', 'deepseek-chat')
    解析 'ollama'                  → ('ollama', None)
    """
    if "/" in spec:
        name, model = spec.split("/", 1)
        return name.strip().lower(), model.strip()
    return spec.strip().lower(), None


def get_provider(
    spec: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> BaseLLMProvider:
    """
    按 spec 字符串实例化 provider。

    Examples:
        get_provider("ollama/qwen2.5:7b")
        get_provider("deepseek/deepseek-chat")
        get_provider("anthropic/claude-3-5-haiku-latest")
    """
    name, model = _parse_provider_spec(spec)

    # 用户在 model_providers 里声明的 provider 优先于内置同名项——用户显式写下
    # 的配置应当能覆盖内置默认（比如把 openai 指向自建的兼容网关）。
    custom = _custom_provider_specs().get(name)
    if custom is not None:
        if custom.env_key and not custom.api_key():
            raise ValueError(custom.missing_key_hint())
        provider = build_custom_provider(custom, model)
        if api_key:
            provider.config.api_key = api_key
        if base_url:
            provider.config.base_url = base_url
        return provider

    cls = _PROVIDER_CLASSES.get(name)
    if not cls:
        known = list(_PROVIDER_CLASSES) + list(_custom_provider_specs())
        raise ValueError(
            f"未知 provider: '{name}'。可用: {', '.join(sorted(known))}\n"
            f"如需接入未列出的服务，在 ~/.aria/providers.yaml 的 model_providers "
            f"段声明即可（只填 base_url 与 env_key，不要写密钥值）。"
        )
    cfg = _build_cfg(name, model)
    # 调用方显式传入的参数优先级最高
    if api_key:
        cfg.api_key = api_key
    if base_url:
        cfg.base_url = base_url
    return cls(cfg)


def list_available_providers() -> List[Dict[str, Any]]:
    """返回所有 provider 及其可用状态（同步，用于 /config 命令显示）"""
    result = []
    for name, cls in _PROVIDER_CLASSES.items():
        cfg       = _build_cfg(name)          # 合并环境变量 + providers.json
        available = cfg.is_configured()
        result.append({
            "name":      name,
            "available": available,
            "local":     cls.local,
            "tools":     cls.supports_tools,
            "thinking":  cls.supports_thinking,
            "source":    "builtin",
        })
    # 用户在 model_providers 里声明的 provider 也要出现在 /config 里——否则
    # 用户配好了却在列表中看不到，会以为没生效。
    for pid, spec in sorted(_custom_provider_specs().items()):
        result.append({
            "name":      pid,
            "available": bool(spec.api_key()) if spec.env_key else True,
            "local":     False,
            "tools":     True,
            "thinking":  False,
            "source":    "custom",
            "base_url":  spec.base_url,
            "hint":      "" if (not spec.env_key or spec.api_key()) else spec.missing_key_hint(),
        })
    return result


def _build_cfg(name: str, model: Optional[str] = None) -> ProviderConfig:
    """
    构建 ProviderConfig：环境变量优先，再回落到 providers.json，
    确保 /apikey set 保存的 key 能被实际使用。
    """
    cfg = ProviderConfig.from_env(name)
    file_cfg = _load_provider_cfg_from_file(name)

    # 补充 api_key（文件里的）— 环境变量已在 from_env() 中优先读取；
    # 文件是后备：提示用户改用环境变量以避免明文存储 key。
    if not cfg.api_key and file_cfg.get("api_key"):
        cfg.api_key = file_cfg["api_key"]
        _env_names = {
            "deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY", "groq": "GROQ_API_KEY",
            "siliconflow": "SILICONFLOW_API_KEY", "moonshot": "MOONSHOT_API_KEY",
            "zhipu": "ZHIPUAI_API_KEY", "dashscope": "DASHSCOPE_API_KEY",
        }
        if name.lower() in _env_names:
            logger.debug(
                "⚠ API key for '%s' loaded from ~/.arthera/providers.json (plaintext). "
                "Migrate to env var: export %s=<key>  then remove api_key from aria_code.providers.json.",
                name, _env_names[name.lower()],
            )
    # 补充 base_url（支持用户自定义端点 / 代理）
    if not cfg.base_url and file_cfg.get("base_url"):
        cfg.base_url = file_cfg["base_url"]

    if model:
        cfg.model = model
    return cfg


async def _try_provider(
    spec: str,
    messages: List[Message],
    on_token: Optional[Callable] = None,
    cancel_event=None,
    *,
    health: ProviderHealthRegistry | None = None,
) -> Optional[Dict[str, Any]]:
    """尝试用指定 provider 完成对话，失败返回 None。"""
    health = health or GLOBAL_PROVIDER_HEALTH
    try:
        name, model = _parse_provider_spec(spec)
        cls = _PROVIDER_CLASSES.get(name)
        if not cls:
            return None

        cfg      = _build_cfg(name, model)
        provider = cls(cfg)

        if health.provider_in_cooldown(name):
            logger.debug(f"[{name}] cooling down, skipped")
            return None

        if not await provider.is_available():
            logger.debug(f"[{name}] 不可用，跳过")
            health.mark_issue(classify_provider_error(name, "provider unavailable"))
            return None

        logger.info(f"[{name}] 尝试生成响应 (model={cfg.model})")
        full_text = ""
        async for event in provider.stream(
            messages, cancel_event=cancel_event
        ):
            t = event.get("type")
            if t == "token":
                tok = event.get("text", "")
                full_text += tok
                if on_token:
                    on_token(tok)
            elif t == "error":
                err = event.get("message")
                logger.debug(f"[{name}] 流式错误: {err}")
                health.mark_issue(classify_provider_error(name, err))
                return None
            elif t == "done":
                break

        if not full_text.strip():
            health.mark_issue(classify_provider_error(name, "provider returned no usable data"))
            return None

        health.mark_success(name)
        return {
            "success":  True,
            "response": full_text,
            "provider": name,
            "model":    cfg.model or "unknown",
        }
    except Exception as e:
        logger.debug(f"[{spec}] 异常: {e}")
        health.mark_issue(classify_provider_error(_parse_provider_spec(spec)[0], e))
        return None


async def stream_cloud_fallback(
    message: str,
    history: List[Dict],
    on_token: Optional[Callable] = None,
    cancel_event=None,
    *,
    health: ProviderHealthRegistry | None = None,
    include_defaults: bool = True,
) -> Dict[str, Any]:
    """
    CLI fallback 入口：当 Ollama 不可用时调用。
    按优先级依次尝试云端 provider，首个成功的直接返回。

    优先级:
      1. 用户 ~/.aria/providers.yaml 里的 fallback 列表
      2. 内置默认链: DeepSeek → OpenAI → Anthropic → Groq → DashScope
    """
    # 构建消息列表
    msgs: List[Message] = [
        Message(role="system", content=(
            "You are Aria, an AI-native product and software-engineering assistant. "
            "Help users review products, understand repositories, design architecture, "
            "write and verify code, and improve quality. Finance is an optional domain "
            "capability used only when the request is explicitly financial. Answer concisely "
            "and never claim to have inspected files or run tests unless tool evidence proves it."
        ))
    ]
    for h in (history or [])[-12:]:
        role = h.get("role", "user")
        if role in ("user", "assistant"):
            msgs.append(Message(role=role, content=h.get("content", "")))
    msgs.append(Message(role="user", content=message))

    # 加载用户配置的 fallback 链
    user_cfg = _load_user_config()
    user_fallback: List[str] = user_cfg.get("fallback", [])

    health = health or GLOBAL_PROVIDER_HEALTH

    # 云端 provider 列表（跳过本地）
    cloud_specs: List[str] = []
    for spec in user_fallback:
        name, _ = _parse_provider_spec(spec)
        cls = _PROVIDER_CLASSES.get(name)
        if cls and not cls.local and not health.provider_in_cooldown(name):
            cloud_specs.append(spec)

    # Built-in discovery is opt-in. The CLI default only follows the user's
    # explicit fallback list, preventing a selected Ollama model from silently
    # consuming an unrelated provider account.
    if include_defaults:
        for name, env_var, model in _DEFAULT_FALLBACK_CHAIN:
            cls = _PROVIDER_CLASSES.get(name)
            if not cls or cls.local:
                continue
            spec = f"{name}/{model}" if model else name
            if not any(s.startswith(name) for s in cloud_specs):
                # 环境变量 OR providers.json 任一有 key 即可
                has_key = (env_var and os.getenv(env_var)) or \
                          bool(_load_provider_cfg_from_file(name).get("api_key"))
                if has_key and not health.provider_in_cooldown(name):
                    cloud_specs.append(spec)

    if not cloud_specs:
        return {
            "success": False,
            "error":   "no_cloud_provider",
            "response": "",
            "provider": "none",
        }

    for spec in cloud_specs:
        result = await _try_provider(spec, msgs, on_token=on_token,
                                     cancel_event=cancel_event, health=health)
        if result:
            return result

    return {
        "success":  False,
        "error":    "all_providers_failed",
        "response": "",
        "provider": "none",
    }
