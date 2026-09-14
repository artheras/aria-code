"""google_veo.py — Google Cloud Vertex AI & Gemini API Veo 视频引擎实现。"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from .base import BaseVideoEngine, VideoShotRequest, VideoShotResult
from aria_code.providers.google_video_provider import has_google_video_config, generate_video_google, DEFAULT_VEO_MODEL

logger = logging.getLogger(__name__)


class GoogleCloudVeoEngine(BaseVideoEngine):
    """Google Cloud Veo 视频生成引擎（当前主要生产驱动）。"""

    name: str = "google"
    display_name: str = "Google Cloud Veo (Vertex AI / Gemini API)"

    def __init__(self, model: str = DEFAULT_VEO_MODEL):
        self.model = model

    def health_check(self) -> Dict[str, Any]:
        """检查 Google Cloud 凭证与配置状态。"""
        vertex_project = os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT", "")
        api_key = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
        configured = has_google_video_config()

        mode = "Vertex AI (ADC)" if vertex_project else ("Gemini API Key" if api_key else "None")
        return {
            "engine": self.name,
            "display_name": self.display_name,
            "configured": configured,
            "auth_mode": mode,
            "model": self.model,
            "project_id": vertex_project if vertex_project else "(using Gemini API Key)",
        }

    def generate_shot(self, request: VideoShotRequest) -> VideoShotResult:
        """调用 Google Cloud Veo 生成单个视频片段。"""
        logger.info(f"[GoogleVeoEngine] Submitting shot: {request.prompt[:60]}...")

        # 丰富 Prompt：若有镜头运镜参数则显式融入 Prompt
        enhanced_prompt = request.prompt
        if request.camera_motion:
            motions = []
            if request.camera_motion.get("zoom", 0) > 0:
                motions.append("camera slow zoom in")
            elif request.camera_motion.get("zoom", 0) < 0:
                motions.append("camera slow zoom out")
            if request.camera_motion.get("pan", 0) > 0:
                motions.append("camera pan right")
            elif request.camera_motion.get("pan", 0) < 0:
                motions.append("camera pan left")
            if request.camera_motion.get("tilt", 0) > 0:
                motions.append("camera tilt up")
            elif request.camera_motion.get("tilt", 0) < 0:
                motions.append("camera tilt down")
            if motions:
                enhanced_prompt = f"{enhanced_prompt}. {', '.join(motions)}."

        try:
            res = generate_video_google(
                prompt=enhanced_prompt,
                image_path=request.image_path,
                model=self.model,
                aspect_ratio=request.aspect_ratio,
                duration_seconds=request.duration_seconds,
            )
            if res.get("success") and res.get("path"):
                return VideoShotResult(
                    success=True,
                    path=res["path"],
                    engine_name=self.name,
                    model_version=res.get("model", self.model),
                    duration=float(res.get("duration", request.duration_seconds)),
                    aspect_ratio=res.get("aspect_ratio", request.aspect_ratio),
                    metadata={"provider": res.get("provider", "Google Cloud")},
                )
            return VideoShotResult(
                success=False,
                engine_name=self.name,
                model_version=self.model,
                error=res.get("error", "Google Veo failed to produce video."),
            )
        except Exception as exc:
            logger.exception(f"[GoogleVeoEngine] Generation exception: {exc}")
            return VideoShotResult(
                success=False,
                engine_name=self.name,
                model_version=self.model,
                error=str(exc),
            )
