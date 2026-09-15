"""模型目录 —— 从 aria_cli.py 抽出的纯数据。

MODELS / MODEL_ALIASES / _MODEL_FALLBACK_PREFIXES 三个字面量原本在
aria_cli.py 的 1158-1551 行，合计约 390 行，占该文件的 4.5%，且完全没有
行为——已用 AST 核实过：零外部名字引用、零 lambda、零函数调用。

与 football_reports / broker_render 不同，这里**不需要**
_rebind_module_function_globals：那套机制解决的是函数 __globals__ 的绑定
问题，而这里是数据。aria_cli 用普通 `from ... import` 就会把名字放进自己的
命名空间，diagnostic_cmds / model_cmds 等 mixin 的裸名引用照常解析。
"""

from __future__ import annotations

MODELS = {
    # ════════════════════════════════════════════════════════════════════
    # ── Google Gemini 平台核心默认模型（Google Cloud）───────────────────────
    # ════════════════════════════════════════════════════════════════════
    "gemini-flash": {
        "id": "google/gemini-3.8-flash",
        "name": "Gemini Flash",
        "version": "3.8",
        "tag": "Platform",
        "speed": "★★★★★",
        "intelligence": "★★★★★",
        "description": "推荐：日常与代码，速度快，需要时会深入思考",
        "capabilities": ["multimodal", "1M context", "tool calls", "code", "thinking"],
        "thinking": True, "tools": True,
        "max_tokens": 8192, "num_ctx": 1048576, "temperature": 0.2,
        "badge": "Default",
    },
    "gemini-pro": {
        "id": "google/gemini-3.1-pro-preview",
        "name": "Gemini Pro",
        "version": "3.1",
        "tag": "Platform",
        "speed": "★★★",
        "intelligence": "★★★★★",
        "description": "深度：复杂分析与大型代码改动，较慢（预览版）",
        "capabilities": ["multimodal", "1M context", "tool calls", "code", "thinking"],
        "thinking": True, "tools": True,
        "max_tokens": 8192, "num_ctx": 1048576, "temperature": 0.2,
        "badge": "Deep",
    },
    "gemini-lite": {
        "id": "google/gemini-3.5-flash-lite",
        "name": "Gemini Flash-Lite",
        "version": "3.5",
        "tag": "Platform",
        "speed": "★★★★★",
        "intelligence": "★★★",
        "description": "最快：简单问答与小改动，成本最低",
        "capabilities": ["multimodal", "1M context", "tool calls", "low latency"],
        "thinking": False, "tools": True,
        "max_tokens": 8192, "num_ctx": 1048576, "temperature": 0.2,
        "badge": "Fast",
    },
    # ════════════════════════════════════════════════════════════════════
    # ── Qwen 家族（阿里巴巴）────────────────────────────────────────────
    # ════════════════════════════════════════════════════════════════════
    "qwen7b": {
        "id": "qwen2.5:7b",
        "name": "Qwen 2.5",
        "version": "7B",
        "tag": "Default",
        "speed": "★★★★",
        "intelligence": "★★★★★",
        "description": "主力推荐：中英双语最强 7B，工具调用稳定，金融/代码俱佳",
        "capabilities": ["chat", "tool calls", "financial analysis", "coding", "Chinese"],
        "thinking": False, "tools": True,
        "max_tokens": 4096, "num_ctx": 32768, "temperature": 0.3,
        "badge": "Default",
    },
    "qwen-coder": {
        "id": "qwen2.5-coder:7b",
        "name": "Qwen Coder",
        "version": "7B",
        "tag": "Code",
        "speed": "★★★★",
        "intelligence": "★★★★★",
        "description": "代码专精：量化策略生成、回测脚本、Python 金融工具",
        "capabilities": ["strategy code", "backtest", "Python", "quant development"],
        "thinking": False, "tools": True,
        "max_tokens": 4096, "num_ctx": 32768, "temperature": 0.2,
        "badge": "Code",
    },
    "qwen14b": {
        "id": "qwen2.5:14b",
        "name": "Qwen 2.5",
        "version": "14B",
        "tag": "Pro",
        "speed": "★★★",
        "intelligence": "★★★★★",
        "description": "高质量推理：需 ~10GB VRAM，复杂分析/长文档首选",
        "capabilities": ["complex analysis", "long context", "Chinese"],
        "thinking": False, "tools": True,
        "max_tokens": 8192, "num_ctx": 32768, "temperature": 0.3,
        "badge": "Pro",
    },
    "qwen32b": {
        "id": "qwen2.5:32b",
        "name": "Qwen 2.5",
        "version": "32B",
        "tag": "Max",
        "speed": "★★",
        "intelligence": "★★★★★",
        "description": "旗舰本地：需 ~20GB VRAM，媲美 GPT-4o 水平",
        "capabilities": ["flagship reasoning", "long context", "deep analysis"],
        "thinking": False, "tools": True,
        "max_tokens": 8192, "num_ctx": 32768, "temperature": 0.3,
        "badge": "Max",
    },
    "qwen3-8b": {
        "id": "qwen3:8b",
        "name": "Qwen 3",
        "version": "8B",
        "tag": "Latest",
        "speed": "★★★★",
        "intelligence": "★★★★★",
        "description": "Qwen3 最新一代：混合推理模式，/think 开启深度思考",
        "capabilities": ["hybrid reasoning", "thinking mode", "chat", "code"],
        "thinking": True, "tools": True,
        "max_tokens": 8192, "num_ctx": 32768, "temperature": 0.6,
        "badge": "Latest",
    },
    "qwen3-30b": {
        "id": "qwen3:30b-a3b",
        "name": "Qwen 3 MoE",
        "version": "30B-A3B",
        "tag": "MoE",
        "speed": "★★★★",
        "intelligence": "★★★★★",
        "description": "混合专家 MoE：30B参数激活3B，速度与质量双赢",
        "capabilities": ["MoE", "fast reasoning", "tool calls"],
        "thinking": True, "tools": True,
        "max_tokens": 8192, "num_ctx": 32768, "temperature": 0.6,
        "badge": "MoE",
    },
    "qwen-fast": {
        "id": "qwen2.5-coder:1.5b",
        "name": "Qwen Fast",
        "version": "1.5B",
        "tag": "Fast",
        "speed": "★★★★★",
        "intelligence": "★★★",
        "description": "超快响应：简单问答、实时报价、快速指令，~1GB RAM",
        "capabilities": ["fast chat", "simple queries", "ultra-low latency"],
        "thinking": False, "tools": False,
        "max_tokens": 2048, "num_ctx": 8192, "temperature": 0.3,
        "badge": "Fast",
    },
    # ════════════════════════════════════════════════════════════════════
    # ── DeepSeek 家族──────────────────────────────────────────────────
    # ════════════════════════════════════════════════════════════════════
    "deepseek-r1": {
        "id": "deepseek-r1:7b",
        "name": "DeepSeek R1",
        "version": "7B",
        "tag": "Think",
        "speed": "★★★",
        "intelligence": "★★★★★",
        "description": "深度推理：复杂投资决策、多步骤分析、Chain-of-Thought",
        "capabilities": ["deep reasoning", "chain-of-thought", "complex quant"],
        "thinking": True, "tools": False,
        "max_tokens": 4096, "num_ctx": 32768, "temperature": 0.3,
        "badge": "Think",
    },
    "deepseek-r1-1.5b": {
        "id": "deepseek-r1:1.5b",
        "name": "DeepSeek R1",
        "version": "1.5B",
        "tag": "Tiny",
        "speed": "★★★★★",
        "intelligence": "★★★",
        "description": "最小推理模型：~1GB，边缘设备/低内存机器首选",
        "capabilities": ["lightweight reasoning", "simple CoT"],
        "thinking": True, "tools": False,
        "max_tokens": 2048, "num_ctx": 8192, "temperature": 0.3,
        "badge": "Fast",
    },
    "deepseek-r1-14b": {
        "id": "deepseek-r1:14b",
        "name": "DeepSeek R1",
        "version": "14B",
        "tag": "Pro",
        "speed": "★★★",
        "intelligence": "★★★★★",
        "description": "强化推理 14B：数学/代码/金融逻辑最强本地选择",
        "capabilities": ["strong reasoning", "math", "code analysis"],
        "thinking": True, "tools": False,
        "max_tokens": 8192, "num_ctx": 32768, "temperature": 0.3,
        "badge": "Think",
    },
    # ════════════════════════════════════════════════════════════════════
    # ── Meta Llama 家族 ───────────────────────────────────────────────
    # ════════════════════════════════════════════════════════════════════
    "llama3.2-3b": {
        "id": "llama3.2:3b",
        "name": "Llama 3.2",
        "version": "3B",
        "tag": "Light",
        "speed": "★★★★★",
        "intelligence": "★★★",
        "description": "Meta 轻量级：~2GB，快速对话，英文性能出色",
        "capabilities": ["fast chat", "English", "summarization"],
        "thinking": False, "tools": True,
        "max_tokens": 4096, "num_ctx": 8192, "temperature": 0.3,
        "badge": "Fast",
    },
    "llama3.1-8b": {
        "id": "llama3.1:8b",
        "name": "Llama 3.1",
        "version": "8B",
        "tag": "Standard",
        "speed": "★★★★",
        "intelligence": "★★★★",
        "description": "Meta 主力 8B：英文任务顶级，工具调用完整支持",
        "capabilities": ["chat", "tool calls", "English", "reasoning"],
        "thinking": False, "tools": True,
        "max_tokens": 4096, "num_ctx": 131072, "temperature": 0.3,
        "badge": "Default",
    },
    "llama3.3-70b": {
        "id": "llama3.3:70b",
        "name": "Llama 3.3",
        "version": "70B",
        "tag": "Large",
        "speed": "★★",
        "intelligence": "★★★★★",
        "description": "Meta 最强开源：70B 需 ~40GB VRAM，媲美 GPT-4o",
        "capabilities": ["flagship English", "complex reasoning", "long context"],
        "thinking": False, "tools": True,
        "max_tokens": 8192, "num_ctx": 131072, "temperature": 0.3,
        "badge": "Pro",
    },
    # ════════════════════════════════════════════════════════════════════
    # ── Mistral / Mixtral 家族 ────────────────────────────────────────
    # ════════════════════════════════════════════════════════════════════
    "mistral-7b": {
        "id": "mistral:7b",
        "name": "Mistral",
        "version": "7B",
        "tag": "EU",
        "speed": "★★★★",
        "intelligence": "★★★★",
        "description": "欧洲顶级开源：结构化输出强，JSON 工具调用稳定",
        "capabilities": ["structured output", "tool calls", "JSON", "English"],
        "thinking": False, "tools": True,
        "max_tokens": 4096, "num_ctx": 32768, "temperature": 0.3,
        "badge": "Default",
    },
    "mistral-nemo": {
        "id": "mistral-nemo:12b",
        "name": "Mistral Nemo",
        "version": "12B",
        "tag": "Balanced",
        "speed": "★★★★",
        "intelligence": "★★★★★",
        "description": "Mistral × Nvidia：128K上下文，多语言支持佳",
        "capabilities": ["long context", "multilingual", "tool calls"],
        "thinking": False, "tools": True,
        "max_tokens": 8192, "num_ctx": 131072, "temperature": 0.3,
        "badge": "Pro",
    },
    # ════════════════════════════════════════════════════════════════════
    # ── Microsoft Phi 家族 ────────────────────────────────────────────
    # ════════════════════════════════════════════════════════════════════
    "phi4": {
        "id": "phi4:14b",
        "name": "Phi-4",
        "version": "14B",
        "tag": "STEM",
        "speed": "★★★",
        "intelligence": "★★★★★",
        "description": "微软 STEM 旗舰：数学/代码/科学推理超越同级，14B 需 ~8GB",
        "capabilities": ["math", "STEM", "code", "science reasoning"],
        "thinking": False, "tools": True,
        "max_tokens": 8192, "num_ctx": 16384, "temperature": 0.3,
        "badge": "STEM",
    },
    "phi4-mini": {
        "id": "phi4-mini:3.8b",
        "name": "Phi-4 Mini",
        "version": "3.8B",
        "tag": "Compact",
        "speed": "★★★★★",
        "intelligence": "★★★★",
        "description": "微软精简版：3.8B 打败多数 7B，数学/代码能力突出",
        "capabilities": ["math", "code", "compact", "fast"],
        "thinking": False, "tools": True,
        "max_tokens": 4096, "num_ctx": 16384, "temperature": 0.3,
        "badge": "Fast",
    },
    # ════════════════════════════════════════════════════════════════════
    # ── Google Gemma 3 家族 ───────────────────────────────────────────
    # ════════════════════════════════════════════════════════════════════
    "gemma3-4b": {
        "id": "gemma3:4b",
        "name": "Gemma 3",
        "version": "4B",
        "tag": "Google",
        "speed": "★★★★★",
        "intelligence": "★★★★",
        "description": "Google 轻量：4B 支持图像理解，多模态能力出色",
        "capabilities": ["multimodal", "vision", "fast chat", "Google"],
        "vision": True, "thinking": False, "tools": True,
        "max_tokens": 4096, "num_ctx": 8192, "temperature": 0.3,
        "badge": "Fast",
    },
    "gemma3-12b": {
        "id": "gemma3:12b",
        "name": "Gemma 3",
        "version": "12B",
        "tag": "Vision",
        "speed": "★★★★",
        "intelligence": "★★★★★",
        "description": "Google 中型：12B 视觉+文本综合能力强，~8GB VRAM",
        "capabilities": ["multimodal", "vision", "reasoning", "multilingual"],
        "vision": True, "thinking": False, "tools": True,
        "max_tokens": 8192, "num_ctx": 16384, "temperature": 0.3,
        "badge": "Vision",
    },
    # ════════════════════════════════════════════════════════════════════
    # ── Cloud 路由（需订阅或 API Key）────────────────────────────────
    # ════════════════════════════════════════════════════════════════════
    "gpt-oss-120b": {
        "id": "gpt-oss:120b-cloud",
        "name": "GPT-OSS",
        "version": "120B",
        "tag": "Cloud·120B",
        "speed": "★★★",
        "intelligence": "★★★★★",
        "description": "云端 120B 中继：机构级分析，复杂金融报告",
        "capabilities": ["institutional analysis", "long-form reports", "complex reasoning"],
        "thinking": True, "tools": True,
        "max_tokens": 8192, "num_ctx": 131072, "temperature": 0.3,
        "badge": "Cloud",
    },
    "deepseek-v3-cloud": {
        "id": "deepseek-v3.1:671b-cloud",
        "name": "DeepSeek V3",
        "version": "671B",
        "tag": "Cloud·671B",
        "speed": "★★★",
        "intelligence": "★★★★★",
        "description": "云端 671B 旗舰：最强推理，研报级分析，需订阅",
        "capabilities": ["flagship reasoning", "research report", "quant strategy"],
        "thinking": True, "tools": True,
        "max_tokens": 8192, "num_ctx": 163840, "temperature": 0.3,
        "badge": "Cloud",
    },
}

# Model aliases: short names / Ollama IDs → MODELS key
MODEL_ALIASES = {
    # ── Google Gemini ─────────────────────────────────────────────────
    "gemini": "gemini-flash", "gemini-pro": "gemini-pro", "gpro": "gemini-pro",
    "gemini-flash": "gemini-flash", "gflash": "gemini-flash", "flash": "gemini-flash",
    "gemini-lite": "gemini-lite", "glite": "gemini-lite", "flash-lite": "gemini-lite",
    # ── Qwen 2.5 ──────────────────────────────────────────────────────
    "qwen7b": "qwen7b",   "q7": "qwen7b",   "sonata": "qwen7b",   "s": "qwen7b",
    "qwen14b": "qwen14b", "q14": "qwen14b",
    "qwen32b": "qwen32b", "q32": "qwen32b",
    "qwen-coder": "qwen-coder", "coder": "qwen-coder", "c": "qwen-coder",
    "qwen-fast": "qwen-fast",   "fast": "qwen-fast",   "prelude": "qwen-fast", "p": "qwen-fast",
    # ── Qwen 3 ────────────────────────────────────────────────────────
    "qwen3": "qwen3-8b",     "q3": "qwen3-8b",    "qwen3-8b": "qwen3-8b",
    "qwen3-30b": "qwen3-30b", "q3-moe": "qwen3-30b", "moe": "qwen3-30b",
    # ── DeepSeek ──────────────────────────────────────────────────────
    "deepseek-r1": "deepseek-r1", "r1": "deepseek-r1", "r1-7b": "deepseek-r1",
    "r1-1.5b": "deepseek-r1-1.5b", "r1-tiny": "deepseek-r1-1.5b",
    "r1-14b": "deepseek-r1-14b",  "r1-pro": "deepseek-r1-14b",
    # ── Llama ─────────────────────────────────────────────────────────
    "llama3.2": "llama3.2-3b",    "llama3": "llama3.2-3b",    "l3": "llama3.2-3b",
    "llama3.1": "llama3.1-8b",    "llama3.1-8b": "llama3.1-8b", "l31": "llama3.1-8b",
    "llama3.3": "llama3.3-70b",   "llama70b": "llama3.3-70b", "l33": "llama3.3-70b",
    # ── Mistral ───────────────────────────────────────────────────────
    "mistral": "mistral-7b",   "m7": "mistral-7b",
    "nemo": "mistral-nemo",    "mistral12b": "mistral-nemo",
    # ── Phi (Microsoft) ───────────────────────────────────────────────
    "phi4": "phi4",       "phi": "phi4",
    "phi4-mini": "phi4-mini", "phi-mini": "phi4-mini",
    # ── Gemma (Google) ────────────────────────────────────────────────
    "gemma": "gemma3-4b",    "gemma3": "gemma3-4b",   "g4": "gemma3-4b",
    "gemma12b": "gemma3-12b", "gemma3-12b": "gemma3-12b",
    # ── Cloud relay ───────────────────────────────────────────────────
    "gpt-oss": "gpt-oss-120b", "120b": "gpt-oss-120b",
    "deepseek-v3": "deepseek-v3-cloud", "v3": "deepseek-v3-cloud", "671b": "deepseek-v3-cloud",
    # ── 旧名向后兼容 ──────────────────────────────────────────────────
    "sonata-thinking": "deepseek-r1", "st": "deepseek-r1",
    "sonata-verbose":  "qwen7b",      "sv": "qwen7b",
    # ── Ollama model ID → registry key ────────────────────────────────
    "qwen2.5:7b":                "qwen7b",
    "qwen2.5:14b":               "qwen14b",
    "qwen2.5:32b":               "qwen32b",
    "qwen2.5:3b":                "qwen-fast",
    "qwen2.5-coder:7b":          "qwen-coder",
    "qwen2.5-coder:14b":         "qwen-coder",
    "qwen2.5-coder:1.5b":        "qwen-fast",
    "qwen3:8b":                  "qwen3-8b",
    "qwen3:30b-a3b":             "qwen3-30b",
    "deepseek-r1:7b":            "deepseek-r1",
    "deepseek-r1:1.5b":          "deepseek-r1-1.5b",
    "deepseek-r1:14b":           "deepseek-r1-14b",
    "llama3.2:3b":               "llama3.2-3b",
    "llama3.1:8b":               "llama3.1-8b",
    "llama3.3:70b":              "llama3.3-70b",
    "mistral:7b":                "mistral-7b",
    "mistral-nemo:12b":          "mistral-nemo",
    "phi4:14b":                  "phi4",
    "phi4-mini:3.8b":            "phi4-mini",
    "gemma3:4b":                 "gemma3-4b",
    "gemma3:12b":                "gemma3-12b",
    "gpt-oss:120b-cloud":        "gpt-oss-120b",
    "deepseek-v3.1:671b-cloud":  "deepseek-v3-cloud",
    # ── 旧 aria 模型 ID ───────────────────────────────────────────────
    "aria-sonata:4.5":           "qwen7b",
    "aria-sonata:4.5-thinking":  "deepseek-r1",
    "aria-sonata:4.5-verbose":   "qwen7b",
    "aria-sonata:4.6":           "qwen7b",
    "aria-sonata:4.6-thinking":  "deepseek-r1",
    "aria-prelude:4.3":          "qwen-fast",
    "aria-prelude:1.5b":         "qwen-fast",
}

# ── 模型降级优先级（单一事实源：预检 / 运行时 fallback 共用）────────────────
# 按能力/稳定性排序：先选大容量本地模型，再退化到轻量模型
_MODEL_FALLBACK_PREFIXES = [
    # 首选：7B+ 本地全能模型
    "qwen3:8b",            # Qwen3 最新，混合推理
    "qwen3:30b-a3b",       # Qwen3 MoE，快速
    "qwen2.5:14b",         # Qwen2.5 高质量
    "qwen2.5:7b",          # Qwen2.5 主力
    "qwen2.5-coder:7b",    # 代码专精
    # 次选：其他家族本地模型
    "llama3.3:70b",        # Meta 旗舰（需大 VRAM）
    "llama3.1:8b",         # Meta 8B 稳定
    "mistral-nemo:12b",    # Mistral 12B
    "mistral:7b",          # Mistral 7B
    "phi4:14b",            # Microsoft Phi-4
    "phi4-mini:3.8b",      # Microsoft Phi-4 Mini
    "gemma3:12b",          # Google Gemma 12B
    "gemma3:4b",           # Google Gemma 4B
    "deepseek-r1:14b",     # DeepSeek R1 推理
    "deepseek-r1:7b",      # DeepSeek R1 7B
    # 轻量回落
    "qwen2.5-coder:3b",    # 小模型
    "qwen2.5:3b",          # 小模型
    "llama3.2:3b",         # Meta 轻量
    "deepseek-r1:1.5b",    # 极小推理
    # Cloud relay（需订阅）
    "gpt-oss",
    "deepseek-v3.1",
]


# Gemini models Google retired or Aria stopped serving, and their replacement
# (the same table as Arthera's aria_model_registry.RETIRED_MODELS).
RETIRED_GEMINI_MODELS = {
    "gemini-2.5-flash": "gemini-3.8-flash",
    "gemini-2.5-pro": "gemini-3.1-pro-preview",
    "gemini-2.5-flash-lite": "gemini-3.5-flash-lite",
    "gemini-2.0-flash": "gemini-3.8-flash",
    "gemini-2.0-flash-exp": "gemini-3.8-flash",
    "gemini-1.5-flash": "gemini-3.8-flash",
    "gemini-1.5-pro": "gemini-3.1-pro-preview",
    "gemini-3-flash-preview": "gemini-3.8-flash",
    "gemini-3.5-flash": "gemini-3.8-flash",
    "gemini-3.1-flash-lite": "gemini-3.5-flash-lite",
    "gemini-3-pro-preview": "gemini-3.1-pro-preview",
}


def current_gemini_model(model: str) -> str:
    """The model that serves `model` now, keeping any provider prefix ("google/")."""
    prefix, _, bare = model.rpartition("/")
    replacement = RETIRED_GEMINI_MODELS.get(bare)
    if not replacement:
        return model
    return f"{prefix}/{replacement}" if prefix else replacement
