"""定位 aria-skills 脚本与 Arthera SDK —— 替掉四个工具文件里写死的绝对路径。

2026-08-19：本目录下 factors / risk_tools / strategy / compliance 四个文件
各自硬编码了开发者本机的绝对路径 —— 指向 aria-skills 的脚本，以及 Arthera
SDK 的安装位置。aria-code 是公开仓库，这带来两个问题：

  1. 功能上，任何别人 clone 下来这些路径都不存在，工具静默降级成
     "Error: 脚本不存在"，而错误信息里还打印出一个跟对方毫无关系的路径。
  2. 信息上，公开仓库里出现开发者本机用户名，以及一个私有仓库
     (Arthera) 的存在与目录结构。

skill 那边其实早就有一套正规的发现顺序 —— packages/aria_skills/loader.py
的 default_skill_roots()（ARIA_SKILLS_PATH 环境变量 → 同级 aria-skills
checkout → ~/.arthera/skills → ~/.claude/skills → …）。这里直接复用它，
而不是再写第二套查找逻辑，免得两处顺序不一致时出现"CLI 能发现这个 skill、
工具却找不到它的脚本"。

Arthera SDK 是私有仓库的东西，公开仓库无从假设它在哪，因此只认显式的
ARTHERA_SDK_PATH，不隐式发现旁边的源码目录；找不到就安静跳过 —— 这些
工具本来就有 mcp_tool 的 ImportError 兜底分支，SDK 缺失不该是致命错误。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from aria_code.packages.aria_core.paths import aria_home

__all__ = ["find_skill_script", "missing_script_message", "ensure_arthera_sdk", "ensure_arthera_root"]


def _skill_roots() -> list[Path]:
    """优先复用 skill loader 的发现顺序；它不可用时退化为等价的最小实现。"""
    try:
        from packages.aria_skills.loader import default_skill_roots

        return [Path(p) for p in default_skill_roots()]
    except Exception:
        roots: list[Path] = []
        configured = os.getenv("ARIA_SKILLS_PATH", "")
        roots.extend(
            Path(item).expanduser() for item in configured.split(os.pathsep) if item.strip()
        )
        repository = Path(__file__).resolve().parents[3]
        roots.extend([
            repository.parent / "aria-skills" / "skills",
            aria_home() / "skills",
            Path.home() / ".aria" / "skills",
            Path.home() / ".claude" / "skills",
        ])
        return roots


def find_skill_script(skill_name: str, script_name: str) -> Path | None:
    """返回 <某个 skill 根>/<skill_name>/scripts/<script_name>，找不到返回 None。

    调用方负责把 None 变成对用户有意义的提示——这里不抛异常，因为"本机没装
    这个 skill 目录"是完全正常的部署形态，不是错误。
    """
    for root in _skill_roots():
        candidate = root / skill_name / "scripts" / script_name
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def missing_script_message(skill_name: str, script_name: str) -> str:
    """脚本缺失时给出可操作的提示，而不是打印一条别人机器上不存在的路径。"""
    return (
        f"Error: 找不到 {skill_name}/scripts/{script_name}。"
        " 请获取 aria-skills 目录，并用 ARIA_SKILLS_PATH 指向其中的 skills/，"
        "或把它放在 aria-code 的同级目录下。"
    )


def ensure_arthera_sdk() -> bool:
    """把 Arthera SDK 加进 sys.path。返回是否找到。

    只认显式配置：Arthera 是私有仓库，公开仓库不该假设它
    存在于某个固定绝对路径。
    """
    candidates: list[Path] = []
    configured = os.getenv("ARTHERA_SDK_PATH", "")
    if configured.strip():
        candidates.append(Path(configured).expanduser())
    arthera_root = os.getenv("ARTHERA_PATH", "")
    if arthera_root.strip():
        candidates.append(Path(arthera_root).expanduser() / "sdks" / "python")

    for candidate in candidates:
        try:
            if candidate.is_dir():
                path_str = str(candidate)
                if path_str not in sys.path:
                    sys.path.append(path_str)
                return True
        except OSError:
            continue
    return False


def ensure_arthera_root() -> bool:
    """把 Arthera 仓库根目录加进 sys.path，让 ``packages.*`` 可导入。返回是否找到。

    跟 ``ensure_arthera_sdk`` 同样的理由：Arthera 是私有仓库，公开仓库不该假设
    它在某个写死的绝对路径上。这里认的是 ``ARTHERA_ROOT``（doctor.py 与
    backtest_cmds.py 已经在用的同一个变量），不隐式发现旁边的源码目录。
    """
    candidates: list[Path] = []
    for var in ("ARTHERA_ROOT", "ARTHERA_PATH"):
        configured = os.getenv(var, "")
        if configured.strip():
            candidates.append(Path(configured).expanduser())

    for candidate in candidates:
        try:
            if candidate.is_dir():
                path_str = str(candidate)
                if path_str not in sys.path:
                    sys.path.append(path_str)
                return True
        except OSError:
            continue
    return False
