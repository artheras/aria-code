"""Layer 1 of the video architecture: deterministic ffmpeg-based editing —
trim, concat, overlay text, overlay audio, convert format/aspect, change
speed. No AI, no model weights, no API key — just a subprocess wrapper
around the `ffmpeg`/`ffprobe` CLIs, same pattern as project_tools.py's
`_git()` helper (timeout-bounded, capture output, never raise — return an
error dict instead).

Layer 2 (video_analysis.py, local Whisper transcription + opencv scene
detection) and Layer 3 (cloud AI generation, not built yet — needs a
provider/budget decision first) are separate modules.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_TIMEOUT = 300.0


def _require_ffmpeg() -> Optional[str]:
    if shutil.which("ffmpeg") is None:
        return "ffmpeg not found on PATH. Install it: brew install ffmpeg"
    return None


def _run_ffmpeg(args: List[str], *, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """Run an ffmpeg command. Returns {"success": bool, "error"?: str}."""
    missing = _require_ffmpeg()
    if missing:
        return {"success": False, "error": missing}
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"ffmpeg timed out after {timeout:.0f}s"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if result.returncode != 0:
        return {"success": False, "error": result.stderr[-800:]}
    return {"success": True}


def _default_output(input_path: str, suffix: str, ext: Optional[str] = None) -> Path:
    src = Path(input_path)
    from artifacts import create_user_artifact

    artifact = create_user_artifact("video", src.stem, f"{src.stem}_{suffix}", ext or src.suffix)
    return artifact.path


def probe_video(input_path: str) -> Dict[str, Any]:
    """Return duration/resolution/codec info via ffprobe."""
    if shutil.which("ffprobe") is None:
        return {"success": False, "error": "ffprobe not found on PATH. Install it: brew install ffmpeg"}
    if not Path(input_path).exists():
        return {"success": False, "error": f"File not found: {input_path}"}
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=width,height,codec_type,codec_name,r_frame_rate:format=duration,size",
             "-of", "json", input_path],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if result.returncode != 0:
        return {"success": False, "error": result.stderr[-500:]}
    import json
    return {"success": True, **json.loads(result.stdout)}


def trim_video(input_path: str, start: float, end: float, *, output_path: Optional[str] = None) -> Dict[str, Any]:
    """Cut [start, end] seconds out of a video (re-encodes for frame-accurate cuts)."""
    if not Path(input_path).exists():
        return {"success": False, "error": f"File not found: {input_path}"}
    if end <= start:
        return {"success": False, "error": "end must be greater than start"}
    out = Path(output_path) if output_path else _default_output(input_path, "trimmed")
    result = _run_ffmpeg([
        "-ss", str(start), "-to", str(end), "-i", input_path,
        "-c:v", "libx264", "-c:a", "aac", str(out),
    ])
    if not result["success"]:
        return result
    return {"success": True, "path": str(out)}


def concat_videos(input_paths: List[str], *, output_path: Optional[str] = None) -> Dict[str, Any]:
    """Concatenate multiple videos in order. Re-encodes each to a common
    codec first so mismatched source formats (different codec/resolution)
    don't silently produce a broken concat — the naive ffmpeg concat demuxer
    only works when all inputs already share a codec."""
    for p in input_paths:
        if not Path(p).exists():
            return {"success": False, "error": f"File not found: {p}"}
    if len(input_paths) < 2:
        return {"success": False, "error": "Need at least 2 videos to concatenate"}

    out = Path(output_path) if output_path else _default_output(input_paths[0], "concat")
    filter_inputs = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(len(input_paths)))
    args = []
    for p in input_paths:
        args += ["-i", p]
    args += [
        "-filter_complex", f"{filter_inputs}concat=n={len(input_paths)}:v=1:a=1[outv][outa]",
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-c:a", "aac", str(out),
    ]
    result = _run_ffmpeg(args)
    if not result["success"]:
        return result
    return {"success": True, "path": str(out)}


def overlay_text(
    input_path: str,
    text: str,
    *,
    position: str = "bottom",
    font_size: int = 36,
    font_color: str = "white",
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Burn a text overlay onto the video.

    Renders the text as a transparent PNG via Pillow and composites it with
    ffmpeg's `overlay` filter, rather than ffmpeg's own `drawtext` filter —
    `drawtext` needs ffmpeg built with libfreetype, which isn't guaranteed
    (confirmed absent on a stock Homebrew ffmpeg install: `No such filter:
    'drawtext'`). Rendering the text ourselves works on any ffmpeg build.
    """
    if not Path(input_path).exists():
        return {"success": False, "error": f"File not found: {input_path}"}

    probe = probe_video(input_path)
    if not probe["success"]:
        return probe
    video_stream = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video_stream:
        return {"success": False, "error": "Could not determine video dimensions"}
    width, height = int(video_stream["width"]), int(video_stream["height"])

    from PIL import Image, ImageDraw, ImageFont

    overlay_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay_img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    margin = 40
    positions = {
        "top": ((width - text_w) // 2, margin),
        "bottom": ((width - text_w) // 2, height - text_h - margin),
        "center": ((width - text_w) // 2, (height - text_h) // 2),
    }
    x, y = positions.get(position, positions["bottom"])
    pad = 10
    draw.rectangle([x - pad, y - pad, x + text_w + pad, y + text_h + pad], fill=(0, 0, 0, 128))
    draw.text((x, y), text, font=font, fill=font_color)

    out = Path(output_path) if output_path else _default_output(input_path, "text")
    with tempfile.TemporaryDirectory() as tmpdir:
        overlay_path = str(Path(tmpdir) / "overlay.png")
        overlay_img.save(overlay_path)
        result = _run_ffmpeg([
            "-i", input_path, "-i", overlay_path,
            "-filter_complex", "[0:v][1:v]overlay=0:0",
            "-c:a", "copy", str(out),
        ])
    if not result["success"]:
        return result
    return {"success": True, "path": str(out)}


def overlay_audio(input_path: str, audio_path: str, *, replace: bool = False, output_path: Optional[str] = None) -> Dict[str, Any]:
    """Add or replace the audio track. replace=True drops the original audio
    entirely; replace=False mixes the new track under the original."""
    if not Path(input_path).exists():
        return {"success": False, "error": f"File not found: {input_path}"}
    if not Path(audio_path).exists():
        return {"success": False, "error": f"File not found: {audio_path}"}

    out = Path(output_path) if output_path else _default_output(input_path, "audio")
    if replace:
        args = ["-i", input_path, "-i", audio_path, "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-shortest", str(out)]
    else:
        args = ["-i", input_path, "-i", audio_path,
                "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first[a]",
                "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-shortest", str(out)]
    result = _run_ffmpeg(args)
    if not result["success"]:
        return result
    return {"success": True, "path": str(out)}


def convert_video(
    input_path: str,
    *,
    output_format: Optional[str] = None,
    aspect: Optional[str] = None,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert container/codec format and/or reframe to a target aspect
    ratio (e.g. "9:16" for vertical social, "1:1" for square) via
    crop-to-fill — center-crops rather than letterboxing, since a cropped
    fill is what "reframe for a platform" almost always means in practice."""
    if not Path(input_path).exists():
        return {"success": False, "error": f"File not found: {input_path}"}

    ext = f".{output_format}" if output_format else None
    out = Path(output_path) if output_path else _default_output(input_path, "converted", ext)

    vf = None
    if aspect:
        try:
            aw, ah = (int(x) for x in aspect.split(":"))
        except ValueError:
            return {"success": False, "error": "aspect must be like '9:16'"}
        vf = f"crop='min(iw,ih*{aw}/{ah})':'min(ih,iw*{ah}/{aw})',scale=trunc(iw/2)*2:trunc(ih/2)*2"

    args = ["-i", input_path]
    if vf:
        args += ["-vf", vf]
    args += ["-c:v", "libx264", "-c:a", "aac", str(out)]
    result = _run_ffmpeg(args)
    if not result["success"]:
        return result
    return {"success": True, "path": str(out)}


def change_speed(input_path: str, factor: float, *, output_path: Optional[str] = None) -> Dict[str, Any]:
    """Speed up (factor > 1) or slow down (factor < 1) a video, audio pitch-
    corrected. atempo only accepts 0.5-2.0 per filter pass, so factors
    outside that range are chained across multiple atempo stages."""
    if not Path(input_path).exists():
        return {"success": False, "error": f"File not found: {input_path}"}
    if factor <= 0:
        return {"success": False, "error": "factor must be positive"}

    out = Path(output_path) if output_path else _default_output(input_path, "speed")
    atempo_stages = []
    remaining = factor
    while remaining > 2.0:
        atempo_stages.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        atempo_stages.append(0.5)
        remaining /= 0.5
    atempo_stages.append(remaining)
    atempo_filter = ",".join(f"atempo={s:.4f}" for s in atempo_stages)

    result = _run_ffmpeg([
        "-i", input_path,
        "-filter_complex", f"[0:v]setpts={1/factor:.6f}*PTS[v];[0:a]{atempo_filter}[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-c:a", "aac", str(out),
    ])
    if not result["success"]:
        return result
    return {"success": True, "path": str(out)}


def create_ken_burns_clip(
    image_path: str,
    *,
    duration: float = 4.0,
    effect: str = "zoom_in",
    aspect_ratio: str = "16:9",
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Animate a static image with a smooth Ken Burns pan/zoom camera motion into an MP4 clip.

    Guarantees 100% local, free, zero-API-cost video creation for storyboards and social cuts.
    Includes a silent stereo audio track so concat with speech-backed clips works seamlessly.
    """
    if not Path(image_path).exists():
        return {"success": False, "error": f"Image file not found: {image_path}"}

    duration = max(1.0, float(duration or 4.0))
    fps = 25
    frames = int(duration * fps)

    if aspect_ratio == "9:16":
        width, height = 1080, 1920
    elif aspect_ratio == "1:1":
        width, height = 1080, 1080
    else:
        width, height = 1920, 1080

    if effect == "zoom_out":
        zoom_expr = "if(lte(zoom,1.0),1.2,max(1.001,zoom-0.0015))"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif effect == "pan_right":
        zoom_expr = "1.15"
        x_expr = "if(lte(on,1),(iw-iw/zoom)*0.2,x+0.5)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif effect == "pan_left":
        zoom_expr = "1.15"
        x_expr = "if(lte(on,1),(iw-iw/zoom)*0.8,x-0.5)"
        y_expr = "ih/2-(ih/zoom/2)"
    else:  # zoom_in
        zoom_expr = "min(zoom+0.0015,1.25)"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"

    out = Path(output_path) if output_path else _default_output(image_path, f"kenburns_{effect}", ".mp4")

    filter_str = (
        f"scale=8000:-1,"
        f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':d={frames}:s={width}x{height}:fps={fps},"
        f"format=yuv420p"
    )

    args = [
        "-loop", "1", "-i", str(image_path),
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-vf", filter_str,
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "128k",
        "-t", f"{duration:.2f}",
        "-pix_fmt", "yuv420p",
        str(out),
    ]

    result = _run_ffmpeg(args, timeout=180.0)
    if not result["success"]:
        return result
    return {
        "success": True,
        "path": str(out),
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "effect": effect,
    }


def burn_subtitles_and_audio(
    video_path: str,
    *,
    audio_path: Optional[str] = None,
    srt_path: Optional[str] = None,
    bgm_path: Optional[str] = None,
    bgm_volume: float = 0.25,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Overlay narration voice, background music (BGM), and/or subtitles onto a video."""
    if not Path(video_path).exists():
        return {"success": False, "error": f"Video file not found: {video_path}"}

    out = Path(output_path) if output_path else _default_output(video_path, "mastered", ".mp4")

    inputs = ["-i", video_path]
    filter_complex = []
    has_narration = audio_path and Path(audio_path).exists()
    has_bgm = bgm_path and Path(bgm_path).exists()

    audio_inputs_idx = 1
    if has_narration:
        inputs += ["-i", audio_path]
        narration_idx = audio_inputs_idx
        audio_inputs_idx += 1

    if has_bgm:
        inputs += ["-stream_loop", "-1", "-i", bgm_path]
        bgm_idx = audio_inputs_idx
        audio_inputs_idx += 1

    # Audio mixing
    if has_narration and has_bgm:
        filter_complex.append(
            f"[{bgm_idx}:a]volume={bgm_volume:.2f}[bgm];"
            f"[{narration_idx}:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        audio_map = ["[aout]"]
    elif has_narration:
        audio_map = [f"{narration_idx}:a"]
    elif has_bgm:
        filter_complex.append(f"[{bgm_idx}:a]volume={bgm_volume:.2f}[bgm]")
        audio_map = ["[bgm]"]
    else:
        audio_map = ["0:a?"]

    # Subtitles video filter
    vf_list = []
    if srt_path and Path(srt_path).exists():
        # Escape path for ffmpeg subtitles filter
        escaped_srt = str(Path(srt_path).resolve()).replace(":", "\\:").replace("'", "\\'")
        vf_list.append(f"subtitles='{escaped_srt}':force_style='FontSize=18,PrimaryColour=&HFFFFFF,BackColour=&H80000000,BorderStyle=4'")

    args = list(inputs)
    if filter_complex:
        args += ["-filter_complex", "".join(filter_complex)]
    if vf_list:
        args += ["-vf", ",".join(vf_list)]

    args += ["-map", "0:v:0"]
    for am in audio_map:
        args += ["-map", am]

    args += ["-c:v", "libx264", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)]
    result = _run_ffmpeg(args, timeout=300.0)
    if not result["success"]:
        return result
    return {"success": True, "path": str(out)}


def assemble_storyboard(
    storyboard: Dict[str, Any],
    *,
    output_path: Optional[str] = None,
    aspect_ratio: str = "16:9",
) -> Dict[str, Any]:
    """Assemble a full multi-shot storyboard into a finished video package.

    storyboard shape:
    {
        "title": "...",
        "aspect_ratio": "16:9" | "9:16",
        "shots": [
            {
                "shot_index": 1,
                "duration": 4.0,
                "clip_path": "/path/to/clip.mp4" (optional if source_image is set),
                "source_image": "/path/to/image.jpg" (used for Ken Burns if clip_path missing),
                "effect": "zoom_in" | "pan_right" | "pan_left" | "zoom_out",
                "caption": "...",
            },
            ...
        ],
        "audio_path": optional narration wav/mp3,
        "bgm_path": optional background music wav/mp3,
        "subtitles_srt": optional SRT file path or text
    }
    """
    shots = storyboard.get("shots") or []
    if not shots:
        return {"success": False, "error": "Storyboard has no shots defined"}

    aspect = storyboard.get("aspect_ratio") or aspect_ratio
    temp_clips: List[str] = []
    effects_cycle = ["zoom_in", "pan_right", "zoom_out", "pan_left"]

    try:
        for idx, shot in enumerate(shots):
            clip = shot.get("clip_path")
            if clip and Path(clip).exists():
                temp_clips.append(str(clip))
            elif shot.get("source_image") and Path(shot["source_image"]).exists():
                effect = shot.get("effect") or effects_cycle[idx % len(effects_cycle)]
                dur = float(shot.get("duration") or 4.0)
                ken_res = create_ken_burns_clip(
                    shot["source_image"],
                    duration=dur,
                    effect=effect,
                    aspect_ratio=aspect,
                )
                if not ken_res.get("success"):
                    return {"success": False, "error": f"Shot {idx + 1} Ken Burns failed: {ken_res.get('error')}"}
                temp_clips.append(ken_res["path"])
            else:
                return {
                    "success": False,
                    "error": f"Shot {idx + 1} has neither valid 'clip_path' nor 'source_image'",
                }

        # 1. Concatenate shot clips
        if len(temp_clips) == 1:
            concat_path = temp_clips[0]
        else:
            concat_res = concat_videos(temp_clips)
            if not concat_res.get("success"):
                return concat_res
            concat_path = concat_res["path"]

        # 2. Master audio & subtitles if present
        audio_p = storyboard.get("audio_path")
        bgm_p = storyboard.get("bgm_path")
        srt_p = storyboard.get("subtitles_srt")

        # If srt content is raw text instead of a file path, write to temporary file
        srt_file: Optional[str] = None
        if srt_p and not Path(srt_p).exists() and ("-->" in srt_p):
            from artifacts import create_user_artifact
            srt_art = create_user_artifact("text", "subtitles", "captions", ".srt")
            srt_art.path.write_text(srt_p, encoding="utf-8")
            srt_file = str(srt_art.path)
        elif srt_p and Path(srt_p).exists():
            srt_file = str(srt_p)

        if audio_p or bgm_p or srt_file:
            final_res = burn_subtitles_and_audio(
                concat_path,
                audio_path=audio_p,
                bgm_path=bgm_p,
                srt_path=srt_file,
                output_path=output_path,
            )
            return final_res

        if output_path and output_path != concat_path:
            shutil.copy2(concat_path, output_path)
            return {"success": True, "path": output_path, "shots_count": len(shots)}

        return {"success": True, "path": concat_path, "shots_count": len(shots)}
    except Exception as exc:
        return {"success": False, "error": f"Failed to assemble storyboard: {exc}"}
