"""local_engine.py — 本地离线高质感运镜生成引擎（免网络、零成本兜底）。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from .base import BaseVideoEngine, VideoShotRequest, VideoShotResult
from aria_code.video_editor import create_ken_burns_clip

logger = logging.getLogger(__name__)


class LocalFallbackEngine(BaseVideoEngine):
    """本地运镜引擎，当云端与私有模型不可用或用户需要零成本预览时使用。"""

    name: str = "local"
    display_name: str = "Local Ken-Burns Motion Engine (Offline)"

    def health_check(self) -> Dict[str, Any]:
        import shutil
        has_ffmpeg = shutil.which("ffmpeg") is not None
        return {
            "engine": self.name,
            "display_name": self.display_name,
            "configured": has_ffmpeg,
            "ffmpeg_available": has_ffmpeg,
            "status_message": "Ready (Offline)" if has_ffmpeg else "ffmpeg not found in PATH",
        }

    def generate_shot(self, request: VideoShotRequest) -> VideoShotResult:
        """从图片素材本地渲染 Ken Burns 运镜视频。"""
        if not request.image_path or not Path(request.image_path).exists():
            return VideoShotResult(
                success=False,
                engine_name=self.name,
                error="Local engine requires a valid source image (image_path).",
            )

        effect = "zoom_in"
        if request.camera_motion:
            if request.camera_motion.get("pan", 0) > 0:
                effect = "pan_right"
            elif request.camera_motion.get("pan", 0) < 0:
                effect = "pan_left"
            elif request.camera_motion.get("zoom", 0) < 0:
                effect = "zoom_out"

        res = create_ken_burns_clip(
            request.image_path,
            duration=float(request.duration_seconds),
            effect=effect,
            aspect_ratio=request.aspect_ratio,
        )

        if res.get("success") and res.get("clip_path"):
            return VideoShotResult(
                success=True,
                path=res["clip_path"],
                engine_name=self.name,
                model_version="ken_burns_v2",
                duration=float(request.duration_seconds),
                aspect_ratio=request.aspect_ratio,
            )

        return VideoShotResult(
            success=False,
            engine_name=self.name,
            error=res.get("error", "Local Ken Burns generation failed."),
        )
