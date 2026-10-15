"""video_tools.py — 视频分析、镜头生成与自动化剪辑制作工具集

向 aria-code 终端交互式会话及桌面端客户端注册以下能力：
1. analyze_video_script: 多模态分析剧本与输入图片，生成包含角色特征锚点与 DiT 物理动效的结构化分镜头剧本 (Storyboard)
2. generate_video_clip: 单镜头生成（通过 VideoEngineRegistry 调度 Google Cloud Veo、自研私有模型或本地引擎）
3. produce_video_from_script: 全自动工作流，一键将图片素材和脚本制作成最终 MP4 视频，支持多引擎与音频自动压音
4. configure_video_engine: 查看或配置当前使用的视频生成引擎（如设置自研模型端点或切换为 Google Cloud）
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from aria_code.video_workflow.engines import VideoEngineRegistry, VideoShotRequest, CustomModelEngine

logger = logging.getLogger(__name__)


def tool_analyze_video_script(params: Dict[str, Any]) -> Dict[str, Any]:
    """LOCAL_TOOLS: 分析文案脚本和图片，生成分镜剧本。"""
    script = str(params.get("script") or "").strip()
    if not script:
        return {"success": False, "error": "缺少脚本文案 (script)"}

    images = params.get("images") or []
    if isinstance(images, str):
        images = [images]

    aspect_ratio = str(params.get("aspect_ratio") or "16:9")
    target_duration = float(params.get("target_duration") or 15.0)
    style = str(params.get("style") or "cinematic")

    from aria_code.agents.video.director import VideoDirectorAgent

    agent = VideoDirectorAgent()
    storyboard = agent.decompose_script_and_images(
        script,
        images=images,
        target_duration=target_duration,
        aspect_ratio=aspect_ratio,
        style=style,
    )
    return {"success": True, "storyboard": storyboard}


def tool_generate_video_clip(params: Dict[str, Any]) -> Dict[str, Any]:
    """LOCAL_TOOLS: 生成单个镜头视频片段，调度自研引擎适配层。"""
    prompt = str(params.get("prompt") or "").strip()
    image_path = str(params.get("image_path") or "").strip() or None

    if not prompt and not image_path:
        return {"success": False, "error": "prompt 或 image_path 至少需要提供一项"}

    aspect_ratio = str(params.get("aspect_ratio") or "16:9")
    duration = int(round(float(params.get("duration") or 4.0)))
    provider = str(params.get("provider") or "auto").lower()
    camera_motion = params.get("camera_motion")
    custom_parameters = params.get("custom_parameters") or {}

    registry = VideoEngineRegistry.get_instance()
    engine = registry.get_engine(None if provider == "auto" else provider)

    req = VideoShotRequest(
        prompt=prompt or "cinematic shot",
        image_path=image_path,
        duration_seconds=duration,
        aspect_ratio=aspect_ratio,
        camera_motion=camera_motion,
        custom_parameters=custom_parameters,
    )

    result = engine.generate_shot(req)
    if result.success and result.path:
        return {
            "success": True,
            "path": result.path,
            "video_url": result.video_url,
            "engine": result.engine_name,
            "model": result.model_version,
            "duration": result.duration,
            "aspect_ratio": result.aspect_ratio,
        }

    # 如果首选引擎失败，尝试用本地引擎兜底
    if image_path and Path(image_path).exists() and engine.name != "local":
        logger.warning(f"[video_tools] Primary engine failed ({result.error}), falling back to local engine.")
        local_engine = registry.get_engine("local")
        fallback_res = local_engine.generate_shot(req)
        if fallback_res.success and fallback_res.path:
            return {
                "success": True,
                "path": fallback_res.path,
                "engine": "local_fallback",
                "model": "ken_burns_v2",
                "duration": fallback_res.duration,
                "aspect_ratio": fallback_res.aspect_ratio,
                "note": f"Primary engine failed ({result.error}), generated via local fallback.",
            }

    return {
        "success": False,
        "error": f"Failed to generate video clip: {result.error}",
    }


def tool_produce_video_from_script(params: Dict[str, Any]) -> Dict[str, Any]:
    """LOCAL_TOOLS: 视频导演全流程制作，从脚本文案与图片一键输出成片。"""
    script = str(params.get("script") or "").strip()
    if not script:
        return {"success": False, "error": "缺少脚本文案 (script)"}

    images = params.get("images") or []
    if isinstance(images, str):
        images = [images]

    aspect_ratio = str(params.get("aspect_ratio") or "16:9")
    target_duration = float(params.get("target_duration") or 15.0)
    provider = str(params.get("provider") or "auto").lower()
    audio_path = str(params.get("audio_path") or "").strip() or None
    bgm_path = str(params.get("bgm_path") or "").strip() or None
    engine_parameters = params.get("engine_parameters")

    from aria_code.agents.video.director import VideoDirectorAgent

    agent = VideoDirectorAgent()
    return agent.produce_video_workflow(
        script,
        images=images,
        target_duration=target_duration,
        aspect_ratio=aspect_ratio,
        provider=provider,
        audio_path=audio_path,
        bgm_path=bgm_path,
        engine_parameters=engine_parameters,
    )


def tool_configure_video_engine(params: Dict[str, Any]) -> Dict[str, Any]:
    """LOCAL_TOOLS: 查看或配置视频生成引擎。"""
    registry = VideoEngineRegistry.get_instance()
    action = str(params.get("action") or "list").lower()

    if action == "list" or action == "status":
        return {
            "success": True,
            "active_engine": registry.get_active_engine_name(),
            "engines": registry.list_engines_status(),
        }

    if action == "set_active":
        engine_name = str(params.get("engine") or "").strip().lower()
        if not engine_name:
            return {"success": False, "error": "Missing 'engine' name (e.g. google, custom, local)"}
        try:
            registry.set_active_engine(engine_name)
            return {
                "success": True,
                "message": f"Active video engine switched to '{engine_name}'.",
                "active_engine": engine_name,
            }
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

    if action == "configure_custom":
        endpoint_url = params.get("endpoint_url")
        auth_token = params.get("auth_token")
        model_name = params.get("model_name")
        custom_engine = registry.get_engine("custom")
        if isinstance(custom_engine, CustomModelEngine):
            if endpoint_url:
                custom_engine.endpoint_url = str(endpoint_url)
            if auth_token is not None:
                custom_engine.auth_token = str(auth_token)
            if model_name:
                custom_engine.model_name = str(model_name)
            return {
                "success": True,
                "message": "Custom video model engine configured successfully.",
                "config": custom_engine.health_check(),
            }
        return {"success": False, "error": "Custom engine not registered."}

    return {"success": False, "error": f"Unknown action '{action}'. Use 'list', 'set_active', or 'configure_custom'."}


def tool_create_video_project(params: Dict[str, Any]) -> Dict[str, Any]:
    """LOCAL_TOOLS: 创建桌面端视频项目，规划卡片化分镜。"""
    from aria_code.video_workflow.session import VideoProjectSession

    title = str(params.get("title") or "").strip()
    script = str(params.get("script") or "").strip()
    if not script:
        return {"success": False, "error": "Missing 'script' text."}

    images = params.get("images") or []
    if isinstance(images, str):
        images = [images]

    aspect_ratio = str(params.get("aspect_ratio") or "16:9")
    target_duration = float(params.get("target_duration") or 15.0)
    provider = str(params.get("provider") or "auto")

    session = VideoProjectSession.get_instance()
    return session.create_project(
        title=title,
        script=script,
        images=images,
        aspect_ratio=aspect_ratio,
        target_duration=target_duration,
        provider=provider,
    )


def tool_get_video_project(params: Dict[str, Any]) -> Dict[str, Any]:
    """LOCAL_TOOLS: 获取指定项目的分镜卡片与母带状态。"""
    from aria_code.video_workflow.session import VideoProjectSession

    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return {"success": False, "error": "Missing 'project_id'."}

    session = VideoProjectSession.get_instance()
    return session.get_project(project_id)


def tool_regenerate_video_shot(params: Dict[str, Any]) -> Dict[str, Any]:
    """LOCAL_TOOLS: 桌面端核心能力 — 单镜头独立重新生成，并秒级重新拼接母带。"""
    from aria_code.video_workflow.session import VideoProjectSession

    project_id = str(params.get("project_id") or "").strip()
    shot_index = int(params.get("shot_index", 0))
    if not project_id or shot_index <= 0:
        return {"success": False, "error": "'project_id' and valid 'shot_index' (>0) are required."}

    new_prompt = params.get("new_prompt")
    engine_name = params.get("engine")

    session = VideoProjectSession.get_instance()
    return session.regenerate_single_shot(
        project_id=project_id,
        shot_index=shot_index,
        new_prompt=new_prompt,
        engine_name=engine_name,
    )


def tool_qa_review_video_clip(params: Dict[str, Any]) -> Dict[str, Any]:
    """LOCAL_TOOLS: 视觉质检 Agent 对视频镜头进行多模态抽样质检与自愈建议。"""
    from aria_code.agents.video.qa_agent import VisualQAAgent

    video_path = str(params.get("video_path") or "").strip()
    if not video_path:
        return {"success": False, "error": "Missing 'video_path'."}

    prompt = str(params.get("prompt") or "")
    shot_index = int(params.get("shot_index", 1))

    agent = VisualQAAgent()
    qa_res = agent.review_shot(shot_index=shot_index, video_path=video_path, original_prompt=prompt)
    import dataclasses
    return {"success": True, "qa_result": dataclasses.asdict(qa_res)}


VIDEO_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "analyze_video_script",
            "description": (
                "智能视频导演分镜分析：解析剧本与图片，锁定角色特征锚点，生成具备 DiT 物理动力学与 3D 运镜参数的结构化分镜剧本 (Storyboard)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "视频文案、解说词或故事概要"},
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "本地图片素材路径列表（用于参考图、角色锚定与图生视频）",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "enum": ["16:9", "9:16", "1:1"],
                        "description": "目标视频画面画幅，默认 16:9",
                    },
                    "target_duration": {"type": "number", "description": "目标视频总时长（秒），默认 15"},
                    "style": {"type": "string", "description": "视觉质感与风格，例如 cinematic, apple_keynote, modern"},
                },
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_video_clip",
            "description": (
                "生成单个视频镜头片段。支持调度 Google Cloud Veo 2.0、自研私有模型端点或本地离线引擎。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "镜头物理动效与画面提示词"},
                    "image_path": {"type": "string", "description": "参考图片路径（用于图生视频或本地运镜）"},
                    "duration": {"type": "number", "description": "镜头时长（秒），默认 4"},
                    "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1"], "description": "画幅比例"},
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "google", "custom", "local"],
                        "description": "生成引擎：auto 自动匹配，google 使用 Google Cloud Veo，custom 使用自研模型，local 纯本地离线",
                    },
                    "camera_motion": {
                        "type": "object",
                        "description": "3D 相机运镜参数，例如 {\"zoom\": 0.2, \"pan\": 0.1, \"tilt\": 0.0}",
                    },
                    "custom_parameters": {
                        "type": "object",
                        "description": "透传给自研模型的私有参数（如 lora, steps, cfg_scale）",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "produce_video_from_script",
            "description": (
                "全自动视频制作工作流：从剧本和图片一键输出成片。包含分镜规划、模型调度、多轨混音（带语音自动压音 Sidechain Ducking）与字幕压制。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "短视频脚本或解说文案"},
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "供视频使用的图片素材路径列表",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "enum": ["16:9", "9:16", "1:1"],
                        "description": "画幅比例：横屏 16:9，竖屏短视频 9:16，方形 1:1",
                    },
                    "target_duration": {"type": "number", "description": "期望总时长（秒），默认 15"},
                    "audio_path": {"type": "string", "description": "配音音频文件路径（可选）"},
                    "bgm_path": {"type": "string", "description": "背景音乐文件路径（可选）"},
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "google", "custom", "local"],
                        "description": "视频生成引擎，默认 auto",
                    },
                    "engine_parameters": {
                        "type": "object",
                        "description": "透传给视频引擎的专属参数",
                    },
                },
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "configure_video_engine",
            "description": (
                "查看或配置视频生成模型引擎。支持查看引擎健康状态、切换默认引擎 (Google Cloud / 自研模型 / 本地)，或设置自研模型端点 URL 及密钥。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "set_active", "configure_custom"],
                        "description": "操作类型：list 查看所有引擎状态，set_active 切换当前活动引擎，configure_custom 配置自研模型端点",
                    },
                    "engine": {
                        "type": "string",
                        "enum": ["google", "custom", "local"],
                        "description": "当 action 为 set_active 时，指定的引擎名称",
                    },
                    "endpoint_url": {"type": "string", "description": "自研私有模型的 HTTP 推理服务端点 URL"},
                    "auth_token": {"type": "string", "description": "自研模型服务的认证 Token"},
                    "model_name": {"type": "string", "description": "自研模型名称，例如 aria-custom-dit-v1"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_video_project",
            "description": "创建桌面端视频项目，将剧本与素材拆解为可视化的分镜工程并持久化存储。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "视频标题"},
                    "script": {"type": "string", "description": "视频文案或剧本"},
                    "images": {"type": "array", "items": {"type": "string"}, "description": "素材图片路径列表"},
                    "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1"], "description": "画幅比例"},
                    "target_duration": {"type": "number", "description": "目标时长（秒）"},
                    "provider": {"type": "string", "enum": ["auto", "google", "custom", "local"], "description": "默认生成引擎"},
                },
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_video_project",
            "description": "获取指定视频项目详情（分镜卡片、当前状态、生成进度与成片路径）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "项目 ID"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "regenerate_video_shot",
            "description": "桌面端单镜头独立重生成：仅重新渲染指定镜头，自动保留其余已有镜头缓存并秒级完成母带重缝合。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "项目 ID"},
                    "shot_index": {"type": "integer", "description": "需要重生成的镜头序号（从 1 开始）"},
                    "new_prompt": {"type": "string", "description": "可选的修改后提示词"},
                    "engine": {"type": "string", "enum": ["google", "custom", "local"], "description": "可选的重生成引擎"},
                },
                "required": ["project_id", "shot_index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "qa_review_video_clip",
            "description": "视觉质检 Agent 对生成的视频镜头进行多模态抽帧质检、打分与缺陷诊断。",
            "parameters": {
                "type": "object",
                "properties": {
                    "video_path": {"type": "string", "description": "被质检的视频 MP4 文件路径"},
                    "prompt": {"type": "string", "description": "原始提示词"},
                    "shot_index": {"type": "integer", "description": "镜头序号"},
                },
                "required": ["video_path"],
            },
        },
    },
]


def register_video_tools(tool_registry: Dict, schema_registry: List) -> int:
    """注册视频制作相关工具到 Aria Code 终端交互循环。"""
    added = 0
    tools_map = {
        "analyze_video_script": tool_analyze_video_script,
        "generate_video_clip": tool_generate_video_clip,
        "produce_video_from_script": tool_produce_video_from_script,
        "configure_video_engine": tool_configure_video_engine,
        "create_video_project": tool_create_video_project,
        "get_video_project": tool_get_video_project,
        "regenerate_video_shot": tool_regenerate_video_shot,
        "qa_review_video_clip": tool_qa_review_video_clip,
    }

    # LOCAL_TOOLS holds (handler, description) pairs, because ToolExecutor
    # indexes local_tools[name][0]. Registering the bare function instead left
    # every one of these tools raising
    #   TypeError: 'function' object is not subscriptable
    # the first time the agent tried to call it — registered, listed, and
    # unusable. The description is taken from the tool's own schema so the two
    # cannot drift apart.
    descriptions = {
        schema.get("function", {}).get("name"): schema.get("function", {}).get("description", "")
        for schema in VIDEO_TOOL_SCHEMAS
        if isinstance(schema, dict)
    }
    for name, fn in tools_map.items():
        if name not in tool_registry:
            tool_registry[name] = (fn, descriptions.get(name, "").strip() or name)
            added += 1

    existing_names = {s.get("function", {}).get("name") for s in schema_registry if isinstance(s, dict)}
    for schema in VIDEO_TOOL_SCHEMAS:
        s_name = schema.get("function", {}).get("name")
        if s_name and s_name not in existing_names:
            schema_registry.append(schema)

    return added

