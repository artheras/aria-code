"""tests/test_music_library_service.py — 音乐库与程序化 BGM 合成单元测试。"""

import os
import wave
import pytest
from pathlib import Path

from aria_code.video_workflow.music import MusicLibraryService, MusicTrackMetadata


def test_music_service_mood_analysis():
    svc = MusicLibraryService.get_instance()
    
    # Keynote 发布会
    keynote_mood = svc.analyze_script_mood("全新一代旗舰猫咪 Taco Pro 亮相 Apple Keynote 发布会。")
    assert keynote_mood["style"] == "apple_keynote"
    assert keynote_mood["genre"] == "ambient"

    # 萌宠可爱
    cute_mood = svc.analyze_script_mood("小猫咪戴着粉色兔兔帽在毛毯上打滚，太可爱了！")
    assert cute_mood["style"] == "cute"
    assert cute_mood["genre"] == "acoustic"

    # 科技硬核
    tech_mood = svc.analyze_script_mood("自主研发超大规模神经网络芯片与分布式智能体。")
    assert tech_mood["style"] == "tech"
    assert tech_mood["genre"] == "electronic"


def test_procedural_ambient_synth_generation(tmp_path):
    out_wav = tmp_path / "ambient_test.wav"
    svc = MusicLibraryService.get_instance()
    
    path = svc.synthesize_ambient_synth_bgm(str(out_wav), duration_seconds=2.0)
    assert os.path.exists(path)
    
    with wave.open(path, "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 44100
        # 2 seconds at 44100 frames/sec = 88200 frames
        assert abs(wf.getnframes() - 88200) < 100


def test_match_or_synthesize_bgm(tmp_path):
    svc = MusicLibraryService.get_instance()
    bgm_file = svc.match_or_synthesize_bgm(
        script="Taco 优雅漫步在纯黑苹果舞台上。",
        style="apple_keynote",
        duration_seconds=3.0,
        output_dir=str(tmp_path),
    )
    assert os.path.exists(bgm_file)
    assert bgm_file.endswith(".wav")
