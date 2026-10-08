"""Exercise the release command and the workflow's staging of README badges."""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release_tree(tmp_path):
    for relative in (
        "scripts/bump_version.py", "pyproject.toml", "npm/package.json",
        "src/aria_code/_version.py", "README.md", "README_CN.md",
        ".github/workflows/release-on-merge.yml",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    return tmp_path


def run_bump(tree, *args):
    return subprocess.run(
        [sys.executable, str(tree / "scripts/bump_version.py"), *args],
        cwd=tree, capture_output=True, text=True, check=False,
    )


def test_next_release_updates_and_stages_both_pypi_badges(release_tree):
    tree = release_tree
    for command in (
        ["git", "init", "-q"], ["git", "add", "."],
        ["git", "-c", "user.name=Release test", "-c",
         "user.email=release-test@invalid.example", "commit", "-qm", "baseline"],
    ):
        subprocess.run(command, cwd=tree, check=True, capture_output=True)
    version = run_bump(tree, "--next-release").stdout.strip()
    bumped = run_bump(tree, version)
    assert bumped.returncode == 0, bumped.stderr
    checked = run_bump(tree, "--check", version)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    for relative in ("README.md", "README_CN.md"):
        text = (tree / relative).read_text(encoding="utf-8")
        assert f"https://img.shields.io/pypi/v/aria-code/{version}?" in text
        assert f"https://pypi.org/project/aria-code/{version}/" in text

    # Run the actual workflow's staging command: updating files is insufficient
    # if the release commit does not contain the new badge and click target.
    workflow = (tree / ".github/workflows/release-on-merge.yml").read_text()
    command = re.search(r"(?m)^\s+(git add .+)$", workflow).group(1)
    subprocess.run(shlex.split(command), cwd=tree, check=True, capture_output=True)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=tree,
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    assert {"README.md", "README_CN.md"}.issubset(staged)


@pytest.mark.parametrize("readme", ["README.md", "README_CN.md"])
@pytest.mark.parametrize("endpoint", [
    "https://img.shields.io/pypi/v/aria-code/",
    "https://pypi.org/project/aria-code/",
])
def test_release_check_refuses_stale_badge_or_click_target(release_tree, readme, endpoint):
    checked = run_bump(release_tree, "--check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    path = release_tree / readme
    text = path.read_text(encoding="utf-8")
    text = re.sub(re.escape(endpoint) + r'[^/?"\s]+', endpoint + "4.4.2", text, count=1)
    path.write_text(text, encoding="utf-8")
    checked = run_bump(release_tree, "--check")
    assert checked.returncode == 1
    assert readme in checked.stdout
    assert "4.4.2" in checked.stdout
