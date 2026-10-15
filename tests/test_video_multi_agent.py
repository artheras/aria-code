"""tests/test_video_multi_agent.py — 视频多 Agent (编剧、质检、音效) 单元测试。"""

import os
import wave
import pytest
from pathlib import Path

from aria_code.agents.video.qa_agent import VisualQAAgent, QACheckResult
from aria_code.agents.video.script_doctor import ScriptDoctorAgent
from aria_code.video_workflow.music.foley_service import FoleySoundService


def test_script_doctor_agent_pacing_and_hook():
    raw_idea = "Taco 是一只猫。它今天戴上了墨镜。全场欢呼。"
    polished = ScriptDoctorAgent.polish_and_structure(
        raw_text=raw_idea,
        target_duration=12.0,
        platform="douyin",
        subject_name="Taco",
    )

    assert polished.hook_strength_score >= 90
    assert len(polished.beats) >= 3
    # 验证第一个镜头具有 Hook 属性
    first_beat = polished.beats[0]
    assert first_beat.role == "hook"
    assert "Taco" in first_beat.narration

    # 验证最后一个镜头具有 CTA 属性
    last_beat = polished.beats[-1]
    assert last_beat.role == "cta"


def test_visual_qa_agent_review_and_self_correction(tmp_path):
    qa = VisualQAAgent(pass_threshold=80)

    # 1. 测试不存在的文件或损坏文件
    missing_res = qa.review_shot(1, "/tmp/non_existent.mp4", "Taco jumping")
    assert missing_res.passed is False
    assert missing_res.quality_score == 0
    assert missing_res.self_correction_prompt_patch is not None

    # 2. 模拟一个正常大小的文件
    dummy_video = tmp_path / "valid_clip.mp4"
    dummy_video.write_bytes(b"FAKE_VIDEO_PAYLOAD_WITH_ENOUGH_BYTES" * 100)

    normal_res = qa.review_shot(1, str(dummy_video), "Taco walking in biker jacket")
    assert normal_res.passed is True
    assert normal_res.quality_score >= 80
    assert normal_res.subject_integrity == "good"


def test_foley_sound_service_cues_and_synthesis(tmp_path):
    shots = [
        {"duration": 3.0, "prompt": "Shot 1: Opening horizon"},
        {"duration": 4.0, "prompt": "Shot 2: Taco wearing metallic sunglasses with lens flare reflection"},
        {"duration": 3.5, "prompt": "Shot 3: Epic conclusion"},
    ]

    cues = FoleySoundService.generate_cues_for_storyboard(shots)
    assert len(cues) >= 2

    # 验证包含转场 whoosh 和高光反光 glint
    sound_types = [c.sound_type for c in cues]
    assert "whoosh" in sound_types
    assert "glint" in sound_types

    # 测试程序化音效轨合成
    foley_wav = tmp_path / "foley_track.wav"
    out_path = FoleySoundService.synthesize_cue_track(cues, total_duration=10.5, output_path=str(foley_wav))
    assert os.path.exists(out_path)

    with wave.open(out_path, "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getframerate() == 44100
        assert wf.getnframes() > 0
