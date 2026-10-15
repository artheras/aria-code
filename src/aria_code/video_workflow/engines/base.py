"""base.py — 自研视频生成引擎抽象基类与数据规范。

定义统一的视频镜头请求 (VideoShotRequest)、生成结果 (VideoShotResult) 以及
引擎抽象接口 (BaseVideoEngine)。所有底层模型（Google Cloud Veo、自研私有模型、
本地引擎等）均需实现该接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class VideoShotRequest:
    """单个视频镜头的生成请求参数。"""
    prompt: str
    image_path: Optional[str] = None
    duration_seconds: int = 5
    aspect_ratio: str = "16:9"  # "16:9" | "9:16" | "1:1"
    camera_motion: Optional[Dict[str, float]] = None  # e.g. {"zoom": 0.2, "pan": 0.0, "tilt": 0.1}
    negative_prompt: Optional[str] = None
    seed: Optional[int] = None
    cfg_scale: float = 0.5
    # 面向自研模型的专属扩展参数
    custom_parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VideoShotResult:
    """单个视频镜头的生成结果。"""
    success: bool
    path: Optional[str] = None
    video_url: Optional[str] = None
    engine_name: str = ""
    model_version: str = ""
    duration: float = 0.0
    aspect_ratio: str = "16:9"
    cost_estimate_usd: Optional[float] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseVideoEngine(ABC):
    """视频生成引擎抽象基类。"""

    name: str = "base"
    display_name: str = "Base Video Engine"

    @abstractmethod
    def generate_shot(self, request: VideoShotRequest) -> VideoShotResult:
        """同步/阻塞轮询生成单个视频镜头。"""
        pass

    @abstractmethod
    def health_check(self) -> Dict[str, Any]:
        """检查当前引擎的配置可用性、鉴权及服务状态。"""
        pass

    def get_capabilities(self) -> Dict[str, Any]:
        """返回引擎支持的功能特性。"""
        return {
            "text_to_video": True,
            "image_to_video": True,
            "camera_control": False,
            "supported_aspect_ratios": ["16:9", "9:16", "1:1"],
            "max_duration_seconds": 10,
        }
