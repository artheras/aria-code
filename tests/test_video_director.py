"""tests/test_video_director.py — 视频导演 Agent 与工具链单元测试。"""

import os
import pytest
from unittest.mock import MagicMock, patch

from aria_code.agents.video.director import (
    VideoDirectorAgent,
    DiTMotionPlanner,
    CharacterConsistencyManager,
)
from aria_code.tools.video_tools import (
    tool_analyze_video_script,
    tool_generate_video_clip,
    tool_produce_video_from_script,
)
from aria_code.video_workflow.engines import VideoShotResult


def test_dit_motion_planner_generates_physics_and_camera():
    plan = DiTMotionPlanner.plan_motion_for_shot(
        shot_index=1,
        narrative="猫咪在聚光灯下打哈欠",
        subject_anchor="Taco the cat",
        style="cinematic",
    )
    assert "prompt" in plan
    assert "camera_motion" in plan
    assert "camera_description" in plan
    assert "Taco the cat" in plan["prompt"]
    assert "Physics & Motion" in plan["prompt"]
    assert plan["camera_motion"]["zoom"] != 0.0


def test_character_consistency_manager_anchor():
    anchor = CharacterConsistencyManager.extract_subject_anchor("Taco is a cute kitten wearing sunglasses", [])
    assert "Taco" in anchor
    assert "sunglasses" in anchor
    assert "leather jacket" in anchor


def test_video_director_agent_decompose():
    agent = VideoDirectorAgent()
    script = "苹果发布会震撼开启。Taco 戴着墨镜霸气登场。观众爆发出热烈掌声。这就是未来的定义。"
    storyboard = agent.decompose_script_and_images(
        script,
        target_duration=16.0,
        aspect_ratio="9:16",
        style="apple_keynote",
    )

    assert storyboard["aspect_ratio"] == "9:16"
    assert len(storyboard["shots"]) >= 3
    assert storyboard["subject_anchor"] != ""

    for s in storyboard["shots"]:
        assert "camera_motion" in s
        assert "prompt" in s
        assert s["duration"] > 0


def test_video_director_workflow_with_mocked_engine(tmp_path):
    agent = VideoDirectorAgent()
    script = "Taco 亮相。未来已来。"

    # Mock 生成引擎返回视频文件
    fake_clip = tmp_path / "shot_1.mp4"
    fake_clip.write_bytes(b"FAKE_VIDEO_STREAM")

    mock_result = VideoShotResult(
        success=True,
        path=str(fake_clip),
        engine_name="google",
        duration=4.0,
        aspect_ratio="16:9",
    )

    with patch.object(agent.engine_registry.get_engine("google"), "generate_shot", return_value=mock_result), \
         patch("aria_code.agents.video.director.assemble_storyboard") as mock_assemble:
        mock_assemble.return_value = {"success": True, "path": str(tmp_path / "final.mp4")}

        res = agent.produce_video_workflow(
            script,
            target_duration=8.0,
            aspect_ratio="16:9",
            provider="google",
        )

        assert res["success"] is True
        assert res["engine_used"] == "google"
        assert mock_assemble.called


def test_tool_analyze_video_script():
    res = tool_analyze_video_script({
        "script": "全新一代 Taco 发布。更快，更萌，更强大。",
        "aspect_ratio": "9:16",
    })
    assert res["success"] is True
    assert "storyboard" in res
    assert len(res["storyboard"]["shots"]) >= 2


def test_tool_generate_video_clip_with_mock():
    mock_result = VideoShotResult(
        success=True,
        path="/tmp/mock_shot.mp4",
        engine_name="custom",
        duration=5.0,
        aspect_ratio="9:16",
    )
    with patch("aria_code.video_workflow.engines.custom_model.CustomModelEngine.generate_shot", return_value=mock_result):
        res = tool_generate_video_clip({
            "prompt": "Taco walking in cyberpunk street",
            "provider": "custom",
            "aspect_ratio": "9:16",
            "duration": 5,
        })
        assert res["success"] is True
        assert res["engine"] == "custom"
        assert res["path"] == "/tmp/mock_shot.mp4"
