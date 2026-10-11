import asyncio

import pytest

from aria_code.apps.cli.conversation_policy import is_social_message
from aria_code.apps.cli.prompts.select import build_turn_system_prompt
from aria_code.apps.cli.tool_scope import select_tool_schemas
from aria_code.runtime.agent_loop import run_parallel_tools
from aria_code.runtime.tool_executor import ToolExecutor


@pytest.mark.parametrize("text", ["你好", "您好！", "你好 Aria", "Hello, Aria!", "THANK YOU", "再见。"])
def test_standalone_social_messages_have_no_tools_or_project_context(text):
    assert is_social_message(text)
    schemas = [{"name": "list_files"}, {"name": "read_file"}]
    assert select_tool_schemas(schemas, text) == []
    prompt = build_turn_system_prompt(text, project_context="PRIVATE_WORKSPACE_CONTENT")
    assert "PRIVATE_WORKSPACE_CONTENT" not in prompt
    assert "repo_map" not in prompt


@pytest.mark.parametrize("text", ["你好，读取 README.md", "Hello, fix my tests", "yes", "continue", "谢谢，继续修复", "hi.txt", "运行 ls"])
def test_greetings_with_a_task_and_task_continuations_keep_their_tools(text):
    assert not is_social_message(text)
    assert select_tool_schemas([{"name": "read_file"}], text)


def test_unsolicited_model_call_cannot_scan_files_during_a_greeting(monkeypatch, tmp_path):
    from aria_code.apps.cli.providers import runtime_bridge as rb
    scanned = []
    settings = []
    async def provider(prompt, history, **kwargs):
        if not history:
            return {"success": True, "response": "", "tool_calls_pending": [
                {"tool": "list_files", "params": {"path": str(tmp_path)}}]}
        return {"success": True, "response": "你好！"}
    def factory(**kwargs):
        settings.append(kwargs)
        return provider
    monkeypatch.setattr(rb, "make_provider_fn", factory)
    config = {"acceptance_gate": True, "_session_workspace_root": str(tmp_path)}
    result = asyncio.run(rb.run_chat_via_runtime(
        prompt="你好", history=[], local_tools={"list_files": (lambda p: scanned.append(p) or {"success": True, "data": {"items": []}}, "List")},
        tool_schemas=[{"name": "list_files"}], model="fixture", config=config,
        api_url=None, ollama_url="", project_context="PRIVATE_WORKSPACE_CONTENT", max_rounds=3,
    ))
    assert result == "你好！"
    assert scanned == []
    assert settings[0]["tool_schemas"] == []
    assert settings[0]["project_context"] is None
    assert "_conversation_only" not in config


def test_social_policy_also_blocks_remote_execution():
    calls = []
    async def remote(*args):
        calls.append(args)
        return {"success": True}
    executor = ToolExecutor({}, remote_executor=remote, config={"_conversation_only": True})
    assert not asyncio.run(executor.execute("external", {}))["success"]
    assert not executor.execute_local("external", {})["success"]
    assert calls == []


def test_duplicate_parallel_reads_execute_once_but_new_round_reads_fresh(tmp_path):
    file = tmp_path / "note.txt"
    file.write_text("before")
    calls = []
    def read(params):
        calls.append(params["path"])
        return {"success": True, "data": {"content": file.read_text()}}
    executor = ToolExecutor({"read_file": (read, "Read")})
    pending = [{"tool": "read_file", "params": {"path": str(file)}} for _ in range(3)]
    results = asyncio.run(run_parallel_tools(pending, executor))
    assert len(calls) == 1 and len(results) == 3
    assert results[1]["reused"] and results[2]["reused"]
    results[1]["data"]["content"] = "modified result"
    assert results[0]["data"]["content"] == "before"
    file.write_text("after")
    assert asyncio.run(run_parallel_tools(pending, executor))[0]["data"]["content"] == "after"
    assert len(calls) == 2


def test_different_read_targets_and_side_effects_are_not_coalesced():
    calls = []
    def handler(params):
        from pathlib import Path
        calls.append(Path(params["path"]).name)
        return {"success": True}
    executor = ToolExecutor({"read_file": (handler, "Read"), "custom": (handler, "Action")})
    pending = [{"tool": tool, "params": {"path": path}} for tool, path in
               [("read_file", "one"), ("read_file", "two"), ("custom", "same"), ("custom", "same")]]
    asyncio.run(run_parallel_tools(pending, executor))
    assert sorted(calls) == ["one", "same", "same", "two"]
