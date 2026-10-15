"""custom_model.py — 自研私有视频模型引擎适配器。

专为用户后续自研、微调或私有化部署的视频生成大模型（如自建 DiT 架构、ComfyUI 服务端、
vLLM / Triton 推理端点、Vertex AI Custom Prediction 或独立 GPU 云服务）打造。
通过标准化的 HTTP/REST 接口规范与可配置的 Payload 模板，支持随时将底层生成驱动
切换为自己的模型。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from .base import BaseVideoEngine, VideoShotRequest, VideoShotResult
from aria_code.artifacts import create_user_artifact

logger = logging.getLogger(__name__)

DEFAULT_CUSTOM_ENDPOINT = "http://localhost:8000/v1/video/generate"


class CustomModelEngine(BaseVideoEngine):
    """自研视频大模型适配引擎。"""

    name: str = "custom"
    display_name: str = "Custom Video Model (Self-Hosted / Private Cloud)"

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        self.endpoint_url = endpoint_url or os.getenv("ARIA_CUSTOM_VIDEO_ENDPOINT", DEFAULT_CUSTOM_ENDPOINT)
        self.auth_token = auth_token or os.getenv("ARIA_CUSTOM_VIDEO_TOKEN", "")
        self.model_name = model_name or os.getenv("ARIA_CUSTOM_VIDEO_MODEL", "aria-custom-dit-v1")

    def health_check(self) -> Dict[str, Any]:
        """检查自研模型端点的联通性。"""
        is_reachable = False
        error_msg = ""
        try:
            health_url = self.endpoint_url.rsplit("/", 1)[0] + "/health"
            resp = requests.get(health_url, timeout=3)
            is_reachable = resp.status_code == 200
        except Exception as exc:
            error_msg = str(exc)

        return {
            "engine": self.name,
            "display_name": self.display_name,
            "endpoint_url": self.endpoint_url,
            "model_name": self.model_name,
            "has_auth_token": bool(self.auth_token),
            "reachable": is_reachable,
            "status_message": "OK" if is_reachable else f"Not reachable ({error_msg or 'no health endpoint'})",
        }

    def generate_shot(self, request: VideoShotRequest) -> VideoShotResult:
        """向自研模型服务提交生成请求，并保存结果。"""
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        # 构造自研模型标准请求负载
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt or "blurry, low quality, artifacts, distorted",
            "duration": request.duration_seconds,
            "aspect_ratio": request.aspect_ratio,
            "cfg_scale": request.cfg_scale,
        }

        if request.seed is not None:
            payload["seed"] = request.seed
        if request.camera_motion:
            payload["camera_motion"] = request.camera_motion

        # 处理首帧图片输入 (图生视频 / I2V)
        if request.image_path and Path(request.image_path).exists():
            img_bytes = Path(request.image_path).read_bytes()
            payload["image_base64"] = base64.b64encode(img_bytes).decode("ascii")

        # 混入自定义扩展参数 (例如 lora_weights, sampling_steps, motion_scale)
        if request.custom_parameters:
            payload.update(request.custom_parameters)

        logger.info(f"[CustomModelEngine] Dispatching to {self.endpoint_url} (model={self.model_name})...")

        try:
            resp = requests.post(self.endpoint_url, headers=headers, json=payload, timeout=120)
            if resp.status_code != 200:
                return VideoShotResult(
                    success=False,
                    engine_name=self.name,
                    model_version=self.model_name,
                    error=f"Custom model endpoint returned HTTP {resp.status_code}: {resp.text[:300]}",
                )

            data = resp.json()

            # 1. 如果端点直接返回 base64 视频流
            video_b64 = data.get("video_base64")
            if video_b64:
                artifact = create_user_artifact("video", "custom_model", "clip", ".mp4")
                artifact.path.write_bytes(base64.b64decode(video_b64))
                return VideoShotResult(
                    success=True,
                    path=str(artifact.path),
                    engine_name=self.name,
                    model_version=self.model_name,
                    duration=request.duration_seconds,
                    aspect_ratio=request.aspect_ratio,
                    metadata=data.get("metadata", {}),
                )

            # 2. 如果端点返回可下载视频 URL
            video_url = data.get("video_url")
            if video_url:
                dl_resp = requests.get(video_url, timeout=120)
                if dl_resp.status_code == 200:
                    artifact = create_user_artifact("video", "custom_model", "clip", ".mp4")
                    artifact.path.write_bytes(dl_resp.content)
                    return VideoShotResult(
                        success=True,
                        path=str(artifact.path),
                        video_url=video_url,
                        engine_name=self.name,
                        model_version=self.model_name,
                        duration=request.duration_seconds,
                        aspect_ratio=request.aspect_ratio,
                        metadata=data.get("metadata", {}),
                    )

            # 3. 如果是异步任务 (task_id / poll_url)
            task_id = data.get("task_id")
            poll_url = data.get("poll_url") or f"{self.endpoint_url.rsplit('/', 1)[0]}/tasks/{task_id}"
            if task_id:
                return self._poll_task(poll_url, headers, request.duration_seconds, request.aspect_ratio)

            return VideoShotResult(
                success=False,
                engine_name=self.name,
                model_version=self.model_name,
                error="Custom endpoint response contained neither video_base64, video_url nor task_id.",
            )
        except Exception as exc:
            logger.exception(f"[CustomModelEngine] Error calling custom endpoint: {exc}")
            return VideoShotResult(
                success=False,
                engine_name=self.name,
                model_version=self.model_name,
                error=str(exc),
            )

    def _poll_task(self, poll_url: str, headers: Dict[str, str], duration: int, aspect: str, max_wait: float = 300.0) -> VideoShotResult:
        """异步轮询任务结果。"""
        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(3.0)
            try:
                res = requests.get(poll_url, headers=headers, timeout=20)
                if res.status_code == 200:
                    pdata = res.json()
                    status = pdata.get("status")
                    if status in ("succeed", "completed", "success"):
                        video_url = pdata.get("video_url")
                        if video_url:
                            v_resp = requests.get(video_url, timeout=120)
                            artifact = create_user_artifact("video", "custom_model", "clip", ".mp4")
                            artifact.path.write_bytes(v_resp.content)
                            return VideoShotResult(
                                success=True,
                                path=str(artifact.path),
                                video_url=video_url,
                                engine_name=self.name,
                                model_version=self.model_name,
                                duration=duration,
                                aspect_ratio=aspect,
                            )
                    elif status in ("failed", "error"):
                        return VideoShotResult(
                            success=False,
                            engine_name=self.name,
                            model_version=self.model_name,
                            error=pdata.get("error", "Task execution failed on custom model."),
                        )
            except Exception as exc:
                logger.debug(f"[CustomModelEngine] Poll error: {exc}")

        return VideoShotResult(
            success=False,
            engine_name=self.name,
            model_version=self.model_name,
            error="Custom model task polling timed out.",
        )
