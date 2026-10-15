"""registry.py — 视频生成引擎注册表与生命周期管理器。

支持全局统一管理 Google Cloud Veo、自研私有模型 (Custom) 与本地引擎 (Local)，
支持通过环境变量、代码显式配置或 MCP 接口随时切换当前生效的主视频引擎。
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Type

from .base import BaseVideoEngine
from .google_veo import GoogleCloudVeoEngine
from .custom_model import CustomModelEngine
from .local_engine import LocalFallbackEngine

logger = logging.getLogger(__name__)


class VideoEngineRegistry:
    """引擎注册表单例管理器。"""

    _instance: Optional[VideoEngineRegistry] = None

    def __init__(self):
        self._engines: Dict[str, BaseVideoEngine] = {}
        # 默认内置三大引擎
        self.register_engine("google", GoogleCloudVeoEngine())
        self.register_engine("custom", CustomModelEngine())
        self.register_engine("local", LocalFallbackEngine())
        self._active_engine_name: Optional[str] = None

    @classmethod
    def get_instance(cls) -> VideoEngineRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Drop the singleton so the next caller builds a fresh registry.

        The registry is process-wide and mutable: registering an engine or
        setting the active one changes it for everything afterwards. In a test
        run that leaks — configuring a custom engine in one test left another
        patching "google" while the director resolved the active engine and
        called something else, which failed only when both ran together.
        """
        cls._instance = None

    def register_engine(self, name: str, engine: BaseVideoEngine) -> None:
        """注册或更新一个视频引擎实例。"""
        self._engines[name.lower()] = engine
        logger.debug(f"[VideoEngineRegistry] Registered engine '{name}': {engine.display_name}")

    def set_active_engine(self, name: str) -> None:
        """设置当前全局默认视频引擎。"""
        name_lower = name.lower()
        if name_lower not in self._engines:
            raise ValueError(f"Unknown engine '{name}'. Available: {list(self._engines.keys())}")
        self._active_engine_name = name_lower
        logger.info(f"[VideoEngineRegistry] Active video engine set to: {name_lower}")

    def get_active_engine_name(self) -> str:
        """获取当前活动引擎名称。优先按环境变量 ARIA_VIDEO_ENGINE，其次按配置，最后默认 google。"""
        if self._active_engine_name:
            return self._active_engine_name
        env_choice = os.getenv("ARIA_VIDEO_ENGINE", "").strip().lower()
        if env_choice in self._engines:
            return env_choice
        # 若自研模型端点被明确指定，可优先考虑
        if os.getenv("ARIA_CUSTOM_VIDEO_ENDPOINT"):
            return "custom"
        return "google"

    def get_engine(self, name: Optional[str] = None) -> BaseVideoEngine:
        """获取指定或当前生效的视频引擎实例。"""
        target = (name or self.get_active_engine_name()).lower()
        if target not in self._engines:
            logger.warning(f"Engine '{target}' not found, falling back to 'local'.")
            target = "local"
        return self._engines[target]

    def list_engines_status(self) -> List[Dict[str, Any]]:
        """获取所有已注册引擎的运行与配置健康度报告。"""
        active_name = self.get_active_engine_name()
        status_list = []
        for name, engine in self._engines.items():
            check = engine.health_check()
            check["is_active"] = (name == active_name)
            status_list.append(check)
        return status_list


def get_video_engine(name: Optional[str] = None) -> BaseVideoEngine:
    """快捷获取当前或指定的视频引擎。"""
    return VideoEngineRegistry.get_instance().get_engine(name)
