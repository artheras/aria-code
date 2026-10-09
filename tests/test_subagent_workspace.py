"""A background task's tools act in the task's workspace, not the session's.

The aria runner called stream_provider_result(prompt, config=…, local_tools=…),
a signature that function never had, so every aria-backed task failed with a
TypeError before doing anything; had it run, its tools would have acted on the
session's directory, worktree or not.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

import apps.cli.providers.runtime_bridge as bridge


def _task(tmp_path, mode="workspace-write", isolated=True):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    spec = SimpleNamespace(repository=str(tmp_path / "repo")) if isolated else None
    return SimpleNamespace(mode=mode, workspace=str(worktree), worktree_spec=spec, session_id="s1")


def test_an_isolated_task_runs_confined_to_its_worktree(tmp_path):
    task = _task(tmp_path)
    cfg, context = bridge.subagent_run_options({"permission_mode": "full-access"}, task)
    assert cfg["permission_mode"] == "workspace-write"
    assert context["_workspace"] == task.workspace
    assert context["_workspace_origin"] == str(tmp_path / "repo")
    assert context["_workspace_restricted"] is True


def test_a_read_only_task_stays_read_only(tmp_path):
    cfg, context = bridge.subagent_run_options({}, _task(tmp_path, mode="read-only", isolated=False))
    assert cfg["permission_mode"] == "read-only"
    assert "_workspace_restricted" not in context


@pytest.mark.asyncio
async def test_the_tasks_writes_land_in_its_worktree(tmp_path, monkeypatch):
    task = _task(tmp_path)
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    written = []

    async def fake_turn(**kwargs):
        executor = bridge.build_tool_executor(
            {"write_file": (lambda p: written.append(p["path"]) or {"success": True}, "")},
            kwargs["config"], kwargs["execution_context"])
        executor.execute_local("write_file", {"path": "notes.md", "content": "x"})
        executor.execute_local("write_file", {"path": str(tmp_path / "repo" / "app.py"), "content": "x"})
        return SimpleNamespace(text="done", error=None)

    monkeypatch.setattr(bridge, "run_chat_via_runtime", fake_turn)
    assert await bridge.run_subagent_turn("edit", task, local_tools={}, tool_schemas=[],
                                          config={}, api_url=None) == "done"
    assert written == [str(Path(task.workspace).resolve() / "notes.md"),
                       str(Path(task.workspace).resolve() / "app.py")]


@pytest.mark.asyncio
async def test_a_failed_turn_fails_the_task(tmp_path, monkeypatch):
    async def fake_turn(**kwargs):
        return SimpleNamespace(text="", error="quota exceeded")

    monkeypatch.setattr(bridge, "run_chat_via_runtime", fake_turn)
    with pytest.raises(RuntimeError, match="quota"):
        await bridge.run_subagent_turn("edit", _task(tmp_path), local_tools={}, tool_schemas=[],
                                       config={}, api_url=None)
