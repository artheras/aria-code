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
    # Gemini 2.5 retires in October 2026 (model_retirement.py). Google's release
    # notes (checked 2026-10-19) deprecated gemini-3.5-flash on 8 October and
    # route it to 3.6 Flash, so it still answers; the newer ids are listed
    # beside it, and /model also lists everything the provider serves today.
    "gemini-flash": {
        "id": "google/gemini-3.5-flash",
        "name": "Gemini Flash",
        "version": "3.5",
        "tag": "Platform",
        "speed": "★★★★★",
        "intelligence": "★★★★★",
        "description": "平台默认（Google 已于 2026-10-08 标记弃用并转到 3.6 Flash，仍可用）、百万级上下文",
        "capabilities": ["multimodal", "1M context", "tool calls", "quant", "code"],
        "thinking": True, "tools": True,
        "max_tokens": 8192, "num_ctx": 1048576, "temperature": 0.2,
        "badge": "Default",
    },
    "gemini-pro": {
        "id": "google/gemini-3.8-flash",
        "name": "Gemini 3.8 Flash",
        "version": "3.8",
        "tag": "Platform",
        "speed": "★★★★★",
        "intelligence": "★★★★★",
        "description": "Flash 旗舰（2026-09 GA）：工程与智能体工作流，推理与工具调用强",
        "capabilities": ["multimodal", "1M context", "tool calls", "quant", "code"],
        "thinking": True, "tools": True,
        "max_tokens": 8192, "num_ctx": 1048576, "temperature": 0.2,
        "badge": "Latest",
    },
    "gemini-4": {
        "id": "google/gemini-4-argon",
        "name": "Gemini 4 Argon",
        "version": "4",
        "tag": "Platform",
        "speed": "★★★★",
        "intelligence": "★★★★★",
        "description": "Google 最新旗舰（2026-10-13 GA）：复杂编码、智能体、数学与科学；比 Flash 贵",
        "capabilities": ["multimodal", "1M context", "tool calls", "quant", "code"],
        "thinking": True, "tools": True,
        "max_tokens": 8192, "num_ctx": 1048576, "temperature": 0.2,
        "badge": "Flagship",
    },
    "gemini-lite": {
        "id": "google/gemini-3.5-flash-lite",
        "name": "Gemini Flash-Lite",
        "version": "3.5",
        "tag": "Platform",
        "speed": "★★★★★",
        "intelligence": "★★★★",
        "description": "极速低价：低延迟、适合批量与实时行情",
        "capabilities": ["fast inference", "1M context", "tool calls", "low latency"],
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
    "gemini": "gemini-flash", "gemini-pro": "gemini-pro", "gemini2.5": "gemini-flash", "gpro": "gemini-pro",
    "gemini-latest": "gemini-pro", "glatest": "gemini-pro",
    "gemini-flash": "gemini-flash", "gflash": "gemini-flash", "flash": "gemini-flash",
    "gemini-lite": "gemini-lite", "glite": "gemini-lite",
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


# ── Lookups ───────────────────────────────────────────────────────────────────
# These two read MODELS / MODEL_ALIASES and nothing else from aria_cli, so they
# belong beside the data rather than in the CLI module. Four mixins were
# importing them from aria_cli purely because that is where they were written.
#
# model_capability is optional, guarded the same way aria_cli guards it. The
# bare import form is deliberate: it is the form every other caller in the tree
# uses, and the package is reachable under two roots whose module objects are
# distinct.
try:
    from model_capability import get_model_capability

    _HAS_MODEL_CAP = True
except ImportError:  # pragma: no cover - exercised only without model_capability
    get_model_capability = None  # type: ignore[assignment]
    _HAS_MODEL_CAP = False


_MODEL_ID_INDEX: dict[str, str] | None = None


def _model_id_index() -> dict[str, str]:
    """Lazily build a lowercase {model id -> MODELS key} index.

    The config stores the provider-qualified *id* ("google/gemini-3.5-flash")
    while MODELS is keyed by short name ("gemini-pro"), so without this lookup
    a registered model resolves to "_community_" and its own registry entry is
    never used.

    Measured, rather than assumed: when model_capability knows the model it
    guesses the same tools/context, so the visible damage is limited. The real
    exposure is a model model_capability does not know, or an install without
    it at all (_HAS_MODEL_CAP False), where get_model_cfg falls back to
    sonata's settings for an unrelated model.
    """
    global _MODEL_ID_INDEX
    if _MODEL_ID_INDEX is None:
        index: dict[str, str] = {}
        for key, cfg in MODELS.items():
            model_id = str(cfg.get("id") or "").strip().lower()
            if model_id and model_id not in index:
                index[model_id] = key
        _MODEL_ID_INDEX = index
    return _MODEL_ID_INDEX


def resolve_model_key(model_str: str) -> str:
    """Resolve any model alias/ID/key to a MODELS key.

    Lookup order: exact key -> alias -> registered model id (case-insensitive).

    For community Ollama models (qwen2.5-coder, llama3.2, deepseek-r1, etc.)
    that are NOT in the MODELS registry, returns the sentinel "_community_"
    so callers know to use model_capability.get_model_capability() instead
    of falling back to hardcoded "prelude" settings.
    """
    if model_str in MODELS:
        return model_str
    if model_str in MODEL_ALIASES:
        return MODEL_ALIASES[model_str]
    normalized = str(model_str or "").strip().lower()
    if normalized in MODELS:
        return normalized
    if normalized in MODEL_ALIASES:
        return MODEL_ALIASES[normalized]
    mapped = _model_id_index().get(normalized)
    if mapped:
        return mapped
    # Community/custom Ollama model — not in registry
    return "_community_"


def get_model_cfg(model_str: str) -> dict:
    """Return the best available config dict for *model_str*.

    For registered models (MODELS table): returns the table entry.
    For community Ollama models: synthesizes a config from model_capability.
    Never silently falls back to 'prelude' settings for an unrelated model.
    """
    key = resolve_model_key(model_str)
    if key in MODELS:
        return MODELS[key]
    # Community model — build config from model_capability registry
    if _HAS_MODEL_CAP:
        cap = get_model_capability(model_str)
        return {
            "id":          model_str,
            "name":        model_str,
            "num_ctx":     cap.context_window,
            "temperature": cap.temperature,
            "max_tokens":  min(cap.context_window // 4, 8192),
            "thinking":    cap.thinking,
            "tools":       cap.tool_calls,
        }
    # Last resort fallback — use qwen7b (sonata) settings as a safe default
    return MODELS.get("sonata", MODELS.get("qwen7b", next(iter(MODELS.values()))))


# ── Choosing a local model when the configured one is not installed ──────────
# This used to live in aria_cli as _pick_best_installed_model, and it ended with
# `return sorted(installed)[0]`. Two things went wrong with that.
#
# First, the caller in print_header() asks "is config['model'] in the set Ollama
# reports?" — and a cloud id is NEVER in that set, because Ollama only lists
# local models. So for anyone running google/gemini-2.5-pro (the shipped
# default) with Ollama installed, the answer was always no, the "heal" branch
# always fired, and print_header() then persisted the result with save_config().
# A deliberate `/model google/gemini-2.5-pro` was overwritten on every startup.
#
# Second, the alphabetical last resort answered with whatever sorted first.
# Measured before the fix, with google/gemini-2.5-pro configured:
#
#   installed {qwen2.5:7b, llama3.2:3b}            -> qwen2.5:7b
#   installed {aardvark-tiny:1b, zzz-exp:0.5b}     -> aardvark-tiny:1b
#   installed {llama3.2:1b}, claude-sonnet-4 cfg   -> llama3.2:1b
#
# A 0.5B model that sorts early becoming the agent's brain is not a fallback,
# it is a silent downgrade to something that cannot hold a tool call together.
#
# So: cloud choices are never healed from a local list, and a model only
# qualifies if the capability registry says it can handle coding. When nothing
# qualifies, this returns None and the caller leaves the configuration alone.

def is_provider_qualified(model: str) -> bool:
    """True for ids that name their own provider ("google/gemini-2.5-pro").

    Ollama serves namespaced community models such as "hf.co/user/model", whose
    first segment is a host rather than a backend — those are local, so the
    check is against the known provider prefixes, not merely against "/".
    """
    name = (model or "").strip().lower()
    if "/" not in name:
        return False
    head = name.split("/", 1)[0]
    return head in _KNOWN_PROVIDER_PREFIXES


_KNOWN_PROVIDER_PREFIXES = frozenset({
    "openai", "anthropic", "google", "xai", "deepseek", "groq", "together",
    "dashscope", "lmstudio", "siliconflow", "moonshot", "zhipu", "mistral",
    "cohere", "perplexity", "baidu", "ernie", "qianfan", "ollama",
})


def _is_capable_enough(model: str) -> bool:
    """True if the capability registry vouches for *model* on coding work."""
    if not _HAS_MODEL_CAP:
        # Without the registry there is no basis for vouching, so don't.
        return False
    from model_capability import can_handle_coding, is_unknown_model

    cap = get_model_capability(model)
    return not is_unknown_model(cap) and can_handle_coding(cap)


def pick_best_installed_model(installed, preferred: str = "") -> str | None:
    """Pick a local model to use when *preferred* is not installed.

    Returns None — meaning "leave the configuration alone" — when *preferred*
    names a cloud provider, or when no installed model is one the capability
    registry vouches for.
    """
    if not installed:
        return None
    if preferred and preferred in installed:
        return preferred
    # A cloud model's absence from the local list says nothing about the user's
    # intent, so it is not grounds for switching them to a local model.
    if is_provider_qualified(preferred):
        return None
    for prefix in _MODEL_FALLBACK_PREFIXES:
        match = next((m for m in sorted(installed) if m.startswith(prefix)), None)
        if match:
            return match
    # No curated match. Take a capable installed model if there is one, in a
    # deterministic order; otherwise decline rather than guess.
    capable = sorted(m for m in installed if _is_capable_enough(m))
    return capable[0] if capable else None
