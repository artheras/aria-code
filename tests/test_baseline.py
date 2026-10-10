"""A check that was already red before the change is not this task's to repair."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from aria_code.runtime.acceptance import AcceptanceGate
from aria_code.runtime.baseline import Baseline, compare, failing_tests
from aria_code.runtime.checkpoints import CheckpointStore
from aria_code.runtime.delivery import report_from_activities
from aria_code.runtime.transactions import FAILED, PREEXISTING, verdict_from_acceptance

PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider"


def test_pytest_failures_are_compared_by_test_id():
    after = "FAILED test_a.py::test_old - assert 1\nFAILED test_a.py::test_new - assert 2\n"
    before = "FAILED test_a.py::test_old - assert 1\n"
    assert failing_tests(after) == {"test_a.py::test_old", "test_a.py::test_new"}
    result = compare(after, True, (1, before))
    assert not result.preexisting
    assert result.new_failures == ("test_a.py::test_new",)
    assert result.old_failures == ("test_a.py::test_old",)

    assert compare(before, True, (1, before)).preexisting
    assert not compare(after, True, (0, "")).preexisting           # passed before: ours
    assert compare("make: *** error", True, (2, "make: *** error")).preexisting
    assert not compare("x", True, None).preexisting                 # no baseline: assume ours


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo,
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "calc"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a + b  # bug\n")
    (repo / "test_calc.py").write_text(
        "from calc import add, sub\n\n"
        "def test_add():\n    assert add(2, 3) == 5\n\n"
        "def test_sub():\n    assert sub(5, 3) == 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def run_in(command, cwd):
    done = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True)
    return {"success": True, "data": {"exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}}


def edit(path: Path, content: str):
    before = path.read_text()
    path.write_text(content)
    CheckpointStore().record_change(path=path, before_content=before, after_content=content,
                                    existed_before=True, source="test", session_id="s")


def gate_for(repo, baseline):
    return AcceptanceGate(runner=lambda command: run_in(command, repo), root=repo,
                          commands=[PYTEST], baseline=baseline)


def test_a_new_failure_is_told_apart_from_an_old_one(repo, tmp_path):
    baseline = Baseline(repo, run=run_in, undo_since=CheckpointStore().max_sequence(),
                        session_id="s", scratch_root=tmp_path / "scratch")
    edit(repo / "calc.py", "def add(a, b):\n    return a * b\n\ndef sub(a, b):\n    return a + b  # bug\n")
    gate = gate_for(repo, baseline)
    gate.record_tool("write_file", {"success": True, "path": str(repo / "calc.py")})

    report = asyncio.run(gate.run())

    check = report.failures[0]
    assert report.regressed
    assert check.new_failures == ("test_calc.py::test_add",)
    assert check.old_failures == ("test_calc.py::test_sub",)
    directive = report.repair_directive()
    assert "test_calc.py::test_add" in directive and "不要修改" in directive
    assert not list((tmp_path / "scratch").glob("*"))        # scratch worktree removed
    assert (repo / "calc.py").read_text().startswith("def add(a, b):\n    return a * b")


def test_only_old_failures_are_not_a_regression(repo, tmp_path):
    baseline = Baseline(repo, run=run_in, undo_since=CheckpointStore().max_sequence(),
                        session_id="s", scratch_root=tmp_path / "scratch")
    edit(repo / "calc.py", "def add(a, b):\n    return a + b  # tidy\n\ndef sub(a, b):\n    return a + b  # bug\n")
    gate = gate_for(repo, baseline)
    gate.record_tool("write_file", {"success": True, "path": str(repo / "calc.py")})

    report = asyncio.run(gate.run())
    summary = gate.summary()

    assert not report.passed and not report.regressed
    assert "改动前就已存在" in report.headline()
    assert summary["verified"] is False and summary["regressions"] is False
    assert verdict_from_acceptance(summary, "") == PREEXISTING
    delivery = report_from_activities([], acceptance=summary)
    assert delivery.status == "done"
    assert "already failing before this change" in delivery.render()


def test_a_task_worktree_is_compared_with_its_starting_snapshot(repo, tmp_path):
    from aria_code.runtime.task_worktree import TaskWorktrees

    tasks = TaskWorktrees(tmp_path / "worktrees")
    task = tasks.ensure(repo)
    work = Path(task.path)
    (work / "calc.py").write_text("def add(a, b):\n    return 0\n\ndef sub(a, b):\n    return a - b\n")
    baseline = Baseline(repo, run=run_in, base_commit=task.base, scratch_root=tmp_path / "scratch")
    gate = AcceptanceGate(runner=lambda command: run_in(command, work), root=work,
                          commands=[PYTEST], baseline=baseline)
    gate.record_tool("write_file", {"success": True, "path": str(work / "calc.py")})

    report = asyncio.run(gate.run())

    assert report.failures[0].new_failures == ("test_calc.py::test_add",)
    assert verdict_from_acceptance(gate.summary(), "") == FAILED


@pytest.fixture
def plain(tmp_path):
    """The eval's fixture: no git, one test red before the task and unrelated to it."""
    work = tmp_path / "plain"
    work.mkdir()
    (work / "legacy.py").write_text("def legacy_rate():\n    return 0.15\n")
    (work / "text.py").write_text("def shout(text):\n    return text.upper()\n")
    (work / "test_legacy.py").write_text(
        "from legacy import legacy_rate\n\ndef test_legacy_rate():\n    assert legacy_rate() == 0.2\n")
    (work / "test_text.py").write_text(
        "from text import slugify\n\ndef test_slugify():\n    assert slugify('A b') == 'a-b'\n")
    return work


def test_outside_git_the_workspace_is_copied_for_the_baseline(plain, tmp_path):
    baseline = Baseline(plain, run=run_in, undo_since=CheckpointStore().max_sequence(),
                        session_id="s", scratch_root=tmp_path / "scratch")
    edit(plain / "text.py", "def shout(text):\n    return text.upper()\n\n"
                            "def slugify(text):\n    return '-'.join(text.lower().split())\n")
    gate = gate_for(plain, baseline)
    gate.record_tool("write_file", {"success": True, "path": str(plain / "text.py")})

    report = asyncio.run(gate.run())

    assert not report.passed and not report.regressed     # only test_legacy, red before too
    assert report.failures[0].old_failures == ("test_legacy.py::test_legacy_rate",)
    assert list((tmp_path / "scratch").iterdir()) == []   # the copy is removed


def test_outside_git_a_new_failure_still_counts(plain, tmp_path):
    baseline = Baseline(plain, run=run_in, undo_since=CheckpointStore().max_sequence(),
                        session_id="s", scratch_root=tmp_path / "scratch")
    edit(plain / "text.py", "def slugify(text):\n    return text\n")
    gate = gate_for(plain, baseline)
    gate.record_tool("write_file", {"success": True, "path": str(plain / "text.py")})

    report = asyncio.run(gate.run())

    assert report.regressed
    assert "test_text.py::test_slugify" in report.failures[0].new_failures


def test_a_large_workspace_outside_git_gets_no_baseline(plain, tmp_path, monkeypatch):
    from aria_code.runtime import baseline as module

    monkeypatch.setattr(module, "_COPY_MAX_FILES", 2)
    baseline = Baseline(plain, run=run_in, scratch_root=tmp_path / "scratch")
    assert asyncio.run(baseline.result(PYTEST)) is None
    assert not (tmp_path / "scratch").exists() or list((tmp_path / "scratch").iterdir()) == []


def test_a_pytest_baseline_runs_past_collection_errors():
    from aria_code.runtime.baseline import _keep_collecting

    assert _keep_collecting("python -m pytest -q").endswith("--continue-on-collection-errors")
    assert _keep_collecting("/venv/bin/pytest tests/x.py").endswith("--continue-on-collection-errors")
    for unchanged in ("npm test", "pytest -q | tail -5", "make test",
                      "pytest --continue-on-collection-errors"):
        assert _keep_collecting(unchanged) == unchanged
