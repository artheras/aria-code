"""image_gen_tools.py — registers real (non-financial) image generation as
LOCAL_TOOLS entries for aria-code's own interactive chat loop.

Real bug this fixes: local_image_provider.py / openai_image_client.py
already exist and are already exposed *outward* via the MCP server
(packages/aria_mcp/server.py) for other clients (Claude Code, Codex) to
call into aria-code — but they were never registered as something
aria-code's own chat loop can call on itself. Confirmed in a live session:
a user asked for a landscape photo and the assistant replied it could only
generate stock charts, because `stock_chart` was literally the only image
tool in its own registry (aria_cli.py's LOCAL_TOOLS) — the model wasn't
lying about its own tools, it genuinely didn't have this one.

Local/free only here, by design: the OpenAI-backed higher-quality path has
a real per-call cost and its own confirmed=true gate — that one stays
MCP-only rather than being wired into the interactive loop's tool-calling,
where a model could invoke it without the same terminal-side cost
visibility the MCP confirm gate assumes.
"""

from __future__ import annotations

from typing import Any, Dict, List


def tool_generate_image(params: Dict[str, Any]) -> Dict[str, Any]:
    """LOCAL_TOOLS entry. params: {prompt, width?, height?, aspect_ratio?, model?, provider?}."""
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return {"success": False, "error": "缺少 prompt"}

    # If Google Cloud credentials are configured and provider is not forced to local, prefer Google Cloud Gemini image models
    provider = str(params.get("provider") or "").lower()
    if provider != "local":
        try:
            from aria_code.providers.google_imagen_provider import (
                has_google_imagen_config,
                generate_image_google,
            )
            if has_google_imagen_config() or provider in ("google", "vertex", "imagen"):
                aspect_ratio = str(params.get("aspect_ratio") or "1:1")
                model = str(params.get("model") or "gemini-3.1-flash-image")
                res = generate_image_google(prompt, model=model, aspect_ratio=aspect_ratio)
                if res.get("success"):
                    return res
        except Exception:
            pass

    try:
        from aria_code.local_image_provider import generate_image_local
    except Exception as exc:
        return {"success": False, "error": f"本地图片生成不可用: {exc}"}
    width = int(params.get("width") or 1024)
    height = int(params.get("height") or 1024)
    return generate_image_local(prompt, width=width, height=height)


def tool_edit_image(params: Dict[str, Any]) -> Dict[str, Any]:
    """LOCAL_TOOLS entry. params: {image_path, prompt, strength?}."""
    image_path = str(params.get("image_path") or "").strip()
    prompt = str(params.get("prompt") or "").strip()
    if not image_path or not prompt:
        return {"success": False, "error": "缺少 image_path 或 prompt"}
    try:
        from aria_code.local_image_provider import edit_image_local
    except Exception as exc:
        return {"success": False, "error": f"本地图片编辑不可用: {exc}"}
    strength = float(params.get("strength") or 0.6)
    return edit_image_local(image_path, prompt, strength=strength)


IMAGE_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "生成一张真实图片——照片风格、插画、风景图等任意主题，不限于股票/金融图表。"
                "支持 Google Cloud Imagen 3 原生高画质生成（配置 GCP/Gemini 时）及本地 SDXL-Turbo 生成。"
                "如果用户要的是K线图/行情图表，用 stock_chart，不要用这个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "图片内容的文字描述"},
                    "aspect_ratio": {"type": "string", "enum": ["1:1", "16:9", "9:16", "4:3", "3:4"], "description": "画面宽高比，默认 1:1"},
                    "model": {"type": "string", "description": "可选 gemini-3.1-flash-image（默认）、gemini-3.1-flash-lite-image（更快）或 gemini-3-pro-image"},
                    "width": {"type": "integer", "description": "本地模型宽度，默认 1024"},
                    "height": {"type": "integer", "description": "本地模型高度，默认 1024"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_image",
            "description": (
                "基于一张已有的本地图片按文字描述做改造（换风格/简化背景/加质感等），"
                "完全本地运行，免费。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "已有图片的本地文件路径"},
                    "prompt": {"type": "string", "description": "如何改造这张图"},
                    "strength": {"type": "number", "description": "0-1，偏离原图的程度，默认 0.6"},
                },
                "required": ["image_path", "prompt"],
            },
        },
    },
]


def register_image_tools(tool_registry: Dict, schema_registry: List) -> int:
    added = 0
    if "generate_image" not in tool_registry:
        tool_registry["generate_image"] = (
            tool_generate_image, "生成真实图片（照片/插画/风景等，非股票图表），本地免费",
        )
        added += 1
    if "edit_image" not in tool_registry:
        tool_registry["edit_image"] = (
            tool_edit_image, "基于已有图片按描述做改造，本地免费",
        )
        added += 1
    existing = {s.get("function", {}).get("name") for s in schema_registry}
    for schema in IMAGE_TOOL_SCHEMAS:
        if schema["function"]["name"] not in existing:
            schema_registry.append(schema)
    return added
