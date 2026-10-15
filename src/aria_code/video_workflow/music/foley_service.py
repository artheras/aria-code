"""foley_service.py — 影视级拟音声效 (Foley SFX) 与转场音效服务。

为视频自动识别并在精确毫秒时间码插入音效：
- whoosh: 镜头快速推拉或转场切换时的空气扫风声；
- glint: 金属墨镜反光、高光扫掠或极光破晓时的清脆晶莹音效；
- impact: 标题出字或产品霸气定格时的低音共振冲击 (Sub-bass impact)；
- click: 极简机械按键或聚光灯开启声。
"""

from __future__ import annotations

import logging
import math
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class FoleyCue:
    sound_type: str  # "whoosh" | "glint" | "impact" | "click"
    timestamp_seconds: float
    volume: float = 0.5
    duration: float = 0.8
    description: str = ""


class FoleySoundService:
    """拟音声效编排与程序化生成服务。"""

    @classmethod
    def generate_cues_for_storyboard(cls, shots: List[Dict[str, Any]]) -> List[FoleyCue]:
        """依据分镜时序与动作特征自动打点生成音效提示清单。"""
        cues: List[FoleyCue] = []
        cursor = 0.0

        for idx, shot in enumerate(shots):
            dur = float(shot.get("duration", 4.0))
            prompt = str(shot.get("prompt", "")).lower()

            # 镜头转场处（除首镜头外）插入转场扫风
            if idx > 0:
                cues.append(FoleyCue(
                    sound_type="whoosh",
                    timestamp_seconds=round(max(0.0, cursor - 0.15), 2),
                    volume=0.35,
                    description="Shot transition whoosh",
                ))

            # 包含光线反光或高光的镜头插入 Glint
            if any(k in prompt for k in ["sunglasses", "reflection", "flare", "shimmer", "glint", "反光", "高光"]):
                cues.append(FoleyCue(
                    sound_type="glint",
                    timestamp_seconds=round(cursor + dur * 0.4, 2),
                    volume=0.4,
                    description="Specular highlight glint shimmer",
                ))

            # 压轴定格或 Hook 开头插入 Sub-impact
            if idx == 0 or idx == len(shots) - 1:
                cues.append(FoleyCue(
                    sound_type="impact",
                    timestamp_seconds=round(cursor + 0.2, 2),
                    volume=0.45,
                    description="Cinematic low-end keynote presence",
                ))

            cursor += dur

        return cues

    @classmethod
    def synthesize_cue_track(
        cls,
        cues: List[FoleyCue],
        total_duration: float,
        output_path: str,
        sample_rate: int = 44100,
    ) -> str:
        """程序化合成包含所有 Foley 打点音效的独立音轨。"""
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        total_samples = int(total_duration * sample_rate)
        samples = [0.0] * total_samples

        for cue in cues:
            start_sample = int(cue.timestamp_seconds * sample_rate)
            if start_sample >= total_samples:
                continue

            cue_samples = int(cue.duration * sample_rate)
            for s in range(cue_samples):
                idx = start_sample + s
                if idx >= total_samples:
                    break

                t = s / sample_rate
                # 根据不同音效类型生成对应的正弦频移波形
                if cue.sound_type == "whoosh":
                    # 扫频白噪过滤带通效果
                    freq = 200.0 + 600.0 * math.sin(math.pi * (t / cue.duration))
                    val = 0.5 * math.sin(2 * math.pi * freq * t) * math.sin(math.pi * (t / cue.duration))
                elif cue.sound_type == "glint":
                    # 高频清脆钟音 2400Hz 快速衰减
                    val = math.sin(2 * math.pi * 2400 * t) * math.exp(-6.0 * t)
                elif cue.sound_type == "impact":
                    # 低频 60Hz -> 30Hz 重低音下潜
                    freq = 65.0 * math.exp(-2.5 * t)
                    val = math.sin(2 * math.pi * freq * t) * math.exp(-3.0 * t)
                else:
                    val = 0.0

                samples[idx] += val * cue.volume

        # 封包为 16-bit 双声道 PCM WAV
        frames = bytearray()
        for sample in samples:
            clamped = int(max(-32767, min(32767, sample * 16000)))
            frames.extend(struct.pack("<hh", clamped, clamped))

        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(frames)

        return str(out_path)
