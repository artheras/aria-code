"""session.py — 桌面端视频工程与会话中枢 (Video Project Session Manager)

专为桌面端（桌面 App / Electron / Tauri / MCP 协议调用）量身打造：
1. 项目工程状态持久化：以结构化 JSON 保存分镜卡片、素材关联、各镜头生成状态、音轨配置与成片记录。
2. 单镜头独立重生成 (Per-Shot Regeneration)：
   当用户对第 N 个镜头不满意时，仅重新生成该镜头的画面，其余镜头全部复用已有缓存，秒级快速重新缝合母带！
3. 状态轮询与进度推送：支持桌面端 UI 逐镜展示生成状态（draft -> generating -> completed -> failed）。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from aria_code.video_editor import assemble_storyboard
from aria_code.video_workflow.engines import VideoEngineRegistry, VideoShotRequest

logger = logging.getLogger(__name__)


@dataclass
class ShotCard:
    shot_index: int
    duration: float
    prompt: str
    narration: str
    caption: str
    source_image: Optional[str] = None
    clip_path: Optional[str] = None
    status: str = "draft"  # "draft" | "generating" | "completed" | "failed"
    engine_used: Optional[str] = None
    camera_motion: Optional[Dict[str, float]] = None
    camera_description: str = ""
    effect: str = "zoom_in"
    qa_score: Optional[int] = None
    error: Optional[str] = None


@dataclass
class VideoProject:
    project_id: str
    title: str
    script: str
    aspect_ratio: str = "16:9"
    target_duration: float = 15.0
    active_engine: str = "google"
    subject_anchor: str = ""
    shots: List[ShotCard] = field(default_factory=list)
    audio_path: Optional[str] = None
    bgm_path: Optional[str] = None
    master_video_path: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class VideoProjectSession:
    """桌面端工程化会话中枢单例。"""

    _instance: Optional[VideoProjectSession] = None

    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = Path(workspace_root or os.path.expanduser("~/.aria-code/projects/video"))
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        from aria_code.agents.video.director import VideoDirectorAgent
        from aria_code.agents.video.qa_agent import VisualQAAgent
        self.director = VideoDirectorAgent()
        self.qa_agent = VisualQAAgent()

    @classmethod
    def get_instance(cls) -> VideoProjectSession:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _project_dir(self, project_id: str) -> Path:
        return self.workspace_root / project_id

    def _project_file(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "project.json"

    def create_project(
        self,
        title: str,
        script: str,
        images: Optional[List[str]] = None,
        aspect_ratio: str = "16:9",
        target_duration: float = 15.0,
        provider: str = "auto",
    ) -> Dict[str, Any]:
        """初始化一个新视频项目，自动拆解分镜并保存工程文件。"""
        project_id = f"proj_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        pdir = self._project_dir(project_id)
        pdir.mkdir(parents=True, exist_ok=True)

        # 调度导演 Agent 进行多模态分镜规划与角色特征锚定
        storyboard = self.director.decompose_script_and_images(
            script,
            images=images,
            target_duration=target_duration,
            aspect_ratio=aspect_ratio,
        )

        shots = []
        for s in storyboard.get("shots", []):
            shots.append(ShotCard(
                shot_index=s["shot_index"],
                duration=s["duration"],
                prompt=s["prompt"],
                narration=s.get("narration", ""),
                caption=s.get("caption", ""),
                source_image=s.get("source_image"),
                camera_motion=s.get("camera_motion"),
                camera_description=s.get("camera_description", ""),
                effect=s.get("effect", "zoom_in"),
            ))

        project = VideoProject(
            project_id=project_id,
            title=title or storyboard.get("title", "未命名视频"),
            script=script,
            aspect_ratio=aspect_ratio,
            target_duration=target_duration,
            active_engine=provider,
            subject_anchor=storyboard.get("subject_anchor", ""),
            shots=shots,
        )

        self._save_project(project)
        return {"success": True, "project": asdict(project)}

    def get_project(self, project_id: str) -> Dict[str, Any]:
        """获取项目工程详情。"""
        pfile = self._project_file(project_id)
        if not pfile.exists():
            return {"success": False, "error": f"Project '{project_id}' not found."}
        try:
            data = json.loads(pfile.read_text(encoding="utf-8"))
            return {"success": True, "project": data}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def list_projects(self) -> List[Dict[str, Any]]:
        """列出所有本地视频项目。"""
        projects = []
        for pdir in self.workspace_root.iterdir():
            pfile = pdir / "project.json"
            if pfile.is_file():
                try:
                    data = json.loads(pfile.read_text(encoding="utf-8"))
                    projects.append({
                        "project_id": data.get("project_id"),
                        "title": data.get("title"),
                        "aspect_ratio": data.get("aspect_ratio"),
                        "shots_count": len(data.get("shots", [])),
                        "has_master": bool(data.get("master_video_path")),
                        "updated_at": data.get("updated_at"),
                    })
                except Exception:
                    pass
        return sorted(projects, key=lambda x: x.get("updated_at", 0), reverse=True)

    def regenerate_single_shot(
        self,
        project_id: str,
        shot_index: int,
        new_prompt: Optional[str] = None,
        engine_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """核心交互：单镜头独立重新生成，其余镜头复用已有缓存，并触发母带快速重装配。"""
        res = self.get_project(project_id)
        if not res["success"]:
            return res
        pdata = res["project"]

        target_shot = None
        for s in pdata["shots"]:
            if s["shot_index"] == shot_index:
                target_shot = s
                break

        if not target_shot:
            return {"success": False, "error": f"Shot index {shot_index} not in project."}

        target_shot["status"] = "generating"
        self._save_raw(project_id, pdata)

        # 准备生成请求
        effective_prompt = new_prompt or target_shot["prompt"]
        target_shot["prompt"] = effective_prompt

        engine_to_use = engine_name or pdata.get("active_engine", "auto")
        registry = VideoEngineRegistry.get_instance()
        engine = registry.get_engine(None if engine_to_use == "auto" else engine_to_use)

        req = VideoShotRequest(
            prompt=effective_prompt,
            image_path=target_shot.get("source_image"),
            duration_seconds=int(round(target_shot.get("duration", 4.0))),
            aspect_ratio=pdata.get("aspect_ratio", "16:9"),
            camera_motion=target_shot.get("camera_motion"),
        )

        shot_res = engine.generate_shot(req)
        if not shot_res.success or not shot_res.path:
            # 兜底到本地引擎
            if target_shot.get("source_image"):
                local_res = registry.get_engine("local").generate_shot(req)
                if local_res.success and local_res.path:
                    shot_res = local_res
                    shot_res.engine_name = "local_fallback"

        if shot_res.success and shot_res.path:
            target_shot["clip_path"] = shot_res.path
            target_shot["engine_used"] = shot_res.engine_name
            target_shot["status"] = "completed"

            # 触发视觉质检
            qa_res = self.qa_agent.review_shot(shot_index, shot_res.path, effective_prompt)
            target_shot["qa_score"] = qa_res.quality_score

            pdata["updated_at"] = time.time()
            self._save_raw(project_id, pdata)

            # 重新缝合母带
            master_res = self.assemble_master(project_id)

            return {
                "success": True,
                "shot_index": shot_index,
                "clip_path": shot_res.path,
                "qa_score": qa_res.quality_score,
                "engine_used": shot_res.engine_name,
                "master_video_path": master_res.get("master_video_path"),
            }
        else:
            target_shot["status"] = "failed"
            target_shot["error"] = shot_res.error or "Generation failed"
            self._save_raw(project_id, pdata)
            return {"success": False, "error": target_shot["error"]}

    def assemble_master(self, project_id: str) -> Dict[str, Any]:
        """将当前项目已生成的所有镜头拼接、压音并输出母带 MP4。"""
        res = self.get_project(project_id)
        if not res["success"]:
            return res
        pdata = res["project"]

        # 构建 storyboard 格式结构
        storyboard = {
            "title": pdata.get("title", "未命名"),
            "aspect_ratio": pdata.get("aspect_ratio", "16:9"),
            "audio_path": pdata.get("audio_path"),
            "bgm_path": pdata.get("bgm_path"),
            "shots": pdata.get("shots", []),
        }

        # 自动配乐处理
        if not storyboard.get("bgm_path"):
            try:
                from aria_code.video_workflow.music import MusicLibraryService
                msvc = MusicLibraryService.get_instance()
                storyboard["bgm_path"] = msvc.match_or_synthesize_bgm(
                    script=pdata.get("script", ""),
                    duration_seconds=float(pdata.get("target_duration", 15.0)) + 4.0,
                    output_dir=str(self._project_dir(project_id)),
                )
            except Exception as exc:
                logger.warning(f"[VideoProjectSession] Auto BGM synthesis failed: {exc}")

        master_out = self._project_dir(project_id) / "master.mp4"
        assemble_res = assemble_storyboard(storyboard, aspect_ratio=storyboard["aspect_ratio"], output_path=str(master_out))

        if assemble_res.get("success") and assemble_res.get("path"):
            pdata["master_video_path"] = assemble_res["path"]
            pdata["updated_at"] = time.time()
            self._save_raw(project_id, pdata)
            return {
                "success": True,
                "master_video_path": assemble_res["path"],
                "duration": assemble_res.get("duration"),
            }

        return {"success": False, "error": assemble_res.get("error", "Assemble failed")}

    def _save_project(self, project: VideoProject) -> None:
        pfile = self._project_file(project.project_id)
        pfile.write_text(json.dumps(asdict(project), ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_raw(self, project_id: str, data: Dict[str, Any]) -> None:
        pfile = self._project_file(project_id)
        pfile.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
