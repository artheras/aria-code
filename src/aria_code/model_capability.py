"""
model_capability.py — Aria Code model capability registry & tool-call adapter.

Responsibilities:
  - Know which local models support native tool calling vs text-based <tool_call>
  - Normalise tool call output across Ollama-native / XML-tag / JSON-fenced formats
  - Inject the correct tool schema format into the Ollama payload
  - Detect capability dynamically when a model is not in the registry

Usage::

    from aria_code.model_capability import get_model_capability, build_ollama_tool_payload

    caps = get_model_capability("qwen2.5-coder:7b")
    # {"tool_calls": True, "format": "ollama_native", "context_window": 32768, ...}

    calls = parse_tool_calls_from_response(full_text, native_tool_calls)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Capability catalogue
# ---------------------------------------------------------------------------

@dataclass
class ModelCapability:
    # Whether the model reliably produces structured tool calls
    tool_calls: bool = False
    # "ollama_native"  → Ollama's message.tool_calls list
    # "xml_tags"       → <tool_call>{"name":…,"arguments":{…}}</tool_call>
    # "json_fence"     → ```json\n{"tool":…,"arguments":{…}}\n```
    # "anthropic_native" → tool calling via the Anthropic SDK
    # "openai_native"  → tool calling via an OpenAI-compatible / Vertex endpoint
    #                    (OpenAI, Gemini, Grok) — structured `tool_calls` in the
    #                    response, not parsed out of the text stream
    # "text_only"      → no tool calling; prompt must ask for plain-text answers
    # "router_only"    → model is only suitable for intent classification / routing;
    #                    MUST NOT handle coding, analysis, or multi-step tasks
    format: str = "text_only"
    context_window: int = 8192
    thinking: bool = False          # extended-reasoning / <think> tokens
    vision: bool = False            # supports image / multimodal input
    finance_tuned: bool = False     # model has finance-domain fine-tuning
    # Recommended sampling params
    temperature: float = 0.3
    top_p: float = 0.9
    # Maximum simultaneous tool calls per round (safety limit)
    max_parallel_tools: int = 1
    # Minimum model size class: "nano" <1B / "small" 1-4B / "medium" 4-14B / "large" >14B
    size_class: str = "medium"
    # Extra notes shown in /models list
    notes: str = ""


def is_router_only(cap: "ModelCapability") -> bool:
    """Return True if this model must NOT handle complex tasks (coding/analysis)."""
    return cap.format == "router_only"


def can_handle_coding(cap: "ModelCapability") -> bool:
    """Return True if the model is large/capable enough for code generation tasks."""
    return (
        cap.format in ("ollama_native", "xml_tags", "anthropic_native", "openai_native")
        and cap.context_window >= 8192
        and cap.size_class not in ("nano",)
    )


def can_handle_analysis(cap: "ModelCapability") -> bool:
    """Return True if the model can handle multi-step financial analysis."""
    return (
        cap.format not in ("router_only",)
        and cap.context_window >= 4096
        and cap.size_class not in ("nano",)
    )


# Prefix → capability mapping.  Longest prefix wins.
_CAPABILITY_TABLE: Dict[str, ModelCapability] = {
    # ── Qwen family ────────────────────────────────────────────────────────
    "qwen2.5-coder:32b":    ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.2, size_class="large",  notes="Best local code+finance model"),
    "qwen2.5-coder:14b":    ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.2, size_class="large"),
    "qwen2.5-coder:7b":     ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.2, size_class="medium"),
    "qwen2.5-coder:3b":     ModelCapability(tool_calls=True,  format="ollama_native", context_window=32768,  temperature=0.3, size_class="small"),
    # 1.5B — too small for reliable tool calls; use text_only to prevent JSON hallucination
    "qwen2.5-coder:1.5b":  ModelCapability(tool_calls=False, format="text_only",     context_window=8192,   temperature=0.4, size_class="small", notes="1.5B — no tools; simple Q&A only"),
    # 0.5B — nano class; only suitable for routing/classification, never complex tasks
    "qwen2.5-coder:0.5b":  ModelCapability(tool_calls=False, format="router_only",   context_window=4096,   temperature=0.5, size_class="nano",  notes="0.5B nano — routing/intent only"),
    "qwen2.5-coder":        ModelCapability(tool_calls=True,  format="ollama_native", context_window=32768,  temperature=0.2, size_class="medium"),
    "qwen2.5:72b":          ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.2, notes="Strongest Qwen general model"),
    "qwen2.5:32b":          ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.2),
    "qwen2.5:14b":          ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.2),
    "qwen2.5:7b":           ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3),
    "qwen2.5":              ModelCapability(tool_calls=True,  format="ollama_native", context_window=32768,  temperature=0.3),
    "qwen3":                ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3, thinking=True),
    "qwq":                  ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3, thinking=True, notes="Math/reasoning focused"),
    # ── DeepSeek family ────────────────────────────────────────────────────
    "deepseek-r1:671b":     ModelCapability(tool_calls=False, format="xml_tags",      context_window=131072, temperature=0.3, thinking=True, notes="State-of-the-art reasoning"),
    "deepseek-r1:70b":      ModelCapability(tool_calls=False, format="xml_tags",      context_window=131072, temperature=0.3, thinking=True),
    "deepseek-r1:32b":      ModelCapability(tool_calls=False, format="xml_tags",      context_window=32768,  temperature=0.3, thinking=True),
    "deepseek-r1:14b":      ModelCapability(tool_calls=False, format="xml_tags",      context_window=32768,  temperature=0.3, thinking=True),
    "deepseek-r1:8b":       ModelCapability(tool_calls=False, format="xml_tags",      context_window=32768,  temperature=0.3, thinking=True),
    "deepseek-r1:7b":       ModelCapability(tool_calls=False, format="xml_tags",      context_window=32768,  temperature=0.3, thinking=True),
    "deepseek-r1":          ModelCapability(tool_calls=False, format="xml_tags",      context_window=32768,  temperature=0.3, thinking=True),
    "deepseek-v3.1":        ModelCapability(tool_calls=True,  format="ollama_native", context_window=163840, temperature=0.3, thinking=True, notes="DeepSeek V3.1 671B"),
    "deepseek-v3":          ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3),
    "deepseek-v2.5":        ModelCapability(tool_calls=True,  format="ollama_native", context_window=65536,  temperature=0.3),
    "deepseek-coder-v2":    ModelCapability(tool_calls=True,  format="ollama_native", context_window=65536,  temperature=0.2),
    # DeepSeek's own API (api.deepseek.com). Current names are deepseek-flash and
    # deepseek-v4-pro; deepseek-v4-flash is a legacy alias still accepted. Third-party
    # listings give these 1M tokens; the official pages do not state it, so the window
    # here is a conservative 128K (an underestimate only means earlier compaction).
    "deepseek-v4":          ModelCapability(tool_calls=True,  format="openai_native", context_window=131072, temperature=0.3, size_class="large"),
    "deepseek-flash":       ModelCapability(tool_calls=True,  format="openai_native", context_window=131072, temperature=0.3, size_class="medium"),
    # ── LLaMA family ───────────────────────────────────────────────────────
    "llama3.3:70b":         ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3, notes="Meta flagship 2024"),
    "llama3.2:90b":         ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3, vision=True,  notes="Multimodal, image+text"),
    "llama3.2:11b":         ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3, vision=True,  notes="Multimodal, image+text"),
    "llama3.2:3b":          ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3),
    "llama3.2":             ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3),
    "llama3.1:405b":        ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3),
    "llama3.1:70b":         ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3),
    "llama3.1:8b":          ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3),
    "llama3.1":             ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3),
    "llama3":               ModelCapability(tool_calls=False, format="text_only",     context_window=8192,   temperature=0.3),
    # ── Mistral family ─────────────────────────────────────────────────────
    "mistral-nemo":         ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3),
    "mistral-large":        ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3),
    "mistral-small":        ModelCapability(tool_calls=True,  format="ollama_native", context_window=32768,  temperature=0.3),
    "mistral:7b":           ModelCapability(tool_calls=True,  format="ollama_native", context_window=32768,  temperature=0.3),
    "mistral":              ModelCapability(tool_calls=True,  format="ollama_native", context_window=32768,  temperature=0.3),
    "mixtral":              ModelCapability(tool_calls=True,  format="ollama_native", context_window=32768,  temperature=0.3),
    # ── Phi family ─────────────────────────────────────────────────────────
    "phi4:14b":             ModelCapability(tool_calls=True,  format="ollama_native", context_window=16384,  temperature=0.3, notes="Microsoft Phi4, compact+capable"),
    "phi4":                 ModelCapability(tool_calls=True,  format="ollama_native", context_window=16384,  temperature=0.3),
    "phi3.5":               ModelCapability(tool_calls=True,  format="ollama_native", context_window=16384,  temperature=0.3),
    "phi3":                 ModelCapability(tool_calls=False, format="text_only",     context_window=8192,   temperature=0.3),
    # ── Google Gemma ───────────────────────────────────────────────────────
    "gemma3:27b":           ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3, vision=True),
    "gemma3:12b":           ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3, vision=True),
    "gemma3:4b":            ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3, vision=True),
    "gemma3":               ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3, vision=True),
    "gemma2":               ModelCapability(tool_calls=False, format="text_only",     context_window=8192,   temperature=0.3),
    # ── Vision / multimodal models ─────────────────────────────────────────────
    "llava:34b":            ModelCapability(tool_calls=False, format="text_only",     context_window=4096,   temperature=0.3, vision=True,  notes="LLaVA 34B vision-language"),
    "llava:13b":            ModelCapability(tool_calls=False, format="text_only",     context_window=4096,   temperature=0.3, vision=True),
    "llava:7b":             ModelCapability(tool_calls=False, format="text_only",     context_window=4096,   temperature=0.3, vision=True),
    "llava":                ModelCapability(tool_calls=False, format="text_only",     context_window=4096,   temperature=0.3, vision=True),
    "bakllava":             ModelCapability(tool_calls=False, format="text_only",     context_window=4096,   temperature=0.3, vision=True),
    "moondream":            ModelCapability(tool_calls=False, format="text_only",     context_window=2048,   temperature=0.3, vision=True,  size_class="small", notes="Tiny vision model"),
    "minicpm-v":            ModelCapability(tool_calls=False, format="text_only",     context_window=8192,   temperature=0.3, vision=True,  size_class="small"),
    "qwen2-vl:72b":         ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3, vision=True,  notes="Qwen2-VL 72B multimodal"),
    "qwen2-vl:7b":          ModelCapability(tool_calls=True,  format="ollama_native", context_window=32768,  temperature=0.3, vision=True),
    "qwen2-vl":             ModelCapability(tool_calls=True,  format="ollama_native", context_window=32768,  temperature=0.3, vision=True),
    "qwen2.5vl:72b":        ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3, vision=True,  notes="Qwen2.5-VL 72B"),
    "qwen2.5vl:7b":         ModelCapability(tool_calls=True,  format="ollama_native", context_window=32768,  temperature=0.3, vision=True),
    "qwen2.5vl":            ModelCapability(tool_calls=True,  format="ollama_native", context_window=32768,  temperature=0.3, vision=True),
    # ── Finance-specific ───────────────────────────────────────────────────
    "finma":                ModelCapability(tool_calls=False, format="text_only",     context_window=4096,   temperature=0.3, finance_tuned=True),
    "fingpt":               ModelCapability(tool_calls=False, format="text_only",     context_window=4096,   temperature=0.3, finance_tuned=True),
    "bloomberggpt":         ModelCapability(tool_calls=False, format="text_only",     context_window=2048,   temperature=0.2, finance_tuned=True),
    # ── Aria own models ────────────────────────────────────────────────────
    # aria-sonata-3b: primary local production model — 3B, good for finance Q&A + tool calls
    "aria-sonata-3b":            ModelCapability(tool_calls=False, format="xml_tags", context_window=16384, temperature=0.3, size_class="small", finance_tuned=True, notes="3B local production model"),
    # aria-sonata 1.x/0.5B series — GGUF Ollama models; xml_tags for text-based tool parsing
    "aria-sonata:4.5-thinking":  ModelCapability(tool_calls=False, format="xml_tags", context_window=8192,  temperature=0.3, size_class="small", thinking=True,  finance_tuned=True),
    "aria-sonata:4.5-verbose":   ModelCapability(tool_calls=False, format="xml_tags", context_window=8192,  temperature=0.3, size_class="small",                finance_tuned=True),
    "aria-sonata:4.6-thinking":  ModelCapability(tool_calls=False, format="xml_tags", context_window=8192,  temperature=0.3, size_class="small", thinking=True,  finance_tuned=True),
    "aria-sonata":               ModelCapability(tool_calls=False, format="xml_tags", context_window=8192,  temperature=0.3, size_class="small",                finance_tuned=True),
    # aria-prelude: nano router model — ONLY for intent classification and routing
    "aria-prelude":              ModelCapability(tool_calls=False, format="router_only", context_window=4096, temperature=0.2, size_class="nano", finance_tuned=True, notes="Nano router — intent classification only"),
    # ── Anthropic Claude (cloud API via providers/llm/anthropic.py) ───────
    # format="anthropic_native" → tool calling via Anthropic SDK, not Ollama
    # All Claude 3+ models share 200K context and native vision support.
    "claude-opus-4-8":              ModelCapability(tool_calls=True, format="anthropic_native", context_window=200000, temperature=0.3, size_class="large",  vision=True, thinking=True,  notes="Claude Opus 4.8 — most capable"),
    "claude-opus-4":                ModelCapability(tool_calls=True, format="anthropic_native", context_window=200000, temperature=0.3, size_class="large",  vision=True, thinking=True),
    "claude-sonnet-4-6":            ModelCapability(tool_calls=True, format="anthropic_native", context_window=200000, temperature=0.3, size_class="large",  vision=True),
    "claude-sonnet-4":              ModelCapability(tool_calls=True, format="anthropic_native", context_window=200000, temperature=0.3, size_class="large",  vision=True),
    "claude-haiku-4-5":             ModelCapability(tool_calls=True, format="anthropic_native", context_window=200000, temperature=0.3, size_class="medium", vision=True,               notes="Claude Haiku 4.5 — fast/cheap"),
    "claude-haiku-4":               ModelCapability(tool_calls=True, format="anthropic_native", context_window=200000, temperature=0.3, size_class="medium", vision=True),
    # Claude 3.x legacy — still widely used
    "claude-3-7-sonnet":            ModelCapability(tool_calls=True, format="anthropic_native", context_window=200000, temperature=0.3, size_class="large",  vision=True, thinking=True),
    "claude-3-5-sonnet":            ModelCapability(tool_calls=True, format="anthropic_native", context_window=200000, temperature=0.3, size_class="large",  vision=True),
    "claude-3-5-haiku":             ModelCapability(tool_calls=True, format="anthropic_native", context_window=200000, temperature=0.3, size_class="medium", vision=True),
    "claude-3-opus":                ModelCapability(tool_calls=True, format="anthropic_native", context_window=200000, temperature=0.3, size_class="large",  vision=True, thinking=True),
    "claude-3-sonnet":              ModelCapability(tool_calls=True, format="anthropic_native", context_window=200000, temperature=0.3, size_class="large",  vision=True),
    "claude-3-haiku":               ModelCapability(tool_calls=True, format="anthropic_native", context_window=200000, temperature=0.3, size_class="medium", vision=True),
    # Generic prefix catch-all for future Claude versions (longest-prefix matching ensures
    # specific entries above still win over this fallback)
    "claude":                       ModelCapability(tool_calls=True, format="anthropic_native", context_window=200000, temperature=0.3, size_class="large",  vision=True),
    # ── Arthera cloud-routed models (large, run via cloud API) ────────────
    "gpt-oss:120b":             ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3, thinking=True, notes="GPT-OSS 120B via Ollama Cloud"),
    "gpt-oss":                  ModelCapability(tool_calls=True,  format="ollama_native", context_window=131072, temperature=0.3, thinking=True, notes="GPT-OSS via Ollama Cloud"),
    "deepseek-v3.1:671b-cloud": ModelCapability(tool_calls=True,  format="ollama_native", context_window=163840, temperature=0.3, thinking=True, notes="DeepSeek V3.1 671B via Ollama Cloud"),
    # ── Google Gemini (cloud, via apps/cli/providers/vertexai_stream.py) ──
    # Registered here as well as in the MODELS catalogue: callers such as the
    # tool-routing check in aria_cli use get_model_capability() directly, and a
    # missing entry silently degrades the model to text_only / 4K context.
    "gemini-4-argon":           ModelCapability(tool_calls=True, format="openai_native", context_window=1048576, temperature=0.2, size_class="large",  vision=True, thinking=True, notes="Gemini 4 Argon — flagship, 2026-10 GA (context per third-party reports)"),
    "gemini-3.6-flash":         ModelCapability(tool_calls=True, format="openai_native", context_window=1048576, temperature=0.2, size_class="large",  vision=True, thinking=True, notes="Gemini 3.6 Flash — token-efficient"),
    "gemini-3.1-pro-preview":   ModelCapability(tool_calls=True, format="openai_native", context_window=1048576, temperature=0.2, size_class="large",  vision=True, thinking=True, notes="Gemini 3.1 Pro (preview) — 1M context"),
    "gemini-3.8-flash":         ModelCapability(tool_calls=True, format="openai_native", context_window=1048576, temperature=0.2, size_class="large",  vision=True, thinking=True, notes="Gemini 3.8 Flash — 1M context (default)"),
    "gemini-3.5-flash-lite":    ModelCapability(tool_calls=True, format="openai_native", context_window=1048576, temperature=0.2, size_class="medium", vision=True,                notes="Gemini 3.5 Flash-Lite — 1M context"),
    "gemini-3.5-flash":         ModelCapability(tool_calls=True, format="openai_native", context_window=1048576, temperature=0.2, size_class="large",  vision=True, thinking=True, notes="Gemini 3.5 Flash — 1M context; deprecated by Google 2026-10-08"),
    "gemini-3.1-flash-lite":    ModelCapability(tool_calls=True, format="openai_native", context_window=1048576, temperature=0.2, size_class="medium", vision=True,                notes="Gemini 3.1 Flash-Lite — 1M context"),
    # Retired by Google (model_retirement.py migrates configs that name them).
    "gemini-2.5-pro":           ModelCapability(tool_calls=True, format="openai_native", context_window=1048576, temperature=0.2, size_class="large",  vision=True, thinking=True, notes="Gemini 2.5 Pro — 1M context"),
    "gemini-2.5-flash":         ModelCapability(tool_calls=True, format="openai_native", context_window=1048576, temperature=0.2, size_class="medium", vision=True,                notes="Gemini 2.5 Flash — 1M context"),
    "gemini-2.0-flash":         ModelCapability(tool_calls=True, format="openai_native", context_window=1048576, temperature=0.2, size_class="medium", vision=True),
    "gemini-1.5-pro":           ModelCapability(tool_calls=True, format="openai_native", context_window=2097152, temperature=0.2, size_class="large",  vision=True),
    "gemini-1.5-flash":         ModelCapability(tool_calls=True, format="openai_native", context_window=1048576, temperature=0.2, size_class="medium", vision=True),
    "gemini":                   ModelCapability(tool_calls=True, format="openai_native", context_window=1048576, temperature=0.2, size_class="large",  vision=True),
    # Gemma served on Vertex AI (Model Garden, managed): "gemma-4-…-maas",
    # "gemma-3-…". These used to match nothing and fell to the default — no
    # tools, a 4K window — so a coding task on Gemma was refused as a
    # tool-less route. Ollama's "gemma3:27b" ids keep their own entries above.
    "gemma-4":                  ModelCapability(tool_calls=True, format="openai_native", context_window=131072,  temperature=0.2, size_class="medium", vision=True, notes="Gemma 4 on Vertex AI"),
    "gemma-3":                  ModelCapability(tool_calls=True, format="openai_native", context_window=131072,  temperature=0.2, size_class="medium", vision=True, notes="Gemma 3 on Vertex AI"),
    # ── OpenAI (cloud, via providers/llm/openai_compat.py) ────────────────
    "gpt-5":                    ModelCapability(tool_calls=True, format="openai_native", context_window=400000, temperature=0.2, size_class="large",  vision=True, thinking=True),
    "gpt-4.1":                  ModelCapability(tool_calls=True, format="openai_native", context_window=1047576, temperature=0.2, size_class="large",  vision=True),
    "gpt-4o-mini":              ModelCapability(tool_calls=True, format="openai_native", context_window=128000, temperature=0.2, size_class="medium", vision=True),
    "gpt-4o":                   ModelCapability(tool_calls=True, format="openai_native", context_window=128000, temperature=0.2, size_class="large",  vision=True),
    "gpt-4-turbo":              ModelCapability(tool_calls=True, format="openai_native", context_window=128000, temperature=0.2, size_class="large",  vision=True),
    "gpt-4":                    ModelCapability(tool_calls=True, format="openai_native", context_window=8192,   temperature=0.2, size_class="large"),
    "o4-mini":                  ModelCapability(tool_calls=True, format="openai_native", context_window=200000, temperature=1.0, size_class="medium", thinking=True),
    "o3-mini":                  ModelCapability(tool_calls=True, format="openai_native", context_window=200000, temperature=1.0, size_class="medium", thinking=True),
    "o3":                       ModelCapability(tool_calls=True, format="openai_native", context_window=200000, temperature=1.0, size_class="large",  thinking=True),
    "o1":                       ModelCapability(tool_calls=True, format="openai_native", context_window=200000, temperature=1.0, size_class="large",  thinking=True),
    # NVIDIA Nemotron 3 Super: https://research.nvidia.com/labs/nemotron/Nemotron-3-Super/
    "nemotron-3-super-120b-a12b": ModelCapability(
        tool_calls=True, format="openai_native", context_window=1048576,
        temperature=0.2, size_class="large", thinking=True,
        notes="NVIDIA Nemotron 3 Super; OpenAI-compatible tools verified on Nebius",
    ),
    # ── xAI Grok (cloud, OpenAI-compatible endpoint) ──────────────────────
    "grok-4":                   ModelCapability(tool_calls=True, format="openai_native", context_window=256000, temperature=0.3, size_class="large",  vision=True),
    "grok":                     ModelCapability(tool_calls=True, format="openai_native", context_window=131072, temperature=0.3, size_class="large"),
}

# Provider-qualified ids ("google/gemini-2.5-pro", "openai/gpt-4o") are stripped
# down to the bare model name before prefix matching.  The CLI stores the fully
# qualified id in its config, so without this every cloud model looked unknown.
_VENDOR_PREFIXES = (
    "google/", "openai/", "anthropic/", "x-ai/", "xai/", "meta-llama/",
    "mistralai/", "deepseek/", "qwen/", "alibaba/", "vertex_ai/", "vertexai/",
    "azure/", "bedrock/", "openrouter/", "together/", "groq/", "nvidia/",
)

# Default fallback when model is unknown.  Deliberately conservative: guessing
# tool support wrong breaks every turn.  ``UNKNOWN_MODEL_NOTE`` lets callers
# detect this case and warn the user instead of degrading in silence.
UNKNOWN_MODEL_NOTE = "Unknown model — conservative settings applied"

_DEFAULT_CAPABILITY = ModelCapability(
    tool_calls=False, format="text_only", context_window=4096, temperature=0.5,
    notes=UNKNOWN_MODEL_NOTE,
)


def is_unknown_model(cap: "ModelCapability") -> bool:
    """Return True if *cap* is the unregistered-model fallback.

    A model that is missing from the registry runs without tools and with a 4K
    context budget.  For a cloud model that is a severe, invisible downgrade —
    it cannot read files and the context gauge is computed against the wrong
    denominator — so callers should surface it rather than absorb it.
    """
    return cap.notes == UNKNOWN_MODEL_NOTE


def strip_vendor_prefix(model_name: str) -> str:
    """Strip a provider prefix ("google/gemini-2.5-pro" → "gemini-2.5-pro")."""
    name = (model_name or "").strip().lower()
    for prefix in _VENDOR_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def get_model_capability(model_name: str) -> ModelCapability:
    """
    Return capability for *model_name* using longest-prefix matching.

    Provider-qualified ids are normalised first, so the fully qualified id the
    CLI stores in its config resolves to the same entry as the bare name.

    Examples::

        get_model_capability("qwen2.5-coder:7b-instruct-q4_K_M")
        # → same as "qwen2.5-coder:7b" entry

        get_model_capability("google/gemini-2.5-pro")
        # → same as "gemini-2.5-pro" entry
    """
    name = strip_vendor_prefix(model_name)
    # Strip GGUF quantisation suffixes like :q4_k_m, :f16, etc.
    clean = re.sub(r":[qfQ][0-9].*$", "", name)
    # Also strip common instruct/chat/gguf tags appended after the size
    clean = re.sub(r"-(instruct|chat|gguf|base|it)$", "", clean)
    # Cloud ids carry a dated or "-latest" suffix ("gemini-2.5-pro-preview-06-05",
    # "claude-3-5-sonnet-20241022") that must not defeat prefix matching.
    clean = re.sub(r"-(latest|preview|exp)(-[0-9-]+)?$", "", clean)

    best_prefix = ""
    best_cap = _DEFAULT_CAPABILITY
    for prefix, cap in _CAPABILITY_TABLE.items():
        if clean.startswith(prefix.lower()) and len(prefix) > len(best_prefix):
            best_prefix = prefix
            best_cap = cap

    return best_cap


# ---------------------------------------------------------------------------
# Tool schema injection helpers
# ---------------------------------------------------------------------------

def build_ollama_tool_payload(
    tools_schema: List[Dict],
    model_name: str,
) -> Optional[List[Dict]]:
    """
    Return the `tools` field for an Ollama /api/chat request, or None when the
    model does not support native tool calling.

    When format == "ollama_native" the schema is passed as-is.
    When format == "xml_tags" we skip the field and rely on prompt injection.
    """
    cap = get_model_capability(model_name)
    if not cap.tool_calls or cap.format != "ollama_native":
        return None
    return tools_schema


def build_tool_system_prompt(
    tools_schema: List[Dict],
    model_name: str,
) -> str:
    """
    For models that do NOT support native tool calls (xml_tags / text_only),
    return a system-prompt block that instructs the model to emit
    ``<tool_call>{"name":…,"arguments":{…}}</tool_call>`` tags.
    """
    cap = get_model_capability(model_name)
    if cap.tool_calls and cap.format == "ollama_native":
        return ""  # handled by native API

    tool_list = []
    for t in tools_schema:
        fn = t.get("function", t)
        params = fn.get("parameters", {}).get("properties", {})
        required = fn.get("parameters", {}).get("required", [])
        param_str = ", ".join(
            f"{k}: {v.get('type','any')}{'*' if k in required else ''}"
            for k, v in params.items()
        )
        tool_list.append(f"  - {fn['name']}({param_str}): {fn.get('description','')}")

    tools_block = "\n".join(tool_list)
    return (
        "\n\n## Available Tools\n\n"
        "When you need to call a tool, output EXACTLY this format (nothing else on the line):\n\n"
        '<tool_call>{"name": "tool_name", "arguments": {"param": "value"}}</tool_call>\n\n'
        "Tools available:\n"
        f"{tools_block}\n\n"
        "Rules:\n"
        "1. Call ONE tool at a time. Wait for the result before calling the next.\n"
        "2. After receiving a tool result, continue your analysis or call another tool.\n"
        "3. When done with all tools, write your final answer in plain text.\n"
        "4. Never make up tool results — always call the tool.\n"
    )


# ---------------------------------------------------------------------------
# Tool call parsers
# ---------------------------------------------------------------------------

def parse_tool_calls_from_response(
    text: str,
    native_calls: Optional[List[Dict]] = None,
    model_name: str = "",
) -> List[Dict[str, Any]]:
    """
    Unified parser.  Returns list of {"tool": str, "params": dict}.

    Priority:
      1. native_calls (Ollama tool_calls list) — most reliable
      2. XML tags  <tool_call>…</tool_call>
      3. JSON code fences  ```json … ```
      4. Raw JSON object containing "name"/"arguments" keys
    """
    # 1. Native Ollama tool calls
    if native_calls:
        result = []
        for tc in native_calls:
            fn = tc.get("function", tc)
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if name:
                result.append({"tool": name, "params": args})
        if result:
            return result

    if not text:
        return []

    results: List[Dict[str, Any]] = []

    # 2. XML tag format: <tool_call>…</tool_call>
    xml_pattern = re.compile(
        r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE
    )
    for m in xml_pattern.finditer(text):
        tc = _try_parse_json(m.group(1))
        if tc:
            results.append(_normalise_call(tc))

    if results:
        return results

    # 3. JSON fence: ```json … ``` or ``` … ```
    fence_pattern = re.compile(
        r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE
    )
    for m in fence_pattern.finditer(text):
        tc = _try_parse_json(m.group(1))
        if tc and ("name" in tc or "tool" in tc):
            results.append(_normalise_call(tc))

    if results:
        return results

    # 4. Bare JSON object anywhere in text
    json_pattern = re.compile(r"\{[^{}]*\"(?:name|tool)\"[^{}]*\}", re.DOTALL)
    for m in json_pattern.finditer(text):
        tc = _try_parse_json(m.group(0))
        if tc and ("name" in tc or "tool" in tc):
            results.append(_normalise_call(tc))

    return results


def _try_parse_json(s: str) -> Optional[Dict]:
    try:
        return json.loads(s.strip())
    except (json.JSONDecodeError, TypeError):
        return None


def _normalise_call(tc: Dict) -> Dict[str, Any]:
    """Normalise various key naming conventions → {"tool": …, "params": …}."""
    name = tc.get("name") or tc.get("tool") or tc.get("function", {}).get("name", "")
    args = (
        tc.get("arguments")
        or tc.get("params")
        or tc.get("parameters")
        or tc.get("function", {}).get("arguments", {})
        or {}
    )
    if isinstance(args, str):
        args = _try_parse_json(args) or {}
    return {"tool": name, "params": args}


# ---------------------------------------------------------------------------
# Recommended local models for finance work
# ---------------------------------------------------------------------------

RECOMMENDED_FINANCE_MODELS: List[Dict[str, str]] = [
    {
        "model":       "qwen2.5-coder:7b",
        "reason":      "Best balance of tool calling + code generation for finance scripts",
        "install":     "ollama pull qwen2.5-coder:7b",
        "vram_gb":     "5",
    },
    {
        "model":       "qwen2.5:14b",
        "reason":      "Strong quantitative reasoning, multi-turn strategy analysis",
        "install":     "ollama pull qwen2.5:14b",
        "vram_gb":     "10",
    },
    {
        "model":       "deepseek-r1:14b",
        "reason":      "Deep reasoning for complex factor models (slow but thorough)",
        "install":     "ollama pull deepseek-r1:14b",
        "vram_gb":     "10",
    },
    {
        "model":       "llama3.2:3b",
        "reason":      "Ultra-fast for quick quotes and simple questions",
        "install":     "ollama pull llama3.2:3b",
        "vram_gb":     "2",
    },
    {
        "model":       "phi4:14b",
        "reason":      "Math-strong, good for Greeks / derivative pricing",
        "install":     "ollama pull phi4",
        "vram_gb":     "9",
    },
]
