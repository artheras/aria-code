"""State model and responsive layout policy for the startup dashboard."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal, Optional


DashboardLayout = Literal["wide", "stacked", "minimal"]


def select_dashboard_layout(width: int, height: Optional[int] = None) -> DashboardLayout:
    """Choose a stable layout for the available terminal width."""
    # The reference mascot needs room beside the model label and runtime.
    if width >= 100:
        return "wide"
    if height is not None and height <= 24:
        return "minimal"
    if width >= 64:
        return "stacked"
    return "minimal"


@dataclass(frozen=True)
class StartupDashboardViewModel:
    """Display-ready startup state, independent from Rich and the CLI runtime."""

    version: str
    runtime_label: str
    cwd: str
    control_status: str
    health_status: str
    tool_count: int
    skill_count: int
    lang: str = "en"
    first_run: bool = False
    update_notice: Optional[str] = None
    auto_healed_from: str = ""
    current_id: str = ""
    badge: str = ""
    best_lite_id: str = ""
    best_lite_installed: bool = True
    git_branch: str = ""
    git_dirty: bool = False
    mcp_server_count: int = 0

    @property
    def is_zh(self) -> bool:
        return self.lang.lower().startswith("zh")

    @property
    def capabilities(self) -> str:
        parts = []
        if self.mcp_server_count:
            parts.append(
                f"MCP {self.mcp_server_count}"
                if not self.is_zh
                else f"MCP {self.mcp_server_count}"
            )
        if self.is_zh:
            parts.extend((f"{self.tool_count} 个工具", f"{self.skill_count} 个技能"))
        else:
            tool_word = "tool" if self.tool_count == 1 else "tools"
            skill_word = "skill" if self.skill_count == 1 else "skills"
            parts.extend((f"{self.tool_count} {tool_word}", f"{self.skill_count} {skill_word}"))
        return " · ".join(parts)

    @property
    def workspace_state(self) -> str:
        if not self.git_branch:
            return ""
        state = "有改动" if self.is_zh else "dirty"
        clean = "干净" if self.is_zh else "clean"
        return f"{self.git_branch} · {state if self.git_dirty else clean}"

    @property
    def compact_health(self) -> str:
        plain = re.sub(r"\[/?[^]]+\]", "", self.health_status)
        match = re.search(r"Ollama\s+(?:online|在线)\s*·\s*(\d+)", plain, re.IGNORECASE)
        if match:
            prefix = "本地" if self.is_zh else "Local"
            return f"{prefix}: Ollama {match.group(1)}"
        if "offline" in plain.lower() or "离线" in plain:
            return "本地: Ollama 离线" if self.is_zh else "Local: Ollama offline"
        return plain

    @property
    def getting_started_title(self) -> str:
        return "快速开始" if self.is_zh else "Quick start"

    @property
    def runtime_title(self) -> str:
        return "运行状态" if self.is_zh else "Runtime"

    @property
    def whats_new_title(self) -> str:
        return "版本更新" if self.is_zh else "What's new"

    @property
    def getting_started_lines(self) -> tuple[str, str]:
        if self.is_zh:
            return (
                "直接描述任务即可",
                "/plan · /help · @ 上下文",
            )
        return (
            "Describe the task naturally",
            "/plan · /help · @ context",
        )
