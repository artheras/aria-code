"""Video workflow: plan rules, subtitles, and a real ffmpeg render with the models stubbed.

Google Cloud calls are replaced with fakes so the test is free and offline; the
render itself runs ffmpeg for real, and the checks read the resulting file.
"""
import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from aria_code.video_workflow import google_models as gm
from aria_code.video_workflow import pipeline
from aria_code.video_workflow.plan import Asset, Beat, EditPlan, to_srt, validate_plan

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed")


def _beat(bid, start, end, **kw):
    return Beat(id=bid, start=start, end=end, purpose=kw.pop("purpose", "evidence"), visual="v", **kw)


def test_plan_rules_are_explained_in_words():
    assets = {"A1": Asset(id="A1", path="/x.jpg", kind="image")}
    plan = EditPlan("p", "t", "douyin", "9:16", "zh", beats=[
        _beat("B1", 0, 3, purpose="evidence", asset_id="A1"),
        _beat("B2", 3.5, 6, asset_id="A9"),
        _beat("B3", 6, 16, source="generate", generate_prompt=""),
    ])
    problems = validate_plan(plan, assets)
    assert "The first beat should be the hook." in problems
    assert any("starts at 3.5s" in p for p in problems)
    assert any("asset 'A9'" in p for p in problems)
    assert any("without a prompt" in p for p in problems)
    assert any("at most 8s" in p for p in problems)


def test_subtitles_split_across_the_beat():
    plan = EditPlan("p", "t", "douyin", "9:16", "zh", beats=[
        _beat("B1", 0, 4, purpose="hook", copy="这是一段比较长的旁白，用来检查字幕会被切成几行显示。"),
    ])
    srt = to_srt(plan, max_chars=12)
    assert srt.count("-->") >= 2
    assert srt.startswith("1\n00:00:00,000 --> ")
    assert "00:00:04,000" in srt


def test_generation_is_refused_without_confirmation():
    with pytest.raises(gm.GoogleModelError):
        gm.generate_shot("a shot", "/tmp/x.mp4", duration_seconds=4, aspect_ratio="9:16")


def _make_image(path: Path, color: str):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:s=1200x1600", "-frames:v", "1", str(path)], check=True)


def _fake_speech(text, output_path, voice=None, model=None):
    # 0.25 s of audible tone per character, so beats have to stretch to fit.
    seconds = max(1.0, len(text) * 0.25)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-ac", "1", "-ar", "24000", output_path], check=True)
    return output_path


@needs_ffmpeg
def test_plan_then_render_produces_a_checked_video(tmp_path, monkeypatch):
    images = [tmp_path / "a.png", tmp_path / "b.png"]
    _make_image(images[0], "red")
    _make_image(images[1], "blue")

    def fake_json(parts, schema, system="", model=None, timeout=120.0):
        if "assets" in schema["properties"]:
            return {"assets": [
                {"id": "A1", "description": "红色背景", "orientation": "portrait", "suggested_use": "开头"},
                {"id": "A2", "description": "蓝色背景", "orientation": "portrait", "suggested_use": "结尾"},
            ]}
        return {"title": "测试短片", "beats": [
            {"id": "B1", "duration": 2, "purpose": "hook", "visual": "红色", "source": "asset", "asset_id": "A1", "effect": "zoom_in", "copy": "三秒看懂这个产品"},
            {"id": "B2", "duration": 2, "purpose": "cta", "visual": "蓝色", "source": "asset", "asset_id": "A2", "effect": "pan_left", "copy": "现在就试试"},
        ]}

    monkeypatch.setattr(gm, "generate_json", fake_json)
    monkeypatch.setattr(gm, "image_part", lambda path: {"text": f"<image {Path(path).name}>"})
    monkeypatch.setattr(gm, "synthesize_speech", _fake_speech)

    project = tmp_path / "demo"
    planned = pipeline.plan_video("三秒看懂这个产品，现在就试试。", [str(p) for p in images], str(project), platform="douyin", target_seconds=5)
    assert planned["plan"]["aspect_ratio"] == "9:16"
    assert (project / "edit_plan.json").exists()

    result = pipeline.render_video(str(project))
    assert Path(result["video_path"]).exists()
    checks = {c["check"]: c for c in result["checks"]}
    assert checks["frame_size"]["ok"], checks["frame_size"]
    assert checks["audio"]["ok"]
    assert checks["duration"]["ok"], checks["duration"]
    # "三秒看懂这个产品" is 8 characters → 2 s of speech + padding > the planned 2 s.
    plan = json.loads((project / "edit_plan.json").read_text(encoding="utf-8"))
    assert plan["beats"][0]["end"] > 2.0
    manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["generated_seconds"] == 0 and {a["id"] for a in manifest["assets"]} == {"A1", "A2"}


@needs_ffmpeg
def test_generated_beats_wait_for_confirmation(tmp_path):
    image = tmp_path / "a.png"
    _make_image(image, "green")
    (tmp_path / "assets.json").write_text(json.dumps([{"id": "A1", "path": str(image), "kind": "image"}]), encoding="utf-8")
    plan = EditPlan("p", "t", "douyin", "9:16", "zh", beats=[
        _beat("B1", 0, 3, purpose="hook", asset_id="A1"),
        _beat("B2", 3, 7, source="generate", generate_prompt="a sunrise over a city"),
    ])
    (tmp_path / "edit_plan.json").write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    result = pipeline.render_video(str(tmp_path))
    assert result["status"] == "needs_confirmation" and result["generated_seconds"] == 4


def test_one_asset_three_beats_in_a_row_is_sent_back():
    assets = {"A1": Asset(id="A1", path="/a.jpg", kind="image"), "A2": Asset(id="A2", path="/b.jpg", kind="image")}
    plan = EditPlan("p", "t", "douyin", "9:16", "zh", beats=[
        _beat("B1", 0, 3, purpose="hook", asset_id="A1"),
        _beat("B2", 3, 6, asset_id="A1"),
        _beat("B3", 6, 9, asset_id="A1"),
    ])
    problems = validate_plan(plan, assets)
    assert any("3 beats in a row" in p for p in problems)
    assert any("Assets A2 are not used" in p for p in problems)



@needs_ffmpeg
def test_mcp_render_tool_does_not_spend_without_confirmation(tmp_path):
    import asyncio

    from aria_code.packages.aria_mcp.server import _call_video_workflow_plan, _call_video_workflow_render

    assert asyncio.run(_call_video_workflow_plan({"script": "x"}))["success"] is False
    image = tmp_path / "a.png"
    _make_image(image, "green")
    (tmp_path / "assets.json").write_text(json.dumps([{"id": "A1", "path": str(image), "kind": "image"}]), encoding="utf-8")
    plan = EditPlan("p", "t", "douyin", "9:16", "zh", beats=[
        _beat("B1", 0, 3, purpose="hook", asset_id="A1"),
        _beat("B2", 3, 7, source="generate", generate_prompt="a sunrise over a city"),
    ])
    (tmp_path / "edit_plan.json").write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    result = asyncio.run(_call_video_workflow_render({"project_dir": str(tmp_path), "confirm_generation": "yes"}))
    assert result["success"] is True and result["status"] == "needs_confirmation"


def test_animate_rules_and_cost():
    assets = {"A1": Asset(id="A1", path="/x.jpg", kind="image"), "V1": Asset(id="V1", path="/x.mp4", kind="video")}
    plan = EditPlan("p", "t", "douyin", "9:16", "zh", beats=[
        _beat("B1", 0, 3, purpose="hook", source="animate", asset_id="A1", generate_prompt="slow push in, hair moves in the wind"),
        _beat("B2", 3, 6, source="animate", asset_id="V1", generate_prompt="pan"),
        _beat("B3", 6, 9, source="animate", asset_id="A1"),
    ])
    problems = validate_plan(plan, assets)
    assert any("only images can be animated" in p for p in problems)
    assert any("without describing the motion" in p for p in problems)
    assert plan.generated_seconds() == 9
    assert pipeline._generation_problems(plan, allow_generation=False)


@needs_ffmpeg
def test_animated_beat_sends_the_users_image_to_veo(tmp_path, monkeypatch):
    image = tmp_path / "a.png"
    _make_image(image, "green")
    (tmp_path / "assets.json").write_text(json.dumps([{"id": "A1", "path": str(image), "kind": "image"}]), encoding="utf-8")
    plan = EditPlan("p", "t", "douyin", "9:16", "zh", beats=[
        _beat("B1", 0, 3, purpose="hook", source="animate", asset_id="A1", generate_prompt="slow push in", copy="看这里"),
    ])
    (tmp_path / "edit_plan.json").write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    assert pipeline.render_video(str(tmp_path))["status"] == "needs_confirmation"

    calls = []

    def fake_shot(prompt, output_path, *, duration_seconds, aspect_ratio, image_path=None, model=None, confirmed=False):
        calls.append((prompt, image_path, confirmed))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(image), "-t", str(duration_seconds),
                        "-vf", "scale=720:1280", "-pix_fmt", "yuv420p", output_path], check=True)
        return output_path

    monkeypatch.setattr(gm, "generate_shot", fake_shot)
    monkeypatch.setattr(gm, "synthesize_speech", _fake_speech)
    result = pipeline.render_video(str(tmp_path), confirm_generation=True)
    assert Path(result["video_path"]).exists()
    assert calls and calls[0][1] == str(image) and calls[0][2] is True
    assert "Keep the subject" in calls[0][0] and "slow push in" in calls[0][0]
