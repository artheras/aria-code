"""The edit plan: the one document every stage of the video workflow reads and writes.

Its beat fields follow the short-form-video-editing skill's edit-plan schema
(id, start, end, purpose, visual, copy, caption, pending) so the plan can be
checked by that skill's release gate. Additions the renderer needs — where the
visual comes from, the camera move, the narration — live beside them.

A plan is what the user reviews before anything is rendered or paid for, so
validation here is strict and its messages are meant to be read by a person.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

PURPOSES = ("hook", "evidence", "conclusion", "cta")
EFFECTS = ("zoom_in", "zoom_out", "pan_left", "pan_right")
ASPECT_RATIOS = ("9:16", "16:9", "1:1")
SOURCE_KINDS = ("asset", "generate", "animate")
# Billed per second by Veo. "animate" starts from one of the user's own images
# (image-to-video), so the shot keeps that image instead of inventing a new one.
PAID_SOURCES = ("generate", "animate")

# Platform → default aspect ratio, from the skill's platform specs.
PLATFORM_ASPECT = {
    "douyin": "9:16", "tiktok": "9:16", "xiaohongshu": "9:16", "reels": "9:16", "shorts": "9:16",
    "bilibili": "16:9", "youtube": "16:9", "x": "16:9", "linkedin": "1:1",
}

# Veo produces short clips; a generated beat longer than this is split or refused.
MAX_GENERATED_SECONDS = 8


@dataclass
class Asset:
    id: str
    path: str
    kind: str  # "image" | "video"
    description: str = ""
    subjects: List[str] = field(default_factory=list)
    text_in_frame: str = ""
    orientation: str = ""  # "portrait" | "landscape" | "square"
    suggested_use: str = ""
    rights_note: str = ""  # anything the user must confirm before release


@dataclass
class Beat:
    id: str
    start: float
    end: float
    purpose: str
    visual: str  # what the viewer sees, in words
    source: str = "asset"  # "asset" | "generate" | "animate"
    asset_id: str = ""  # when source is "asset" or "animate"
    generate_prompt: str = ""  # when source is "generate", or the motion for "animate"
    effect: str = "zoom_in"
    copy: str = ""  # narration spoken over the beat
    caption: str = ""  # on-screen text; defaults to the narration
    pending: bool = False  # something the user must still supply or approve

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


@dataclass
class EditPlan:
    project_id: str
    title: str
    platform: str
    aspect_ratio: str
    language: str
    beats: List[Beat]
    voice: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return round(self.beats[-1].end, 3) if self.beats else 0.0

    def generated_seconds(self) -> float:
        return round(sum(beat.duration for beat in self.beats if beat.source in PAID_SOURCES), 3)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["duration"] = self.duration
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EditPlan":
        beats = [Beat(**{key: value for key, value in beat.items() if key in Beat.__dataclass_fields__}) for beat in data.get("beats", [])]
        fields = {key: value for key, value in data.items() if key in cls.__dataclass_fields__ and key != "beats"}
        return cls(beats=beats, **fields)

    def retime(self) -> None:
        """Lay the beats end to end from 0, keeping each beat's duration."""
        cursor = 0.0
        for beat in self.beats:
            length = beat.duration
            beat.start = round(cursor, 3)
            beat.end = round(cursor + length, 3)
            cursor = beat.end


def validate_plan(plan: EditPlan, assets: Dict[str, Asset]) -> List[str]:
    """Problems that block rendering, as sentences. Empty means renderable."""
    problems: List[str] = []
    if plan.aspect_ratio not in ASPECT_RATIOS:
        problems.append(f"Aspect ratio {plan.aspect_ratio!r} is not one of {', '.join(ASPECT_RATIOS)}.")
    if not plan.beats:
        problems.append("The plan has no beats.")
        return problems
    expected_start = 0.0
    seen = set()
    for beat in plan.beats:
        label = f"Beat {beat.id}"
        if beat.id in seen:
            problems.append(f"{label} appears twice.")
        seen.add(beat.id)
        if beat.purpose not in PURPOSES:
            problems.append(f"{label}: purpose {beat.purpose!r} is not one of {', '.join(PURPOSES)}.")
        if beat.end <= beat.start:
            problems.append(f"{label} ends before it starts.")
        if abs(beat.start - expected_start) > 0.05:
            problems.append(f"{label} starts at {beat.start}s but the previous beat ends at {expected_start}s.")
        expected_start = beat.end
        if beat.effect not in EFFECTS:
            problems.append(f"{label}: camera move {beat.effect!r} is not one of {', '.join(EFFECTS)}.")
        if beat.source not in SOURCE_KINDS:
            problems.append(f"{label}: source {beat.source!r} must be one of {', '.join(SOURCE_KINDS)}.")
        elif beat.source in ("asset", "animate") and beat.asset_id not in assets:
            problems.append(f"{label} uses asset {beat.asset_id!r}, which is not among the provided files.")
        if beat.source == "animate" and beat.asset_id in assets and assets[beat.asset_id].kind != "image":
            problems.append(f"{label} animates {beat.asset_id!r}, which is a video; only images can be animated.")
        if beat.source in PAID_SOURCES:
            if not beat.generate_prompt.strip():
                problems.append(f"{label} is a generated shot without a prompt." if beat.source == "generate"
                                else f"{label} animates an image without describing the motion.")
            if beat.duration > MAX_GENERATED_SECONDS:
                problems.append(f"{label} is a generated shot of {beat.duration}s; generated shots are at most {MAX_GENERATED_SECONDS}s.")
    if plan.beats[0].purpose != "hook":
        problems.append("The first beat should be the hook.")
    # Variety: a still repeated shot after shot reads as a slideshow of one image.
    run_asset, run_length = "", 0
    for beat in plan.beats:
        current = beat.asset_id if beat.source in ("asset", "animate") else ""
        run_length = run_length + 1 if current and current == run_asset else 1
        run_asset = current
        if current and run_length == 3:
            problems.append(f"Asset {current} is used in 3 beats in a row (up to {beat.id}); alternate it with other assets.")
    used = {beat.asset_id for beat in plan.beats if beat.source in ("asset", "animate")}
    unused = [asset_id for asset_id in assets if asset_id not in used]
    if unused and len(plan.beats) >= len(assets):
        problems.append(f"Assets {', '.join(unused)} are not used although the video has room for them.")
    return problems


def to_srt(plan: EditPlan, max_chars: int = 18) -> str:
    """Subtitles from the beats' captions, split into readable lines across each beat."""
    def stamp(seconds: float) -> str:
        millis = int(round(seconds * 1000))
        hours, rest = divmod(millis, 3_600_000)
        minutes, rest = divmod(rest, 60_000)
        secs, ms = divmod(rest, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"

    def chunks(text: str) -> List[str]:
        text = " ".join(text.split())
        if not text:
            return []
        pieces, current = [], ""
        for char in text:
            current += char
            breakable = char in "，。！？；、,.!?; " and len(current) >= max_chars * 0.5
            if len(current) >= max_chars or breakable:
                pieces.append(current.strip())
                current = ""
        if current.strip():
            pieces.append(current.strip())
        return pieces

    entries: List[str] = []
    index = 1
    for beat in plan.beats:
        lines = chunks(beat.caption or beat.copy)
        if not lines:
            continue
        total = sum(len(line) for line in lines)
        cursor = beat.start
        for line in lines:
            span = beat.duration * len(line) / total
            entries.append(f"{index}\n{stamp(cursor)} --> {stamp(min(beat.end, cursor + span))}\n{line}\n")
            cursor += span
            index += 1
    return "\n".join(entries)


def default_aspect(platform: str, requested: Optional[str] = None) -> str:
    if requested in ASPECT_RATIOS:
        return requested
    return PLATFORM_ASPECT.get((platform or "").strip().lower(), "9:16")
