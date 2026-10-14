"""Script + images → finished short video, as a resumable pipeline.

    plan_video()    1 understand assets   (Gemini, reads every image)
                    2 write the edit plan (Gemini, JSON schema, validated)
                    → edit_plan.json, for the user to review and edit
    render_video()  3 narrate             (Gemini TTS; beats stretch to fit speech)
                    4 render              (ffmpeg; Veo only with confirmation)
                    5 verify              (ffprobe / blackdetect / silencedetect)
                    6 manifest            (sources, models, checks)

Only stages 1–2 use judgement. Everything after reads the plan and is
deterministic, so an unsatisfying video is fixed by editing edit_plan.json and
rendering again, not by rerunning the model. Each stage writes into the
project directory and a finished stage is reused on the next run.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from aria_code.video_workflow import google_models as gm
from aria_code.video_workflow.plan import (
    EFFECTS, MAX_GENERATED_SECONDS, PAID_SOURCES, PURPOSES, SOURCE_KINDS, Asset, Beat, EditPlan, default_aspect, to_srt, validate_plan,
)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
FPS = 25
SPEECH_PADDING = 0.4  # seconds of air after each beat's narration
FRAME_SIZE = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}

Progress = Callable[[str, str], None]


def _noop(stage: str, message: str) -> None:  # pragma: no cover - default sink
    pass


class VideoWorkflowError(RuntimeError):
    pass


# ── ffmpeg helpers ───────────────────────────────────────────────────────────

def _ffmpeg(args: List[str], timeout: float = 600.0) -> None:
    if shutil.which("ffmpeg") is None:
        raise VideoWorkflowError("ffmpeg is not installed (brew install ffmpeg)")
    result = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise VideoWorkflowError(f"ffmpeg failed: {result.stderr[-400:]}")


def ffmpeg_has_filter(name: str) -> bool:
    """Whether this ffmpeg build has a filter. Homebrew's default build lacks libass, so no `subtitles`."""
    result = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30)
    return any(line.split()[1:2] == [name] for line in result.stdout.splitlines() if len(line.split()) > 1)


def media_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise VideoWorkflowError(f"Could not read the duration of {Path(path).name}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 1. Understand assets ─────────────────────────────────────────────────────

_ASSET_SCHEMA = {
    "type": "object",
    "properties": {
        "assets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                    "subjects": {"type": "array", "items": {"type": "string"}},
                    "text_in_frame": {"type": "string"},
                    "orientation": {"type": "string", "enum": ["portrait", "landscape", "square"]},
                    "suggested_use": {"type": "string"},
                    "rights_note": {"type": "string"},
                },
                "required": ["id", "description", "orientation", "suggested_use"],
            },
        }
    },
    "required": ["assets"],
}


def _frame_for_understanding(video: Path, workdir: Path) -> Path:
    frame = workdir / f"{video.stem}_frame.jpg"
    if not frame.exists():
        middle = max(0.0, media_duration(str(video)) / 2)
        _ffmpeg(["-ss", f"{middle:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(frame)], timeout=60)
    return frame


def understand_assets(paths: List[str], script: str, workdir: Path, *, language: str = "zh") -> List[Asset]:
    """Describe each provided image or clip so the plan can place it deliberately."""
    files = [Path(p).expanduser().resolve() for p in paths]
    for file in files:
        if not file.exists():
            raise VideoWorkflowError(f"Asset not found: {file}")
        if file.suffix.lower() not in IMAGE_SUFFIXES | VIDEO_SUFFIXES:
            raise VideoWorkflowError(f"{file.name}: only images and video clips can be used as assets")
    assets = [
        Asset(id=f"A{index + 1}", path=str(file), kind="video" if file.suffix.lower() in VIDEO_SUFFIXES else "image")
        for index, file in enumerate(files)
    ]
    if not assets:
        return []

    parts: List[Dict[str, Any]] = [{"text": (
        "下面是一个短视频项目的素材。逐个描述每个素材，供之后写分镜使用。\n"
        f"脚本：\n{script.strip()}\n\n"
        "每个素材写：画面内容、主体、画面里出现的文字、横竖构图、在这个脚本里最适合放在哪里；"
        "如果画面里有人脸、商标、他人作品或看起来来自第三方的内容，在 rights_note 里写明需要确认的授权。"
        f"用{'中文' if language.startswith('zh') else 'English'}回答。id 必须与标注的一致。"
    )}]
    workdir.mkdir(parents=True, exist_ok=True)
    for asset in assets:
        source = Path(asset.path)
        view = _frame_for_understanding(source, workdir) if asset.kind == "video" else source
        parts.append({"text": f"素材 {asset.id}（{asset.kind}，文件名 {source.name}）："})
        parts.append(gm.image_part(str(view)))

    described = {item.get("id"): item for item in gm.generate_json(parts, _ASSET_SCHEMA).get("assets", [])}
    for asset in assets:
        item = described.get(asset.id, {})
        asset.description = str(item.get("description") or "")
        asset.subjects = [str(s) for s in item.get("subjects") or []]
        asset.text_in_frame = str(item.get("text_in_frame") or "")
        asset.orientation = str(item.get("orientation") or "")
        asset.suggested_use = str(item.get("suggested_use") or "")
        asset.rights_note = str(item.get("rights_note") or "")
    return assets


# ── 2. Write the edit plan ───────────────────────────────────────────────────

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "beats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "duration": {"type": "number"},
                    "purpose": {"type": "string", "enum": list(PURPOSES)},
                    "visual": {"type": "string"},
                    "source": {"type": "string", "enum": list(SOURCE_KINDS)},
                    "asset_id": {"type": "string"},
                    "generate_prompt": {"type": "string"},
                    "effect": {"type": "string", "enum": list(EFFECTS)},
                    "copy": {"type": "string"},
                    "caption": {"type": "string"},
                    "pending": {"type": "boolean"},
                },
                "required": ["id", "duration", "purpose", "visual", "source", "effect", "copy"],
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "beats"],
}


def _plan_schema(assets: List[Asset], allow_generation: bool) -> Dict[str, Any]:
    """The plan schema narrowed to what is actually legal for this request.

    asset_id was optional and unconstrained, and the rule that a shot taken
    from the material must name it lived only in the prose instructions.
    Structured output drops optional fields, so the model answered
    source="asset" for every beat and omitted asset_id on all of them — the
    plan then failed validation twice over ("uses asset ''"), after two full
    rounds of paid calls, with an attached image sitting unused.

    A rule the schema can state is stated here instead: the ids that exist are
    the only ones it can choose from, and with generation switched off
    "asset" is the only source there is, so naming one is not optional.
    """
    schema = deepcopy(_PLAN_SCHEMA)
    beat = schema["properties"]["beats"]["items"]
    ids = [asset.id for asset in assets]
    if ids:
        beat["properties"]["asset_id"] = {"type": "string", "enum": ids}
    if not allow_generation:
        # Every other source is paid, so there is nothing else to choose.
        beat["properties"]["source"] = {"type": "string", "enum": ["asset"]}
        if ids:
            beat["required"] = [*beat["required"], "asset_id"]
    return schema


def _plan_instructions(target_seconds: int, allow_generation: bool, language: str) -> str:
    return (
        "你是短视频剪辑师，按 short-form-video-editing 技能的规则把脚本和素材写成分镜。规则：\n"
        "- 第一个镜头是 hook，3 秒内抓住注意力；最后是 conclusion 或 cta。\n"
        f"- 总时长约 {target_seconds} 秒；每个镜头 2–6 秒，一个镜头只讲一件事。\n"
        "- copy 是这个镜头的旁白，口语、短句，朗读时长要放得进这个镜头（中文约每秒 4 个字）。\n"
        "- caption 是屏幕字幕，比旁白更短；不写就沿用旁白。\n"
        "- source=asset 时 asset_id 必须是给出的素材 id；同一素材可复用，但换一个镜头运动。\n"
        + (
            f"- 想让用户给的某张图片真正动起来（人物动作、镜头推进、水流烟雾），用 source=animate，asset_id 写那张图片，generate_prompt 只描述动作和镜头运动，不要改画面内容；画面会从这张图开始，保持原图。\n"
            f"- 素材不够时才用 source=generate，并写出具体的 generate_prompt（画面、镜头、光线、风格，英文更稳）。\n"
            f"- animate 和 generate 镜头都不超过 {MAX_GENERATED_SECONDS} 秒、按秒计费，尽量少用；能用素材的普通运镜解决就用 source=asset。\n"
            if allow_generation else
            "- 不允许生成或让图片动起来：每个镜头都必须使用给出的素材（source=asset）。\n"
        )
        + "- 画面要有变化：同一素材最多连续用 2 个镜头，给出的素材尽量都用上。\n"
        "- 需要用户补充的内容（缺素材、缺授权、事实待核实）把 pending 设为 true，并写进 notes。\n"
        "- 不编造数据、价格、功效或承诺。\n"
        f"- 用{'中文' if language.startswith('zh') else 'English'}写旁白与字幕。"
    )


def _plan_from_model(data: Dict[str, Any], *, project_id: str, platform: str, aspect: str, language: str) -> EditPlan:
    beats: List[Beat] = []
    cursor = 0.0
    for index, raw in enumerate(data.get("beats") or []):
        duration = max(1.5, float(raw.get("duration") or 3.0))
        beats.append(Beat(
            id=str(raw.get("id") or f"B{index + 1}"),
            start=round(cursor, 3),
            end=round(cursor + duration, 3),
            purpose=str(raw.get("purpose") or "evidence"),
            visual=str(raw.get("visual") or ""),
            source=str(raw.get("source") or "asset"),
            asset_id=str(raw.get("asset_id") or ""),
            generate_prompt=str(raw.get("generate_prompt") or ""),
            effect=str(raw.get("effect") or "zoom_in"),
            copy=str(raw.get("copy") or ""),
            caption=str(raw.get("caption") or ""),
            pending=bool(raw.get("pending")),
        ))
        cursor += duration
    return EditPlan(
        project_id=project_id, title=str(data.get("title") or project_id), platform=platform, aspect_ratio=aspect,
        language=language, beats=beats, notes=[str(n) for n in data.get("notes") or []],
    )


def write_edit_plan(
    script: str,
    assets: List[Asset],
    *,
    project_id: str,
    platform: str,
    aspect: str,
    language: str,
    target_seconds: int,
    allow_generation: bool,
) -> EditPlan:
    """Ask Gemini for the plan; if it breaks a rule, show it the problems once and ask again."""
    catalogue = [
        {key: value for key, value in asdict(asset).items() if key != "path"} | {"file": Path(asset.path).name}
        for asset in assets
    ]
    base = (
        f"平台：{platform}，画幅 {aspect}\n\n脚本：\n{script.strip()}\n\n"
        f"素材：\n{json.dumps(catalogue, ensure_ascii=False, indent=2)}"
    )
    system = _plan_instructions(target_seconds, allow_generation, language)
    by_id = {asset.id: asset for asset in assets}
    schema = _plan_schema(assets, allow_generation)
    plan = _plan_from_model(gm.generate_json([{"text": base}], schema, system=system),
                            project_id=project_id, platform=platform, aspect=aspect, language=language)
    problems = validate_plan(plan, by_id) + _generation_problems(plan, allow_generation)
    if problems:
        retry = base + "\n\n上一版分镜有这些问题，请修正后重写完整分镜：\n- " + "\n- ".join(problems)
        plan = _plan_from_model(gm.generate_json([{"text": retry}], schema, system=system),
                                project_id=project_id, platform=platform, aspect=aspect, language=language)
        problems = validate_plan(plan, by_id) + _generation_problems(plan, allow_generation)
        if problems:
            raise VideoWorkflowError("The edit plan still breaks the rules after one revision:\n- " + "\n- ".join(problems))
    return plan


def _generation_problems(plan: EditPlan, allow_generation: bool) -> List[str]:
    if allow_generation:
        return []
    return [f"Beat {beat.id} uses Veo ({beat.source}), but generation is not allowed for this video." for beat in plan.beats if beat.source in PAID_SOURCES]


def plan_video(
    script: str,
    asset_paths: List[str],
    project_dir: str,
    *,
    platform: str = "douyin",
    aspect_ratio: Optional[str] = None,
    language: str = "zh",
    target_seconds: int = 30,
    allow_generation: bool = False,
    force: bool = False,
    progress: Progress = _noop,
) -> Dict[str, Any]:
    """Stages 1–2. Writes assets.json and edit_plan.json into project_dir and returns them."""
    root = Path(project_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "script.txt").write_text(script, encoding="utf-8")
    project_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", root.name).strip("-") or "video"
    aspect = default_aspect(platform, aspect_ratio)

    # Nothing to cut together, and not allowed to generate anything: the
    # planner would be asked to pick shots from an empty list, write beats
    # with an empty asset, and fail validation a revision later — a minute of
    # paid model calls to reach a conclusion that is available here.
    if not asset_paths and not allow_generation:
        raise VideoWorkflowError(
            "No material to make a video from: attach images or clips, "
            "or allow shots to be generated."
        )

    assets_file = root / "assets.json"
    if assets_file.exists() and not force:
        assets = [Asset(**item) for item in json.loads(assets_file.read_text(encoding="utf-8"))]
        progress("understand", "沿用已有的素材理解")
    else:
        progress("understand", f"正在理解 {len(asset_paths)} 个素材")
        assets = understand_assets(asset_paths, script, root / "work", language=language)
        _write_json(assets_file, [asdict(asset) for asset in assets])

    progress("plan", "正在写分镜")
    plan = write_edit_plan(script, assets, project_id=project_id, platform=platform, aspect=aspect, language=language,
                           target_seconds=target_seconds, allow_generation=allow_generation)
    _write_json(root / "edit_plan.json", plan.to_dict())
    return {
        "project_dir": str(root),
        "plan_path": str(root / "edit_plan.json"),
        "plan": plan.to_dict(),
        "assets": [asdict(asset) for asset in assets],
        "generated_seconds": plan.generated_seconds(),
        "pending": [beat.id for beat in plan.beats if beat.pending],
    }


# ── 3. Narrate ───────────────────────────────────────────────────────────────

def narrate(plan: EditPlan, root: Path, progress: Progress = _noop) -> Path:
    """Speak each beat, stretch beats that are shorter than their speech, and build one narration track."""
    voice_dir = root / "narration"
    voice_dir.mkdir(parents=True, exist_ok=True)
    for beat in plan.beats:
        if not beat.copy.strip():
            continue
        key = hashlib.sha1(f"{plan.voice}|{beat.copy}".encode("utf-8")).hexdigest()[:12]
        speech = voice_dir / f"{beat.id}-{key}.wav"
        if not speech.exists():
            progress("narrate", f"正在为镜头 {beat.id} 配音")
            gm.synthesize_speech(beat.copy, str(speech), voice=plan.voice or None)
        needed = media_duration(str(speech)) + SPEECH_PADDING
        if needed > beat.duration:
            beat.end = round(beat.start + needed, 3)
        setattr(beat, "_speech", str(speech))
        plan.retime()

    padded: List[str] = []
    for beat in plan.beats:
        out = voice_dir / f"{beat.id}-padded.wav"
        speech = getattr(beat, "_speech", None)
        if speech:
            _ffmpeg(["-i", speech, "-af", f"apad,atrim=0:{beat.duration:.3f}", "-ar", "44100", "-ac", "2", str(out)], timeout=60)
        else:
            _ffmpeg(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100", "-t", f"{beat.duration:.3f}", str(out)], timeout=60)
        padded.append(str(out))

    track = root / "narration.wav"
    listing = voice_dir / "concat.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in padded), encoding="utf-8")
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "pcm_s16le", str(track)], timeout=120)
    return track


# ── 4. Render ────────────────────────────────────────────────────────────────

def _normalise_clip(source: str, out: Path, duration: float, aspect: str) -> None:
    width, height = FRAME_SIZE[aspect]
    _ffmpeg([
        "-i", source, "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-vf", (f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
                f"fps={FPS},tpad=stop_mode=clone:stop_duration={duration:.3f},format=yuv420p"),
        "-map", "0:v:0", "-map", "1:a:0", "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out),
    ])


def _compose_still(image: str, out: Path, aspect: str) -> None:
    """Fit an image into the frame over a blurred fill of itself.

    Rendered directly, a square logo in a 9:16 frame was cropped by the camera
    move and a transparent PNG came out on solid black. Composing first keeps
    the whole subject in frame, and the camera move then works on an image of
    the frame's own shape.
    """
    width, height = FRAME_SIZE[aspect]
    fg_width = int(width * 0.86)
    _ffmpeg([
        "-i", image,
        "-filter_complex", (
            f"[0:v]format=rgba,split=2[bg][fg];"
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"boxblur=40:2,colorchannelmixer=rr=0.55:gg=0.55:bb=0.55[bgb];"
            f"color=c=0x1c1f1e:s={width}x{height}[base];"
            f"[base][bgb]overlay=0:0[filled];"
            f"[fg]scale='min({fg_width},iw*{height}*0.7/ih)':-2[fgs];"
            f"[filled][fgs]overlay=(W-w)/2:(H-h)/2,format=yuv420p"
        ),
        "-frames:v", "1", "-q:v", "2", str(out),
    ], timeout=120)


def _image_clip(image: str, out: Path, duration: float, aspect: str, effect: str) -> None:
    from aria_code.video_editor import create_ken_burns_clip

    composed = out.with_name(out.stem + "-frame.jpg")
    _compose_still(image, composed, aspect)
    result = create_ken_burns_clip(str(composed), duration=duration, effect=effect, aspect_ratio=aspect, output_path=str(out))
    if not result.get("success"):
        raise VideoWorkflowError(f"Could not animate {Path(image).name}: {result.get('error')}")


def render_video(
    project_dir: str,
    *,
    confirm_generation: bool = False,
    progress: Progress = _noop,
) -> Dict[str, Any]:
    """Stages 3–6 from project_dir/edit_plan.json. Returns the video path, checks and manifest."""
    root = Path(project_dir).expanduser().resolve()
    plan_file, assets_file = root / "edit_plan.json", root / "assets.json"
    if not plan_file.exists() or not assets_file.exists():
        raise VideoWorkflowError("Run plan_video first: edit_plan.json and assets.json are missing")
    plan = EditPlan.from_dict(json.loads(plan_file.read_text(encoding="utf-8")))
    assets = {item["id"]: Asset(**item) for item in json.loads(assets_file.read_text(encoding="utf-8"))}
    plan.retime()

    problems = validate_plan(plan, assets)
    if problems:
        raise VideoWorkflowError("The edit plan cannot be rendered:\n- " + "\n- ".join(problems))
    generated = plan.generated_seconds()
    if generated and not confirm_generation:
        return {
            "status": "needs_confirmation",
            "generated_seconds": generated,
            "generated_beats": [beat.id for beat in plan.beats if beat.source in PAID_SOURCES],
            "message": f"这个分镜有 {generated} 秒需要用 Veo 生成，按秒计费。确认后再渲染。",
        }

    narration = narrate(plan, root, progress)
    _write_json(plan_file, plan.to_dict())  # durations now fit the narration

    clips_dir = root / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clips: List[str] = []
    models_used = {"planner": gm.DEFAULT_PLANNER_MODEL, "tts": gm.DEFAULT_TTS_MODEL}
    for beat in plan.beats:
        out = clips_dir / f"{beat.id}.mp4"
        progress("render", f"正在渲染镜头 {beat.id}")
        if beat.source in PAID_SOURCES:
            raw = clips_dir / f"{beat.id}-veo.mp4"
            if not raw.exists():
                # animate: image-to-video, the user's image is the first frame.
                image = assets[beat.asset_id].path if beat.source == "animate" else None
                prompt = beat.generate_prompt
                if image:
                    prompt = f"Animate this exact image. Keep the subject, faces, colours and composition unchanged. Motion: {prompt}"
                gm.generate_shot(prompt, str(raw), duration_seconds=int(round(beat.duration)) or 4,
                                 aspect_ratio=plan.aspect_ratio, image_path=image, confirmed=True)
                models_used["video"] = gm.DEFAULT_VEO_MODEL
            _normalise_clip(str(raw), out, beat.duration, plan.aspect_ratio)
        else:
            asset = assets[beat.asset_id]
            if asset.kind == "video":
                _normalise_clip(asset.path, out, beat.duration, plan.aspect_ratio)
            else:
                _image_clip(asset.path, out, beat.duration, plan.aspect_ratio, beat.effect)
        clips.append(str(out))

    silent = root / "work" / "picture.mp4"
    silent.parent.mkdir(parents=True, exist_ok=True)
    listing = root / "work" / "clips.txt"
    listing.write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c:v", "libx264", "-r", str(FPS), "-an", "-pix_fmt", "yuv420p", str(silent)])

    srt = root / "subtitles.srt"
    srt.write_text(to_srt(plan), encoding="utf-8")
    final = root / f"{plan.project_id}.mp4"
    progress("render", "正在合成字幕与旁白")
    burned = ffmpeg_has_filter("subtitles")
    if burned:
        subtitle_path = str(srt.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        width, _ = FRAME_SIZE[plan.aspect_ratio]
        font_size = 11 if width >= 1920 else 9
        _ffmpeg([
            "-i", str(silent), "-i", str(narration),
            "-vf", f"subtitles='{subtitle_path}':force_style='FontSize={font_size},Outline=1,Shadow=0,MarginV=36'",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k", "-shortest", str(final),
        ])
    else:
        # No libass: ship the captions as a selectable subtitle track and say so
        # in the checks, rather than silently delivering a video without text.
        _ffmpeg([
            "-i", str(silent), "-i", str(narration), "-i", str(srt),
            "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-c:s", "mov_text", "-shortest", str(final),
        ])

    progress("verify", "正在检查成片")
    checks = verify_video(str(final), plan)
    checks.append({
        "check": "captions_burned_in",
        "ok": burned,
        "detail": "字幕已烧录进画面" if burned else "当前 ffmpeg 不支持字幕烧录（缺 libass），字幕以可开关字幕轨嵌入；发抖音等平台前请安装带 libass 的 ffmpeg",
    })
    manifest = build_manifest(root, plan, assets, final, checks, models_used)
    _write_json(root / "manifest.json", manifest)
    return {
        "status": "rendered" if all(check["ok"] for check in checks) else "rendered_with_issues",
        "video_path": str(final),
        "subtitles_path": str(srt),
        "manifest_path": str(root / "manifest.json"),
        "duration": plan.duration,
        "checks": checks,
    }


# ── 5. Verify ────────────────────────────────────────────────────────────────

def _ffprobe_streams(path: str) -> Dict[str, Any]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration", "-of", "json", path],
        capture_output=True, text=True, timeout=30,
    )
    return json.loads(result.stdout or "{}")


def _detect(path: str, filter_args: List[str], marker: str) -> int:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, *filter_args, "-f", "null", "-"],
                            capture_output=True, text=True, timeout=300)
    return result.stderr.count(marker)


def verify_video(path: str, plan: EditPlan) -> List[Dict[str, Any]]:
    """What a person would check before posting, measured on the file itself."""
    info = _ffprobe_streams(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = float((info.get("format") or {}).get("duration") or 0)
    width, height = FRAME_SIZE[plan.aspect_ratio]
    black = _detect(path, ["-vf", "blackdetect=d=0.5:pix_th=0.10", "-an"], "black_start")
    silence = _detect(path, ["-af", "silencedetect=n=-45dB:d=1.5", "-vn"], "silence_start") if has_audio else 0
    return [
        {"check": "duration", "ok": abs(duration - plan.duration) <= 0.6, "detail": f"成片 {duration:.1f}s，分镜 {plan.duration:.1f}s"},
        {"check": "frame_size", "ok": (video.get("width"), video.get("height")) == (width, height), "detail": f"{video.get('width')}x{video.get('height')}，应为 {width}x{height}"},
        {"check": "audio", "ok": has_audio, "detail": "有音轨" if has_audio else "没有音轨"},
        {"check": "black_frames", "ok": black == 0, "detail": f"{black} 段超过 0.5 秒的黑帧"},
        {"check": "long_silence", "ok": silence == 0, "detail": f"{silence} 段超过 1.5 秒的静音"},
        {"check": "pending_beats", "ok": not any(b.pending for b in plan.beats),
         "detail": "无待确认镜头" if not any(b.pending for b in plan.beats) else "待确认：" + "、".join(b.id for b in plan.beats if b.pending)},
    ]


# ── 6. Manifest ──────────────────────────────────────────────────────────────

def build_manifest(root: Path, plan: EditPlan, assets: Dict[str, Asset], final: Path, checks: List[Dict[str, Any]], models: Dict[str, str]) -> Dict[str, Any]:
    used = {beat.asset_id for beat in plan.beats if beat.source in ("asset", "animate")}
    return {
        "project_id": plan.project_id,
        "title": plan.title,
        "platform": plan.platform,
        "aspect_ratio": plan.aspect_ratio,
        "duration": plan.duration,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "video": {"path": str(final), "sha256": _sha256(final)},
        "assets": [
            {"id": asset.id, "file": Path(asset.path).name, "sha256": _sha256(Path(asset.path)), "rights_note": asset.rights_note}
            for asset in assets.values() if asset.id in used
        ],
        "generated_seconds": plan.generated_seconds(),
        "models": models,
        "checks": checks,
        "pending": [beat.id for beat in plan.beats if beat.pending],
        "notes": plan.notes,
    }
