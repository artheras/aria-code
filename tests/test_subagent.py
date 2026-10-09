"""Tests for runtime.subagent background task system."""

import pytest
from aria_code.runtime.subagent import (
    _TASKS,
    SubagentTask,
    tool_spawn_task,
    tool_task_status,
    tool_task_result,
    tool_task_list,
    tool_task_cancel,
    register_runner,
    restore_tasks,
)
from aria_code.runtime.task_ledger import TaskLedger


@pytest.fixture(autouse=True)
def clear_tasks(tmp_path):
    """Clear the task registry and runner between tests."""
    # Same module object the names at the top of this file come from:
    # `runtime.subagent` (via src/aria_code) and `aria_code.runtime.subagent`
    # (via src) are distinct modules with separate _RUNNER/_LEDGER globals, so
    # this fixture was isolating a module nobody called and the tests ran
    # against the real on-disk task ledger.
    import aria_code.runtime.subagent as _sa
    _TASKS.clear()
    _orig_runner = _sa._RUNNER
    _orig_ledger = _sa._LEDGER
    _sa._RUNNER = None  # ensure no runner is registered during tests
    _sa._LEDGER = TaskLedger(tmp_path / "tasks.json")
    yield
    _TASKS.clear()
    _sa._RUNNER = _orig_runner
    _sa._LEDGER = _orig_ledger


class TestSpawnTask:
    def test_spawn_returns_task_id(self):
        result = tool_spawn_task({"prompt": "analyze AAPL"})
        assert result["success"] is True
        assert "task_id" in result
        assert len(result["task_id"]) == 8

    def test_missing_prompt_returns_error(self):
        result = tool_spawn_task({})
        assert result["success"] is False
        assert "prompt" in result["error"].lower()

    def test_task_stored_in_registry(self):
        result = tool_spawn_task({"prompt": "hello"})
        tid = result["task_id"]
        assert tid in _TASKS

    def test_status_is_pending_without_runner(self):
        result = tool_spawn_task({"prompt": "test"})
        assert result["status"] == "pending"

    def test_default_task_is_read_only_and_shared(self):
        result = tool_spawn_task({"prompt": "inspect code"})
        task = _TASKS[result["task_id"]]
        assert task.mode == "read-only"
        assert task.isolation == "shared"

    def test_write_task_defaults_to_worktree_isolation(self):
        result = tool_spawn_task({"prompt": "edit code", "mode": "workspace-write"})
        task = _TASKS[result["task_id"]]
        assert task.mode == "workspace-write"
        assert task.isolation == "worktree"

    def test_invalid_execution_mode_is_rejected(self):
        result = tool_spawn_task({"prompt": "test", "mode": "full-access"})
        assert result["success"] is False

    def test_write_task_cannot_use_shared_workspace(self):
        result = tool_spawn_task({
            "prompt": "edit code",
            "mode": "workspace-write",
            "isolation": "shared",
        })
        assert result["success"] is False
        assert "require worktree" in result["error"]

    async def test_registered_runner_receives_task_contract(self):
        captured = {}

        async def runner(prompt, task):
            captured["prompt"] = prompt
            captured["task"] = task
            return "inspection complete"

        register_runner(runner)
        result = tool_spawn_task({"prompt": "inspect", "_workspace": "/tmp"})
        task = _TASKS[result["task_id"]]
        await task.async_task

        assert task.status == "done"
        assert task.result == "inspection complete"
        assert captured["task"] is task
        assert "Subagent execution contract" in captured["prompt"]
        assert "Workspace: /tmp" in captured["prompt"]


class TestTaskStatus:
    def test_status_of_existing_task(self):
        spawn = tool_spawn_task({"prompt": "test"})
        result = tool_task_status({"task_id": spawn["task_id"]})
        assert result["success"] is True
        assert result["status"] == "pending"

    def test_status_of_nonexistent_task(self):
        result = tool_task_status({"task_id": "deadbeef"})
        assert result["success"] is False

    def test_missing_task_id(self):
        result = tool_task_status({})
        assert result["success"] is False


class TestTaskResult:
    def test_result_of_pending_task_fails(self):
        spawn = tool_spawn_task({"prompt": "test"})
        result = tool_task_result({"task_id": spawn["task_id"]})
        assert result["success"] is False

    def test_result_of_completed_task(self):
        tid = "abc00001"
        task = SubagentTask(task_id=tid, prompt="test", status="done", result="done output")
        _TASKS[tid] = task
        result = tool_task_result({"task_id": tid})
        assert result["success"] is True
        assert result["result"] == "done output"

    def test_result_of_failed_task(self):
        tid = "abc00002"
        task = SubagentTask(task_id=tid, prompt="test", status="failed", error="boom")
        _TASKS[tid] = task
        result = tool_task_result({"task_id": tid})
        assert result["success"] is False
        assert "boom" in result["error"]


class TestTaskList:
    def test_empty_list(self):
        result = tool_task_list({})
        assert result["success"] is True
        assert result["tasks"] == []

    def test_list_with_tasks(self):
        tool_spawn_task({"prompt": "task1"})
        tool_spawn_task({"prompt": "task2"})
        result = tool_task_list({})
        assert result["total"] == 2
        assert len(result["tasks"]) == 2


class TestTaskCancel:
    def test_cancel_pending_task(self):
        spawn = tool_spawn_task({"prompt": "test"})
        result = tool_task_cancel({"task_id": spawn["task_id"]})
        assert result["success"] is True
        assert result["cancelled"] is True
        assert _TASKS[spawn["task_id"]].status == "cancelled"

    def test_cancel_already_done_task(self):
        tid = "done0001"
        task = SubagentTask(task_id=tid, prompt="test", status="done", result="ok")
        _TASKS[tid] = task
        result = tool_task_cancel({"task_id": tid})
        assert result["success"] is False

    def test_cancel_nonexistent(self):
        result = tool_task_cancel({"task_id": "deadbeef"})
        assert result["success"] is False


class TestTaskPersistence:
    def test_ledger_round_trip_and_running_recovery(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ARIA_TASK_LEDGER_PATH", str(tmp_path / "tasks.json"))
        import aria_code.runtime.subagent as subagent
        subagent._LEDGER = TaskLedger(tmp_path / "tasks.json")
        task = SubagentTask(task_id="recover1", prompt="inspect", status="running")
        subagent._TASKS[task.task_id] = task
        subagent._persist(task)
        subagent._TASKS.clear()

        assert restore_tasks() == 1
        restored = subagent._TASKS["recover1"]
        assert restored.status == "interrupted"
        assert "restarted" in restored.error

    def test_completed_handoff_is_exposed(self):
        task = SubagentTask(
            task_id="handoff1", prompt="inspect", status="done", result="ok",
            handoff={"verification": "pytest -q passed"},
        )
        _TASKS[task.task_id] = task
        result = tool_task_result({"task_id": task.task_id})
        assert result["handoff"]["verification"] == "pytest -q passed"
