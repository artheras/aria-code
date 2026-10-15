"""tests/test_video_session.py — 桌面端工程会话与单镜头重生成单元测试。"""

import os
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from aria_code.video_workflow.session import VideoProjectSession, VideoProject, ShotCard
from aria_code.tools.video_tools import (
    tool_create_video_project,
    tool_get_video_project,
    tool_regenerate_video_shot,
    tool_qa_review_video_clip,
)
from aria_code.video_workflow.engines import VideoShotResult


def test_project_session_create_and_get(tmp_path):
    session = VideoProjectSession(workspace_root=str(tmp_path))
    created = session.create_project(
        title="Taco 发布会特别篇",
        script="Taco 登场。聚光灯扫掠。全场欢呼。",
        aspect_ratio="9:16",
        target_duration=12.0,
    )

    assert created["success"] is True
    project_id = created["project"]["project_id"]
    assert project_id.startswith("proj_")
    assert len(created["project"]["shots"]) >= 2

    # 验证获取项目
    retrieved = session.get_project(project_id)
    assert retrieved["success"] is True
    assert retrieved["project"]["title"] == "Taco 发布会特别篇"
    assert retrieved["project"]["aspect_ratio"] == "9:16"


def test_regenerate_single_shot_and_assemble(tmp_path):
    session = VideoProjectSession(workspace_root=str(tmp_path))
    created = session.create_project(
        title="单镜测试",
        script="镜头一开场。镜头二高潮。镜头三结尾。",
        aspect_ratio="16:9",
        target_duration=12.0,
    )
    project_id = created["project"]["project_id"]

    fake_clip = tmp_path / "mock_clip.mp4"
    fake_clip.write_bytes(b"MOCK_GENERATED_VIDEO_DATA" * 50)

    mock_engine_res = VideoShotResult(
        success=True,
        path=str(fake_clip),
        engine_name="google",
        duration=4.0,
    )

    with patch.object(session.director.engine_registry.get_engine("google"), "generate_shot", return_value=mock_engine_res), \
         patch("aria_code.video_workflow.session.assemble_storyboard") as mock_assemble:
        mock_assemble.return_value = {"success": True, "path": str(tmp_path / "master.mp4"), "duration": 12.0}

        # 触发单镜头重生成第 2 镜
        regen_res = session.regenerate_single_shot(
            project_id=project_id,
            shot_index=2,
            new_prompt="Taco 眨动眼睛，墨镜闪过一道金色光芒",
        )

        assert regen_res["success"] is True
        assert regen_res["shot_index"] == 2
        assert regen_res["clip_path"] == str(fake_clip)
        assert regen_res["qa_score"] is not None

        # 检查工程文件更新状态
        proj = session.get_project(project_id)["project"]
        shot_2 = next(s for s in proj["shots"] if s["shot_index"] == 2)
        assert shot_2["status"] == "completed"
        assert "金色光芒" in shot_2["prompt"]


def test_tool_functions_for_desktop_session(tmp_path):
    # 用 patch 保证测试独立
    with patch("aria_code.video_workflow.session.VideoProjectSession.get_instance") as mock_inst:
        mock_session = VideoProjectSession(workspace_root=str(tmp_path))
        mock_inst.return_value = mock_session

        # 1. tool_create_video_project
        c_res = tool_create_video_project({
            "title": "Desktop Proj",
            "script": "欢迎来到未来世界。Taco 即将登场。",
            "aspect_ratio": "9:16",
        })
        assert c_res["success"] is True
        pid = c_res["project"]["project_id"]

        # 2. tool_get_video_project
        g_res = tool_get_video_project({"project_id": pid})
        assert g_res["success"] is True
        assert g_res["project"]["project_id"] == pid

        # 3. tool_qa_review_video_clip
        sample_video = tmp_path / "sample.mp4"
        sample_video.write_bytes(b"VALID_VIDEO_STREAM_DATA" * 100)
        qa_res = tool_qa_review_video_clip({
            "video_path": str(sample_video),
            "prompt": "Taco test clip",
            "shot_index": 1,
        })
        assert qa_res["success"] is True
        assert "qa_result" in qa_res
        assert qa_res["qa_result"]["passed"] is True
