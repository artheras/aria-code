"""守卫：生产代码里不许出现开发者本机的绝对路径。

背景（2026-08-19）：aria-code 是公开仓库，但当时有 6 个生产文件写死了
`/Users/<dev>/Desktop/...` —— 指向 aria-skills 的脚本、Arthera SDK 的安装
位置、以及本仓库自己的 packages/ 目录。两个后果：

  1. 功能：别人 clone 下来这些路径都不存在，工具静默降级，错误信息里还
     打印一条跟对方毫无关系的路径。
  2. 信息：公开仓库泄露开发者用户名，以及一个私有仓库(Arthera)的存在与
     目录布局。

tests/test_data_service_imports.py 早就为 data_service.py 单独锁过同一条
规则；这里把它推广到全部已跟踪的 Python 生产代码，因为逐个文件加断言
挡不住下一个新文件重犯。

正确写法参见 packages/aria_tools/financial/_paths.py：
  - 本仓库内的路径 → 相对 __file__ 解析
  - aria-skills 的脚本 → 复用 packages/aria_skills/loader.default_skill_roots()
    的发现顺序（ARIA_SKILLS_PATH 环境变量 → 同级 checkout → …）
  - 私有 Arthera 集成 → 只认显式 ARTHERA_ROOT / ARTHERA_PATH；
    未使用的 SDK 路径辅助函数和隐式 checkout 兜底已移除
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 形如 /Users/<name>/ 或 /home/<name>/ 的绝对家目录路径。
# /Users/ 后必须再跟一段路径，避免误伤 "macOS 的 /Users 目录" 这类散文描述。
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+")

# tests/ 里的这个字符串是合法的：那些用例本身就在断言路径已被脱敏
# （assertNotIn("/Users/mac/Desktop", text)），或用它当固定装置。
# 本文件自己的 docstring 同理。
_EXEMPT_PREFIXES = ("tests/",)


def _tracked_python_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [
        line for line in out.splitlines()
        if line and not line.startswith(_EXEMPT_PREFIXES)
    ]


def test_no_hardcoded_developer_home_paths_in_tracked_python_sources():
    offenders: list[str] = []
    for rel in _tracked_python_files():
        path = REPO_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = _HOME_PATH_RE.search(line)
            if match:
                offenders.append(f"{rel}:{lineno}: {match.group(0)}")

    assert not offenders, (
        "生产代码里出现了开发者本机绝对路径（公开仓库，别人 clone 后必然失效）：\n  "
        + "\n  ".join(offenders)
        + "\n\n改用相对 __file__ 的解析或环境变量；范例见 "
          "packages/aria_tools/financial/_paths.py"
    )
