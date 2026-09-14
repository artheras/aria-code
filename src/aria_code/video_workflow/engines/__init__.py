"""engines package — 视频生成引擎抽象与多模型适配层。"""

from .base import BaseVideoEngine, VideoShotRequest, VideoShotResult
from .google_veo import GoogleCloudVeoEngine
from .custom_model import CustomModelEngine
from .local_engine import LocalFallbackEngine
from .registry import VideoEngineRegistry, get_video_engine

__all__ = [
    "BaseVideoEngine",
    "VideoShotRequest",
    "VideoShotResult",
    "GoogleCloudVeoEngine",
    "CustomModelEngine",
    "LocalFallbackEngine",
    "VideoEngineRegistry",
    "get_video_engine",
]
