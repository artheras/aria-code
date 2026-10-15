"""service.py — 视频背景音乐 (BGM) 与音效智能管理服务。

核心功能：
1. 情绪与风格智能选曲：根据剧本文案与视觉风格（Keynote发布会、科技极简、萌宠轻快、史诗电影等）
   自动分析匹配最契合的背景音乐风格。
2. 免版税与本地音乐库索引：统一管理本地存储 (~/.aria-code/music 或 assets/music) 的免版税音频资产。
3. 程序化高保真氛围音轨合成 (Procedural Ambient Synth)：零依赖、零外链，可在本地瞬时合成指定时长的
   苹果发布会质感 44.1kHz 立体声环境背景音轨。
4. AI 生成式配乐 (MusicGen / Suno) 提示词编排：为后续接入云端生成式音乐大模型提供专业的 Prompt 生成器。
"""

from __future__ import annotations

import json
import logging
import math
import os
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MusicTrackMetadata:
    id: str
    title: str
    genre: str  # ambient, electronic, acoustic, orchestral, lofi
    mood: str   # professional, futuristic, cute, epic, chill
    bpm: int = 90
    duration_seconds: float = 30.0
    file_path: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    license: str = "Royalty-Free / CC0"


class MusicLibraryService:
    """背景音乐与音频资产管理服务单例。"""

    _instance: Optional[MusicLibraryService] = None

    STYLE_MOOD_MAP = {
        "apple_keynote": {"genre": "ambient", "mood": "professional", "bpm": 80, "desc": "Apple keynote ambient synthesizer, C major 7th chords, minimalist, spacious, inspiring"},
        "tech": {"genre": "electronic", "mood": "futuristic", "bpm": 110, "desc": "Modern tech, crisp digital synth plucks, clean bassline, innovation narrative"},
        "cute": {"genre": "acoustic", "mood": "cute", "bpm": 105, "desc": "Playful acoustic, wooden marimba, ukulele, cheerful, warm, bouncy pet style"},
        "cinematic": {"genre": "orchestral", "mood": "epic", "bpm": 75, "desc": "Cinematic orchestral swelling, deep cello, expansive cinematic atmosphere"},
        "chill": {"genre": "lofi", "mood": "chill", "bpm": 85, "desc": "Warm lo-fi chill hip-hop, vinyl crackle, gentle Rhodes piano, relaxing vibe"},
    }

    def __init__(self, custom_library_dir: Optional[str] = None):
        self.library_dir = Path(custom_library_dir or os.path.expanduser("~/.aria-code/music"))
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self._tracks: Dict[str, MusicTrackMetadata] = {}
        self._load_local_library()

    @classmethod
    def get_instance(cls) -> MusicLibraryService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load_local_library(self) -> None:
        """扫描本地音乐库目录下的音频文件与元数据。"""
        meta_file = self.library_dir / "catalog.json"
        if meta_file.exists():
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8"))
                for item in data.get("tracks", []):
                    self._tracks[item["id"]] = MusicTrackMetadata(**item)
            except Exception as exc:
                logger.warning(f"[MusicLibraryService] Failed to parse {meta_file}: {exc}")

        # 扫描目录下的 mp3 / wav 文件
        for ext in ("*.mp3", "*.wav", "*.m4a"):
            for audio_file in self.library_dir.glob(ext):
                tid = audio_file.stem
                if tid not in self._tracks:
                    self._tracks[tid] = MusicTrackMetadata(
                        id=tid,
                        title=tid.replace("_", " ").title(),
                        genre="general",
                        mood="ambient",
                        file_path=str(audio_file),
                    )

    def analyze_script_mood(self, script: str, style: str = "apple_keynote") -> Dict[str, Any]:
        """根据脚本词汇与视频风格推断配乐情绪与匹配标准。"""
        script_lower = script.lower()
        matched_style = style.lower()

        # 关键字启发式推导
        if any(w in script_lower for w in ["发布会", "keynote", "apple", "旗舰", "pro", "极简", "优雅"]):
            matched_style = "apple_keynote"
        elif any(w in script_lower for w in ["猫", "狗", "萌", "可爱", "taco", "cute", "pet", "bunny", "兔"]):
            matched_style = "cute"
        elif any(w in script_lower for w in ["芯片", "算法", "ai", "算力", "科技", "智能", "未来"]):
            matched_style = "tech"
        elif any(w in script_lower for w in ["震撼", "史诗", "宇宙", "宏大", "征程", "突破"]):
            matched_style = "cinematic"

        spec = self.STYLE_MOOD_MAP.get(matched_style, self.STYLE_MOOD_MAP["apple_keynote"])
        return {
            "style": matched_style,
            "genre": spec["genre"],
            "mood": spec["mood"],
            "target_bpm": spec["bpm"],
            "ai_prompt": spec["desc"],
        }

    def match_or_synthesize_bgm(
        self,
        script: str,
        style: str = "apple_keynote",
        duration_seconds: float = 20.0,
        output_dir: Optional[str] = None,
    ) -> str:
        """获取或合成与脚本最匹配的背景音乐，返回音频文件绝对路径。"""
        mood_spec = self.analyze_script_mood(script, style)
        matched_genre = mood_spec["genre"]

        # 1. 如果本地库有匹配的音频文件，优先使用
        for track in self._tracks.values():
            if track.genre == matched_genre and track.file_path and os.path.exists(track.file_path):
                logger.info(f"[MusicLibraryService] Using matched local library track: {track.title}")
                return track.file_path

        # 2. 如果没有本地预录文件，运行程序化高保真合成 (零依赖快速产出)
        target_dir = Path(output_dir) if output_dir else self.library_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = f"bgm_{mood_spec['style']}_{int(duration_seconds)}s.wav"
        out_path = target_dir / filename

        if not out_path.exists():
            logger.info(f"[MusicLibraryService] Synthesizing procedural ambient keynote track ({duration_seconds}s)...")
            self.synthesize_ambient_synth_bgm(str(out_path), duration_seconds=duration_seconds)

        return str(out_path)

    @classmethod
    def synthesize_ambient_synth_bgm(
        cls,
        output_path: str,
        duration_seconds: float = 25.0,
        sample_rate: int = 44100,
    ) -> str:
        """纯 Python 程序化合成 44.1kHz 立体声苹果发布会风格环境氛围音轨 (Cmaj7 -> Am9)。"""
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        total_samples = int(duration_seconds * sample_rate)
        # 苹果发布会经典和弦走带: Cmaj7 (C, E, G, B) -> Am9 (A, C, E, G, B) -> Fmaj7 -> Gsus4
        chords = [
            [261.63, 329.63, 392.00, 493.88],  # Cmaj7
            [220.00, 261.63, 329.63, 392.00],  # Am7/Am9
            [174.61, 220.00, 261.63, 329.63],  # Fmaj7
            [196.00, 261.63, 293.66, 392.00],  # Gsus4
        ]

        chord_dur = 6.0  # 每个和弦 6 秒展开
        frames = bytearray()

        for i in range(total_samples):
            t = i / sample_rate
            chord_idx = int((t / chord_dur) % len(chords))
            chord = chords[chord_idx]

            # 和弦内插淡入淡出包络
            t_in_chord = t % chord_dur
            env = math.sin(math.pi * min(1.0, t_in_chord / chord_dur)) ** 0.6

            # 整体首尾淡入淡出
            fade_in = min(1.0, t / 1.5)
            fade_out = min(1.0, (duration_seconds - t) / 2.0)
            master_vol = 0.28 * fade_in * fade_out * env

            left_sample = 0.0
            right_sample = 0.0

            # 4个声音分音合唱 (Chorus & Stereo Detune)
            for idx, freq in enumerate(chord):
                # 微音分失谐 (Detuning) 营造空灵大空间感
                freq_l = freq * (1.0 + 0.0012 * (idx - 1.5))
                freq_r = freq * (1.0 - 0.0012 * (idx - 1.5))

                phase_l = 2.0 * math.pi * freq_l * t
                phase_r = 2.0 * math.pi * freq_r * t

                # 正弦基波 + 微弱次泛音 (Warm Ambient)
                tone_l = 0.7 * math.sin(phase_l) + 0.2 * math.sin(phase_l * 2) + 0.1 * math.sin(phase_l * 0.5)
                tone_r = 0.7 * math.sin(phase_r) + 0.2 * math.sin(phase_r * 2) + 0.1 * math.sin(phase_r * 0.5)

                # 立体声微向展开
                pan_bias = (idx / 3.0) - 0.5  # -0.5 ~ +0.5
                left_sample += tone_l * (0.6 - pan_bias * 0.3)
                right_sample += tone_r * (0.6 + pan_bias * 0.3)

            # 压限防止削波
            left_val = int(max(-32767, min(32767, left_sample * master_vol * 12000)))
            right_val = int(max(-32767, min(32767, right_sample * master_vol * 12000)))

            frames.extend(struct.pack("<hh", left_val, right_val))

        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(frames)

        return str(out_path)
