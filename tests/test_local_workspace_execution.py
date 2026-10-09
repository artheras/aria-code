"""Exercise the local file -> edit -> checks -> repair chain, not fake write metadata."""
import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aria_code.runtime import AcceptanceGate, ToolExecutor
from aria_code.runtime.approval import ApprovalDecision
from aria_code.runtime.gateway import TurnResult, run_turn
from aria_code.workspace import WorkspaceSecurity
from aria_code.apps.cli.workspace_route import workspace_config


def test_reads_and_writes_have_distinct_roots(fake_home):
    security = WorkspaceSecurity(cwd="/work/project", allowed_roots=["/Volumes/Reference"],
                                 write_roots=["/Volumes/Editable"], allow_home=True)
    assert security.is_safe_path("/Volumes/Reference/spec.md")
    assert not security.is_safe_path("/Volumes/Reference/spec.md", write=True)
    assert security.is_safe_path("/Volumes/Editable/app.py", write=True)
    assert not security.is_safe_path(Path.home() / "unrelated-project/app.py", write=True)
    assert security.resolve("src/app.py") == Path("/work/project/src/app.py")


def test_model_cannot_grant_itself_roots_or_skip_read_only():
    called = []
    executor = ToolExecutor({"write_file": (lambda p: called.append(p) or {"success": True}, "")},
                            config={"permission_mode": "read-only"})
    result = executor.execute_local("write_file", {"path": "app.py", "_workspace": "/",
                                                  "_allowed_write_roots": ["/"], "_skip_confirm": True})
    assert not result["success"] and not called


def test_write_outside_project_requires_host_grant(fake_home):
    called = []
    outside = Path.home() / "unrelated-project" / "app.py"
    tools = {"write_file": (lambda p: called.append(p) or {"success": True}, "")}
    blocked = ToolExecutor(tools).execute_local("write_file", {"path": str(outside)})
    assert not blocked["success"] and not called
    allowed = ToolExecutor(tools, config={"write_roots": [str(outside.parent)]})
    assert allowed.execute_local("write_file", {"path": str(outside)})["success"]
    assert called[0]["_allowed_write_roots"] == [str(outside.parent)]


def test_symlink_cannot_escape_write_root(tmp_path, fake_home):
    outside = Path.home() / "unrelated-project" / "app.py"
    (tmp_path / "escape.py").symlink_to(outside)
    tools = {"write_file": (lambda p: pytest.fail("must not execute escaped write"), "")}
    executor = ToolExecutor(tools, config={"workspace_root": str(tmp_path)})
    result = executor.execute_local("write_file", {"path": "escape.py"})
    assert not result["success"]


def test_google_workspace_route_preserves_model_and_saved_config(monkeypatch):
    from aria_code.apps.cli.providers import base
    monkeypatch.setattr(base, "google_readiness", lambda cfg: "")
    config = {"backend_chat": True, "local_provider": "google", "gcp_project": "test"}
    selected = workspace_config("修复我的项目", "google/gemini-2.5-flash", config, "https://cloud")
    assert selected["backend_chat"] is False
    assert config["backend_chat"] is True
    assert workspace_config("explain decorators", "google/gemini-2.5-flash", config, "https://cloud") is config


def test_backend_without_google_credentials_stays_explicitly_unavailable(monkeypatch):
    from aria_code.apps.cli.providers import base
    monkeypatch.setattr(base, "google_readiness", lambda cfg: "no credentials")
    config = {"backend_chat": True, "local_provider": "google"}
    assert workspace_config("这个项目测试挂了", "google/gemini-2.5-flash", config, "https://cloud") is config


def test_chinese_file_request_uses_local_workspace(tmp_path):
    from aria_code.apps.cli.workspace_route import needs_workspace
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    assert needs_workspace("帮我修改calc.py", cwd=tmp_path)


@pytest.mark.asyncio
async def test_cli_selects_project_and_explicit_google_provider(tmp_path, monkeypatch):
    import io
    from aria_code import aria_cli as cli
    project = tmp_path / "app"
    shared = tmp_path / "shared"
    project.mkdir()
    shared.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["aria", "-C", "app", "--add-dir", "shared",
                                      "--model", "google/gemini-2.5-flash", "-p", "hello"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(cli, "load_config", lambda: {"backend_chat": True,
                                                    "check_for_update_on_startup": False})
    seen = []

    class Terminal:
        def __init__(self, config):
            self.config = config

        async def run_prompt(self, prompt, **kwargs):
            seen.append((Path.cwd(), dict(self.config), prompt))

    monkeypatch.setattr(cli, "ArtheraTerminal", Terminal)
    await cli.main()
    cwd, config, prompt = seen[0]
    assert cwd == project
    assert config["_session_workspace_root"] == str(project)
    assert config["_session_write_roots"] == [str(shared)]
    assert config["backend_chat"] is False
    assert config["local_provider"] == "google" and config["model"] == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_exhausted_budget_cannot_complete_successfully():
    from aria_code.runtime import AgentEventComplete, AgentOptions, run_agent
    from aria_code.runtime.budget import BudgetConfig, BudgetTracker
    budget = BudgetTracker(BudgetConfig(max_tokens=1))
    budget.record("google", 1, 0)

    async def provider(*args, **kwargs):
        pytest.fail("budget must stop before another paid request")

    events = [event async for event in run_agent("fix", [], provider_fn=provider,
              tool_executor=ToolExecutor({}), options=AgentOptions(budget=budget))]
    final = next(event.result for event in events if isinstance(event, AgentEventComplete))
    assert final.success is False and final.stop_reason == "budget_exhausted"


@pytest.mark.asyncio
async def test_round_limit_is_incomplete():
    async def provider(*args, **kwargs):
        return {"success": True, "response": "still working", "tool_calls_pending":
                [{"tool": "read_file", "params": {"path": "app.py"}}]}
    result = await run_turn("fix project", [], provider_fn=provider, tool_executor=ToolExecutor({}),
                            max_rounds=1)
    assert not result.ok
    assert result.final.stop_reason == "max_rounds"
    assert result.error == "max_rounds"


def test_failed_checks_cannot_be_ok_even_for_legacy_success():
    final = SimpleNamespace(success=True, cancelled=False, acceptance={"verified": False})
    assert not TurnResult(text="done", final=final).ok
    assert not TurnResult(text="no final").ok


@pytest.mark.asyncio
async def test_real_file_tools_trigger_checks_and_repair(tmp_path, monkeypatch):
    from aria_code.apps.cli.tools import write_tools, file_tools
    from aria_code.change_store import ChangeStore
    monkeypatch.setattr(write_tools, "_change_store", lambda: ChangeStore())
    monkeypatch.setattr(write_tools, "_ui", lambda: (None, False))
    monkeypatch.setattr(write_tools, "_write_policy", lambda: ["allow"])
    monkeypatch.setattr(write_tools, "_record_checkpoint", lambda *a, **kw: (None, None))
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    checks = []

    def runner(command):
        proc = subprocess.run(command, cwd=tmp_path, shell=True, capture_output=True, text=True)
        checks.append(proc.returncode)
        return {"success": True, "data": {"exit_code": proc.returncode,
                "stdout": proc.stdout, "stderr": proc.stderr}}

    gate = AcceptanceGate(runner=runner, root=tmp_path, max_attempts=2,
                          commands=[f"{shlex.quote(sys.executable)} -B -m pytest -q"])
    executor = ToolExecutor({
        "read_file": (file_tools.tool_read_file, ""),
        "write_file": (write_tools.tool_write_file, ""),
        "edit_file": (write_tools.tool_edit_file, ""),
    }, config={"workspace_root": str(tmp_path)})

    turns = iter([
        {"success": True, "tool_calls_pending": [{"tool": "read_file", "params": {"path": "calc.py"}}]},
        {"success": True, "tool_calls_pending": [{"tool": "write_file", "params":
            {"path": "calc.py", "content": "def add(a, b):\n    return a * b\n"}}]},
        {"success": True, "response": "check it"},
        {"success": True, "tool_calls_pending": [{"tool": "edit_file", "params":
            {"path": "calc.py", "old_string": "a * b", "new_string": "a + b"}}]},
        {"success": True, "response": "fixed and verified"},
    ])
    seen_history = []
    async def provider(prompt, history, **kwargs):
        seen_history.append((prompt, history))
        return next(turns)
    result = await run_turn("修复这个项目", [], provider_fn=provider, tool_executor=executor,
                            acceptance=gate, max_rounds=8,
                            confirm_tools={"write_file", "edit_file"},
                            approval_callback=lambda *a: ApprovalDecision.allow())
    assert result.ok
    assert checks == [1, 0]
    assert result.final.acceptance["verified"] is True
    assert result.final.acceptance["attempts"] == 2
    assert "return a + b" in (tmp_path / "calc.py").read_text()
    assert any("## Tool Results" in prompt for prompt, _ in seen_history)
    assert any("calc.py" in json.dumps(history) for _, history in seen_history)


def test_headless_failed_acceptance_exits_nonzero():
    from aria_code.aria_cli import ArtheraTerminal
    fake = SimpleNamespace(config={})
    events = SimpleNamespace(emit=lambda *a, **kw: None)
    with pytest.raises(SystemExit) as error:
        ArtheraTerminal._finish_prompt(fake, {"success": True, "response": "done",
                                           "acceptance": {"verified": False}},
                                    json_output=True, fmt="json", output_file=None,
                                    quiet=True, machine_out=None, events=events)
    assert error.value.code == 1


def test_directory_grants_are_session_only(tmp_path):
    from aria_code.packages.aria_services.settings import SettingsService
    service = SettingsService(tmp_path, tmp_path / "config.json", tmp_path / "sessions")
    service.save({"model": "google/gemini-2.5-flash", "write_roots": ["/work/shared"],
                  "_session_workspace_root": "/work/project", "_session_read_roots": ["/Volumes/docs"],
                  "_session_write_roots": ["/Volumes/code"]})
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["write_roots"] == ["/work/shared"]
    assert not any(key.startswith("_session_") for key in saved)


def test_denied_approval_never_reaches_handler():
    executor = ToolExecutor({"write_file": (lambda p: pytest.fail("denied tool ran"), "")})
    assert not executor.execute_local("write_file", {"path": "app.py"},
                                     approval=ApprovalDecision.deny())["success"]


@pytest.mark.asyncio
async def test_acceptance_runs_in_host_workspace_and_uses_typed_approval(tmp_path):
    from aria_code.apps.cli.providers.runtime_bridge import build_acceptance_gate
    calls = []
    executor = ToolExecutor({"run_command": (lambda p: calls.append(p) or
                            {"success": True, "data": {"exit_code": 0}}, "")},
                            config={"_session_workspace_root": str(tmp_path)})
    gate = build_acceptance_gate(executor, {"_session_workspace_root": str(tmp_path),
                                          "acceptance_commands": ["python3 check.py"]})
    gate.record_tool("edit_file", {"success": True, "data": {"path": "app.py", "applied": True}})
    assert (await gate.run()).passed
    assert gate.root == tmp_path
    assert calls[0]["cwd"] == str(tmp_path)
    assert calls[0]["user_approved"] is True
    assert calls[0]["policy"] == "balanced"


@pytest.mark.asyncio
async def test_acceptance_requires_exit_status_and_invalidates_green_after_write(tmp_path):
    gate = AcceptanceGate(root=tmp_path, runner=lambda c: {"success": True}, commands=["pytest"])
    applied = {"success": True, "data": {"path": "app.py", "staged": True, "applied": True}}
    assert gate.record_tool("edit_file", applied) == ("app.py",)
    assert not (await gate.run()).passed
    gate.runner = lambda c: {"success": True, "data": {"exit_code": 0}}
    gate.record_tool("edit_file", applied)
    assert (await gate.run()).passed
    assert gate.summary()["verified"] is True
    gate.record_tool("edit_file", applied)
    assert gate.summary()["verified"] is False
    assert gate.summary()["pending_paths"] == ["app.py"]
