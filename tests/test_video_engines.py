"""tests/test_video_engines.py — 自研视频生成引擎适配层单元测试。"""

import os
import pytest
from unittest.mock import MagicMock, patch

from aria_code.video_workflow.engines import (
    BaseVideoEngine,
    VideoShotRequest,
    VideoShotResult,
    GoogleCloudVeoEngine,
    CustomModelEngine,
    LocalFallbackEngine,
    VideoEngineRegistry,
    get_video_engine,
)


def test_base_video_engine_contract():
    class DummyEngine(BaseVideoEngine):
        name = "dummy"
        display_name = "Dummy Engine"

        def generate_shot(self, request: VideoShotRequest) -> VideoShotResult:
            return VideoShotResult(success=True, path="/tmp/dummy.mp4", engine_name=self.name)

        def health_check(self):
            return {"configured": True}

    engine = DummyEngine()
    assert engine.name == "dummy"
    req = VideoShotRequest(prompt="Test prompt")
    res = engine.generate_shot(req)
    assert res.success is True
    assert res.path == "/tmp/dummy.mp4"
    assert engine.get_capabilities()["image_to_video"] is True


def test_google_veo_engine_health_and_dispatch():
    engine = GoogleCloudVeoEngine()
    health = engine.health_check()
    assert health["engine"] == "google"
    assert "auth_mode" in health

    req = VideoShotRequest(
        prompt="Cat jumping",
        aspect_ratio="16:9",
        duration_seconds=5,
        camera_motion={"zoom": 0.2, "pan": 0.1},
    )

    with patch("aria_code.video_workflow.engines.google_veo.generate_video_google") as mock_gen:
        mock_gen.return_value = {
            "success": True,
            "path": "/tmp/veo_shot.mp4",
            "model": "veo-2.0-generate-001",
            "duration": 5,
            "aspect_ratio": "16:9",
        }
        res = engine.generate_shot(req)
        assert res.success is True
        assert res.path == "/tmp/veo_shot.mp4"
        assert res.engine_name == "google"

        # 验证提示词注入了 camera_motion
        called_prompt = mock_gen.call_args[1]["prompt"]
        assert "camera slow zoom in" in called_prompt
        assert "camera pan right" in called_prompt


def test_custom_model_engine_direct_response(tmp_path):
    engine = CustomModelEngine(endpoint_url="https://ai.example.com/v1/generate", auth_token="secret_key")
    health = engine.health_check()
    assert health["engine"] == "custom"
    assert health["has_auth_token"] is True

    req = VideoShotRequest(
        prompt="A robotic cat walking",
        duration_seconds=4,
        aspect_ratio="9:16",
        cfg_scale=0.7,
        custom_parameters={"lora": "cat_v1", "steps": 30},
    )

    fake_video_bytes = b"FAKE_MP4_CONTENT_12345"
    import base64
    fake_b64 = base64.b64encode(fake_video_bytes).decode("ascii")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "success",
        "video_base64": fake_b64,
        "metadata": {"seed": 42},
    }

    with patch("requests.post", return_value=mock_resp):
        res = engine.generate_shot(req)
        assert res.success is True
        assert res.engine_name == "custom"
        assert os.path.exists(res.path)
        assert open(res.path, "rb").read() == fake_video_bytes


def test_registry_management_and_switching():
    registry = VideoEngineRegistry.get_instance()
    
    # 验证初始预置引擎
    engines_status = registry.list_engines_status()
    engine_names = [e["engine"] for e in engines_status]
    assert "google" in engine_names
    assert "custom" in engine_names
    assert "local" in engine_names

    # 测试切换活跃引擎
    registry.set_active_engine("custom")
    assert registry.get_active_engine_name() == "custom"
    assert isinstance(registry.get_engine(), CustomModelEngine)

    registry.set_active_engine("google")
    assert registry.get_active_engine_name() == "google"
    assert isinstance(registry.get_engine(), GoogleCloudVeoEngine)

    with pytest.raises(ValueError):
        registry.set_active_engine("non_existent_engine")


def test_tool_configure_video_engine():
    from aria_code.tools.video_tools import tool_configure_video_engine

    # list
    status = tool_configure_video_engine({"action": "list"})
    assert status["success"] is True
    assert "engines" in status

    # configure_custom
    conf = tool_configure_video_engine({
        "action": "configure_custom",
        "endpoint_url": "http://my-gpu-cluster.internal:8000/v1/generate",
        "model_name": "taco-video-v1",
        "auth_token": "bearer-token-xyz",
    })
    assert conf["success"] is True
    assert conf["config"]["endpoint_url"] == "http://my-gpu-cluster.internal:8000/v1/generate"
    assert conf["config"]["model_name"] == "taco-video-v1"

    # switch
    sw = tool_configure_video_engine({"action": "set_active", "engine": "custom"})
    assert sw["success"] is True
    assert sw["active_engine"] == "custom"
