"""A REPL turn edits a worktree of the repository and asks before applying."""

import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo,
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo = tmp_path / "calc"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    monkeypatch.chdir(repo)
    monkeypatch.delenv("ARIA_TASK_ISOLATION", raising=False)
    return repo


@pytest.mark.asyncio
async def test_a_turn_edits_the_worktree_and_the_answer_decides(repo, tmp_path, monkeypatch):
    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli
    import apps.cli.providers.runtime_bridge as bridge
    from rich.console import Console
    from apps.cli import task_isolation
    from aria_code.runtime.task_worktree import TaskWorktrees

    task_isolation.reset_for_tests(TaskWorktrees(tmp_path / "worktrees"))
    seen = {}

    async def fake_turn(**kwargs):
        executor = bridge.build_tool_executor(
            {"write_file": (lambda p: Path(p["path"]).write_text(p["content"]) and {"success": True}, "")},
            kwargs["config"], kwargs["execution_context"])
        seen["write"] = executor.execute_local(
            "write_file", {"path": str(repo / "calc.py"), "content": "def add(a, b):\n    return a + b\n"})
        seen["context"] = kwargs["execution_context"]()
        final = SimpleNamespace(success=True, cancelled=False, acceptance=None, delivery=None,
                                provider="test", stop_reason="completed", error="",
                                metadata=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                                         thinking_tokens=0),
                                tools=["write_file"], sources=[])
        return SimpleNamespace(text="Fixed add.", final=final, error=None, cancelled=False, ok=True)

    asked = []

    def choose(options, selected=0, title="", **_kwargs):
        asked.append(title)
        return 1  # keep working

    monkeypatch.setattr(bridge, "run_chat_via_runtime", fake_turn)
    monkeypatch.setattr(cli, "_arrow_select", choose)
    monkeypatch.setattr(cli, "console", Console(file=io.StringIO(), width=100))
    config = dict(cli.DEFAULT_CONFIG, model="claude-sonnet-4-5", permission_mode="workspace-write")
    terminal = cli.ArtheraTerminal(config)
    terminal.config["_session_workspace_root"] = str(repo)

    await terminal.send_message("fix the add function in calc.py")

    worktree = Path(seen["context"]["_workspace"])
    assert worktree != repo and seen["context"]["_workspace_origin"] == str(repo.resolve())
    assert "a + b" in (worktree / "calc.py").read_text()
    assert "a - b" in (repo / "calc.py").read_text()
    assert asked == ["Apply this task?"]

    assert "Applied task" in task_isolation.command("apply", terminal.config)
    assert "a + b" in (repo / "calc.py").read_text()
    task_isolation.reset_for_tests(None)
