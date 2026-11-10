"""General roles must change actual execution, not just displayed labels."""
import asyncio
from types import SimpleNamespace

import pytest

from aria_code.runtime import subagent as sa
from aria_code.runtime.agent_profiles import get_profile, tool_list_agents
from aria_code.runtime.task_ledger import TaskLedger
from aria_code.runtime.tool_executor import ToolExecutor
from aria_code.apps.cli.providers import runtime_bridge as bridge


@pytest.fixture(autouse=True)
def isolated_scheduler(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "_TASKS", {})
    monkeypatch.setattr(sa, "_RUNNER", None)
    monkeypatch.setattr(sa, "_LEDGER", TaskLedger(tmp_path / "tasks.json"))


def test_catalog_is_domain_neutral_and_discoverable():
    catalog = tool_list_agents({})
    assert {p["name"] for p in catalog["agents"]} == {
        "general", "explorer", "planner", "researcher", "implementer", "reviewer", "data_analyst", "writer"}
    assert "list_agents" in sa.SUBAGENT_TOOLS
    assert get_profile("reviewer").writable is False
    with pytest.raises(ValueError, match="Unknown"):
        get_profile("invented")


@pytest.mark.asyncio
async def test_real_async_executor_starts_task_on_live_loop(tmp_path):
    async def runner(prompt, task):
        assert task.workspace == str(tmp_path)
        assert "Role: researcher" in prompt
        assert "Acceptance criteria: Cite the source" in prompt
        return "FINDINGS: source reviewed; CHANGED_FILES: none; VERIFICATION: static read; BLOCKERS: none"

    sa.register_runner(runner)
    executor = ToolExecutor(sa.SUBAGENT_TOOLS, config={"workspace_root": str(tmp_path)},
                            execution_context=lambda: {"_session_id": "parent"})
    result = await executor.execute("spawn_task", {
        "prompt": "Research product requirements", "agent": "researcher", "success_criteria": "Cite the source"})
    task = sa._TASKS[result["task_id"]]
    await task.async_task
    assert task.status == "done"
    assert task.handoff["verification"] == "static read"
    stored = sa._ledger().load()[task.task_id]
    assert stored["agent"] == "researcher"
    sa._TASKS.clear()
    sa.restore_tasks()
    assert sa._TASKS[task.task_id].success_criteria == "Cite the source"


@pytest.mark.asyncio
async def test_role_tools_are_enforced_even_for_unoffered_calls(tmp_path, monkeypatch):
    captured = {}

    async def fake_turn(**kwargs):
        captured.update(kwargs)
        executor = ToolExecutor(kwargs["local_tools"], config=kwargs["config"])
        assert not executor.execute_local("write_file", {})["success"]
        assert not (await executor.execute("remote_publish", {}))["success"]
        assert executor.execute_local("read_file", {})["success"]
        return SimpleNamespace(text="review complete", error=None)

    monkeypatch.setattr(bridge, "run_chat_via_runtime", fake_turn)
    task = sa.SubagentTask("review", "review sources", agent="reviewer", workspace=str(tmp_path))
    tools = {name: (lambda p: {"success": True}, "") for name in ["read_file", "write_file", "spawn_task"]}
    schemas = [{"name": name} for name in tools]
    assert await bridge.run_subagent_turn("review", task, local_tools=tools, tool_schemas=schemas,
                                         config={}, api_url=None) == "review complete"
    assert set(captured["local_tools"]) == {"read_file"}
    assert captured["tool_schemas"] == [{"name": "read_file"}]
    assert captured["max_rounds"] == 20
    assert captured["history"] == []


def test_host_read_only_cannot_be_spoofed():
    sa.register_runner(lambda p: None)
    executor = ToolExecutor(sa.SUBAGENT_TOOLS, config={"permission_mode": "read-only"})
    result = executor.execute_local("spawn_task", {
        "prompt": "write a document", "agent": "writer", "mode": "workspace-write",
        "_parent_permission_mode": "full-access"})
    assert not result["success"]
    assert "parent" in result["error"]
    assert not sa._TASKS


@pytest.mark.parametrize("params", [
    {"agent": "invented"}, {"agent": "reviewer", "mode": "workspace-write"},
    {"agent": "researcher", "backend": "claude"}, {"max_rounds": 31},
    {"timeout_seconds": 301}, {"timeout_seconds": True},
])
def test_invalid_contracts_do_not_create_tasks(params):
    sa.register_runner(lambda p: None)
    assert not sa.tool_spawn_task({"prompt": "task", **params})["success"]
    assert not sa._TASKS


@pytest.mark.asyncio
async def test_timeout_is_failed_not_done():
    async def runner(prompt, task):
        await asyncio.sleep(5)
        return "should not complete"

    sa.register_runner(runner)
    result = sa.tool_spawn_task({"prompt": "research", "agent": "researcher", "timeout_seconds": 1})
    task = sa._TASKS[result["task_id"]]
    await task.async_task
    assert task.status == "failed"
    assert "deadline" in task.error
    assert not sa.tool_task_result({"task_id": task.task_id})["success"]


@pytest.mark.asyncio
async def test_partial_text_with_runtime_error_is_failure(monkeypatch):
    async def fake_turn(**kwargs):
        return SimpleNamespace(text="Partial analysis", error="round budget exceeded")

    monkeypatch.setattr(bridge, "run_chat_via_runtime", fake_turn)
    with pytest.raises(RuntimeError, match="budget"):
        await bridge.run_subagent_turn("inspect", sa.SubagentTask("one", "inspect"),
                                       local_tools={}, tool_schemas=[], config={}, api_url=None)


def test_no_runner_and_task_limits_are_explicit():
    assert "runner" in sa.tool_spawn_task({"prompt": "inspect", "agent": "explorer"})["error"]
    for i in range(8):
        assert sa.tool_spawn_task({"prompt": f"legacy queued {i}"})["success"]
    assert "limit" in sa.tool_spawn_task({"prompt": "one too many"})["error"]


def test_bridge_does_not_upgrade_read_only_parent():
    task = sa.SubagentTask("one", "write", mode="workspace-write")
    with pytest.raises(PermissionError, match="parent"):
        bridge.subagent_run_options({"permission_mode": "read-only"}, task)


def test_task_ledger_respects_isolated_home(tmp_path, monkeypatch):
    from aria_code.runtime.task_ledger import default_ledger_path
    monkeypatch.delenv("ARIA_TASK_LEDGER_PATH", raising=False)
    monkeypatch.delenv("ARIA_CONFIG_DIR", raising=False)
    monkeypatch.setenv("ARIA_HOME", str(tmp_path))
    assert default_ledger_path() == tmp_path / "task_ledger.json"


@pytest.mark.parametrize("report", [
    "FINDINGS: source read; CHANGED_FILES: none; VERIFICATION: brief.md; BLOCKERS: none",
    "**Findings:** source read\n**Changed Files:** none\n**Verification:** brief.md\n**Blockers:** none",
    "Findings: source read\nChanged_files: none\nVerification: brief.md\nBlockers: none",
])
def test_handoff_accepts_actual_model_heading_styles(report):
    handoff = sa._extract_handoff(report)
    assert handoff == {"findings": "source read", "changed_files": "none", "verification": "brief.md", "blockers": "none"}
