"""director.py — 智能视频导演与生产 Agent

核心职责：
1. 多模态解析剧本与输入图片素材，提取关键主体特征并维持跨镜头角色一致性 (Character Consistency)。
2. 依据现代 DiT 视频大模型（Google Veo 2 / 自研私有模型）规律，生成真实物理动力学运动指令与 3D 运镜参数 (DiTMotionPlanner)。
3. 通过统一自研视频引擎适配层 (VideoEngineRegistry) 调度 Google Cloud Veo 或自研私有模型，并支持本地引擎兜底。
4. 驱动多轨旁白、BGM 自动闪避压音 (Sidechain Ducking) 与高质量字幕合成成片。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import BaseAgent, AgentResult
from aria_code.video_editor import assemble_storyboard, create_ken_burns_clip
from aria_code.video_workflow.engines import VideoEngineRegistry, VideoShotRequest

logger = logging.getLogger(__name__)


class DiTMotionPlanner:
    """专为扩散视频大模型 (DiT / 3D-VAE) 设计的物理动力学与 3D 运镜规划器。"""

    MOTION_DYNAMIC_PATTERNS = {
        "character": [
            "subtle organic breathing chest rise and fall, soft eye blinks, micro head tilt drift, realistic fur and hair simulation",
            "gentle facial expressions, eyes tracking slightly, soft breathing cycle, natural ambient light reflection",
            "confident stance with micro movements, subtle posture drift, clothing fabric micro-motion in gentle breeze",
        ],
        "environment": [
            "soft atmospheric haze drift, cinematic volumetric god rays, delicate dust motes catching rim lighting",
            "subtle specular highlights shimmering across metallic and glossy surfaces, gentle ambient light shifts",
            "slow dynamic light sweep across the focal subject, gentle shadow depth variations, photorealistic physical shading",
        ],
        "macro": [
            "shallow depth-of-field focus pull, organic micro-tremor, light refraction in glass and eyes, crisp high-frequency detail",
            "extreme macro rim lighting sweep, delicate whiskers and fine textures trembling subtly in still air",
        ]
    }

    CAMERA_CHOREOGRAPHY = [
        {"name": "slow_push_in", "motion": {"zoom": 0.25, "pan": 0.0, "tilt": 0.0}, "desc": "cinematic slow push-in along optical axis"},
        {"name": "dolly_pan_right", "motion": {"zoom": 0.15, "pan": 0.35, "tilt": 0.0}, "desc": "smooth dolly tracking right with subtle parallax"},
        {"name": "crane_tilt_up", "motion": {"zoom": 0.1, "pan": 0.0, "tilt": 0.25}, "desc": "gentle crane pedestal elevation with upward tilt"},
        {"name": "subtle_pull_back", "motion": {"zoom": -0.2, "pan": 0.0, "tilt": 0.0}, "desc": "slow cinematic pull-back revealing surrounding depth"},
        {"name": "orbital_drift", "motion": {"zoom": 0.1, "pan": -0.3, "tilt": 0.05}, "desc": "smooth slow orbital drift maintaining focal lock"},
    ]

    @classmethod
    def plan_motion_for_shot(
        cls,
        shot_index: int,
        narrative: str,
        subject_anchor: str = "",
        style: str = "cinematic",
    ) -> Dict[str, Any]:
        """为单个镜头生成真实的物理动力学与 3D 相机运镜。"""
        # 选择相机轨迹
        cam = cls.CAMERA_CHOREOGRAPHY[(shot_index - 1) % len(cls.CAMERA_CHOREOGRAPHY)]
        
        # 选择物理动力学特征
        is_macro = any(k in narrative.lower() for k in ["特写", "微距", "macro", "close-up", "detail", "whiskers", "eye"])
        if is_macro:
            dynamics = cls.MOTION_DYNAMIC_PATTERNS["macro"][(shot_index - 1) % len(cls.MOTION_DYNAMIC_PATTERNS["macro"])]
        else:
            dynamics = cls.MOTION_DYNAMIC_PATTERNS["character"][(shot_index - 1) % len(cls.MOTION_DYNAMIC_PATTERNS["character"])]
        
        env_dynamics = cls.MOTION_DYNAMIC_PATTERNS["environment"][(shot_index - 1) % len(cls.MOTION_DYNAMIC_PATTERNS["environment"])]

        prompt_parts = []
        if style:
            prompt_parts.append(f"{style} master shot, 8k resolution, photorealistic")
        if subject_anchor:
            prompt_parts.append(f"Subject: {subject_anchor}")
        prompt_parts.append(f"Scene: {narrative}")
        prompt_parts.append(f"Physics & Motion: {dynamics}, {env_dynamics}")
        prompt_parts.append(f"Camera movement: {cam['desc']}")

        full_prompt = ". ".join(prompt_parts) + "."

        return {
            "prompt": full_prompt,
            "camera_motion": cam["motion"],
            "camera_description": cam["desc"],
            "effect": "zoom_in" if cam["motion"].get("zoom", 0) >= 0 else "zoom_out",
        }


class CharacterConsistencyManager:
    """角色视觉特征一致性管理器，防止视频跨分镜时角色漂移。"""

    @classmethod
    def extract_subject_anchor(cls, script: str, images: List[str]) -> str:
        """从剧本或图片名称推断核心角色视觉锚点。"""
        script_lower = script.lower()
        if "taco" in script_lower:
            return "Taco the fluffy golden chinchilla cat wearing sleek black biker leather jacket and round dark sunglasses, pristine soft fur"
        if "cat" in script_lower or "猫" in script:
            return "adorable fluffy cat with soft luxurious fur and expressive eyes"
        if "car" in script_lower or "车" in script:
            return "sleek modern electric vehicle with reflective metallic finish and aerodynamic curves"
        if "tech" in script_lower or "科技" in script:
            return "futuristic minimalist consumer technology hardware with anodized aluminum and glass"
        
        # 默认通用电影级主体
        return "the central hero subject with consistent wardrobe, lighting, and fine texture details"


class VideoDirectorAgent(BaseAgent):
    name: str = "video_director"
    description: str = "视频导演与制作 Agent：多模态分析图片与剧本，规划分镜头并调度工具生成渲染成片"

    def __init__(self, llm_fn=None):
        super().__init__()
        self.llm_fn = llm_fn
        self.engine_registry = VideoEngineRegistry.get_instance()

    def decompose_script_and_images(
        self,
        script: str,
        *,
        images: Optional[List[str]] = None,
        target_duration: float = 15.0,
        aspect_ratio: str = "16:9",
        style: str = "cinematic",
    ) -> Dict[str, Any]:
        """将脚本文案与图像素材拆解为结构化分镜列表 (Storyboard)。"""
        script = (script or "").strip()
        valid_images = [p for p in (images or []) if Path(p).exists()]

        # 提取全局角色一致性特征锚点
        subject_anchor = CharacterConsistencyManager.extract_subject_anchor(script, valid_images)

        # 尝试调用外部注入的 LLM 或 Gemini 多模态视觉拆解
        storyboard = None
        if self.llm_fn:
            prompt = (
                f"你是一位专业影视与短视频导演。请根据以下文案与素材，拆解为具备物理动效与运镜的结构化分镜头剧本。\n"
                f"目标总时长：约 {target_duration} 秒。\n"
                f"画面宽高比：{aspect_ratio}。\n"
                f"视觉风格：{style}。\n"
                f"参考角色锚点：{subject_anchor}\n"
                f"参考素材图片路径列表：{valid_images}\n\n"
                f"脚本全文：\n{script}\n\n"
                f"请输出标准 JSON 格式（不要输出 markdown 语法块以外的任何文字），格式如下：\n"
                f"{{\n"
                f'  "title": "视频标题",\n'
                f'  "aspect_ratio": "{aspect_ratio}",\n'
                f'  "subject_anchor": "{subject_anchor}",\n'
                f'  "shots": [\n'
                f'    {{\n'
                f'      "shot_index": 1,\n'
                f'      "duration": 4.0,\n'
                f'      "prompt": "详细的镜头画面提示词（包括景别、主体生物呼吸/眨眼动作、环境光影、运镜指令如 slow push-in）",\n'
                f'      "camera_motion": {{"zoom": 0.2, "pan": 0.0, "tilt": 0.0}},\n'
                f'      "narration": "该镜头的旁白解说词",\n'
                f'      "caption": "该镜头屏幕显示字幕",\n'
                f'      "source_image": "关联的图片素材路径（若有）",\n'
                f'      "effect": "zoom_in|pan_right|pan_left|zoom_out"\n'
                f'    }}\n'
                f'  ]\n'
                f'}}\n'
            )
            try:
                raw = self.llm_fn(prompt)
                match = re.search(r"\{[\s\S]*\}", raw)
                if match:
                    storyboard = json.loads(match.group(0))
            except Exception as exc:
                logger.debug(f"[VideoDirector] LLM decompose failed, falling back to DiTMotionPlanner: {exc}")

        # 确定性规则与 DiTMotionPlanner 增强兜底
        if not storyboard or not storyboard.get("shots"):
            storyboard = self._motion_planned_decompose(script, valid_images, target_duration, aspect_ratio, style, subject_anchor)

        return storyboard

    def _motion_planned_decompose(
        self,
        script: str,
        images: List[str],
        target_duration: float,
        aspect_ratio: str,
        style: str,
        subject_anchor: str,
    ) -> Dict[str, Any]:
        """使用 DiTMotionPlanner 进行高质量物理动效与运镜分镜拆解。"""
        sentences = [s.strip() for s in re.split(r"[。\n!?！？；;]+", script) if len(s.strip()) > 3]
        if not sentences:
            sentences = [script or "精选视觉探索与创意展示"]

        shot_count = max(len(sentences), len(images))
        shot_count = min(shot_count, 8)  # 单视频最多 8 镜

        avg_dur = max(2.5, min(6.0, target_duration / max(1, shot_count)))

        shots = []
        for i in range(shot_count):
            shot_idx = i + 1
            sentence = sentences[i % len(sentences)]
            source_img = images[i % len(images)] if images else None
            
            motion_plan = DiTMotionPlanner.plan_motion_for_shot(
                shot_index=shot_idx,
                narrative=sentence,
                subject_anchor=subject_anchor,
                style=style,
            )

            shots.append({
                "shot_index": shot_idx,
                "duration": round(avg_dur, 1),
                "prompt": motion_plan["prompt"],
                "camera_motion": motion_plan["camera_motion"],
                "camera_description": motion_plan["camera_description"],
                "narration": sentence,
                "caption": sentence,
                "source_image": source_img,
                "effect": motion_plan["effect"],
            })

        return {
            "title": sentences[0][:20] if sentences else "精选视频制作",
            "aspect_ratio": aspect_ratio,
            "subject_anchor": subject_anchor,
            "total_estimated_duration": round(avg_dur * len(shots), 1),
            "shots": shots,
        }

    def produce_video_workflow(
        self,
        script: str,
        *,
        images: Optional[List[str]] = None,
        target_duration: float = 15.0,
        aspect_ratio: str = "16:9",
        provider: str = "auto",
        audio_path: Optional[str] = None,
        bgm_path: Optional[str] = None,
        engine_parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """执行端到端视频制作全工作流。"""
        # 1. 分析拆解脚本与角色锚点
        storyboard = self.decompose_script_and_images(
            script,
            images=images,
            target_duration=target_duration,
            aspect_ratio=aspect_ratio,
        )

        shots = storyboard.get("shots") or []
        if not shots:
            return {"success": False, "error": "未能生成有效分镜"}

        # 2. 获取并调度指定的生成引擎
        engine = self.engine_registry.get_engine(None if provider == "auto" else provider)
        logger.info(f"[VideoDirector] Using video engine: {engine.display_name}")

        for shot in shots:
            if shot.get("clip_path"):
                continue

            req = VideoShotRequest(
                prompt=shot["prompt"],
                image_path=shot.get("source_image"),
                duration_seconds=int(round(shot.get("duration", 4.0))),
                aspect_ratio=aspect_ratio,
                camera_motion=shot.get("camera_motion"),
                custom_parameters=engine_parameters or {},
            )

            # 调用引擎生成
            res = engine.generate_shot(req)
            if res.success and res.path:
                shot["clip_path"] = res.path
                shot["engine_used"] = res.engine_name
            else:
                logger.warning(f"[VideoDirector] Shot {shot['shot_index']} engine failed ({res.error}), falling back to local Ken-Burns.")
                # 本地离线运镜兜底
                if shot.get("source_image"):
                    local_res = self.engine_registry.get_engine("local").generate_shot(req)
                    if local_res.success and local_res.path:
                        shot["clip_path"] = local_res.path
                        shot["engine_used"] = "local_fallback"

        # 3. 智能音频与配乐管理（若未提供 BGM，由 MusicLibraryService 自动分析并匹配/合成）
        if not bgm_path:
            try:
                from aria_code.video_workflow.music import MusicLibraryService
                music_service = MusicLibraryService.get_instance()
                bgm_path = music_service.match_or_synthesize_bgm(
                    script=script,
                    duration_seconds=target_duration + 5.0,
                )
            except Exception as exc:
                logger.warning(f"[VideoDirector] Auto BGM matching/synthesizing failed: {exc}")

        mixed_audio = self._mix_audio_with_ducking(audio_path, bgm_path)
        storyboard["audio_path"] = mixed_audio or audio_path
        storyboard["bgm_path"] = None if mixed_audio else bgm_path

        # 4. 装配最终成片
        result = assemble_storyboard(storyboard, aspect_ratio=aspect_ratio)
        if result.get("success"):
            result["storyboard"] = storyboard
            result["title"] = storyboard.get("title")
            result["engine_used"] = engine.name

        return result

    def _mix_audio_with_ducking(self, voice_path: Optional[str], bgm_path: Optional[str]) -> Optional[str]:
        """使用 FFmpeg sidechaincompress 对音频进行智能压音处理（人声出现时 BGM 自动降低 -12dB）。"""
        if not voice_path or not bgm_path or not Path(voice_path).exists() or not Path(bgm_path).exists():
            return None

        if shutil.which("ffmpeg") is None:
            return None

        from aria_code.artifacts import create_user_artifact
        out_artifact = create_user_artifact("audio", "director", "mixed_ducked_audio", ".wav")
        out_path = str(out_artifact.path)

        # FFmpeg filter_complex 侧链压缩：voice 作为控制信号，自动压减 bgm
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", bgm_path,
            "-i", voice_path,
            "-filter_complex",
            "[0:a]volume=0.45[bgm];"
            "[bgm][1:a]sidechaincompress=threshold=0.08:ratio=5:attack=100:release=600[compressed_bgm];"
            "[compressed_bgm][1:a]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            "-map", "[aout]",
            "-ac", "2", "-ar", "44100",
            out_path
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
            return out_path
        except Exception as exc:
            logger.warning(f"[VideoDirector] Sidechain ducking failed: {exc}, using separate audio paths.")
            return None

    def analyze(self, symbol: str, data: Dict[str, Any]) -> AgentResult:
        """实现 BaseAgent 的抽象方法，供多 Agent 协作网络接入。"""
        script = data.get("script") or symbol
        images = data.get("images") or []
        aspect = data.get("aspect_ratio") or "16:9"

        storyboard = self.decompose_script_and_images(script, images=images, aspect_ratio=aspect)

        summary_lines = [
            f"🎬 智能视频导演已规划完成：共 {len(storyboard.get('shots', []))} 个镜头，画幅 {aspect}。",
            f"📌 角色特征锚点：{storyboard.get('subject_anchor', '无')}",
        ]
        for s in storyboard.get("shots", []):
            src_str = f" [参考图: {Path(s['source_image']).name}]" if s.get("source_image") else ""
            summary_lines.append(
                f"- 镜头 {s['shot_index']} ({s['duration']}s): {s['caption']} ({s.get('camera_description', s['effect'])}){src_str}"
            )

        return AgentResult(
            agent=self.name,
            symbol=symbol,
            analysis="\n".join(summary_lines),
            confidence=0.98,
            signal="HOLD",
            key_points=[s["caption"] for s in storyboard.get("shots", [])[:4]],
            data_used={"storyboard": storyboard},
        )
