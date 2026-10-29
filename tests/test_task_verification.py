"""Acceptance must use host evidence, survive restart and reject unsafe paths."""
from dataclasses import asdict
import json
import os
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from aria_code.runtime import subagent as sa
from aria_code.runtime.events import ToolCallRecord
from aria_code.runtime.task_ledger import TaskLedger
from aria_code.runtime.task_verification import (
    MAX_BYTES, capture_baseline, receipt_from_record, validate_checks, verify_checks,
)
from aria_code.apps.cli.providers import runtime_bridge as bridge
from aria_code.workspace.files import WorkspaceFiles, WorkspaceSecurity


@pytest.fixture(autouse=True)
def isolated_scheduler(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "_TASKS", {})
    monkeypatch.setattr(sa, "_RUNNER", None)
    monkeypatch.setattr(sa, "_LEDGER", TaskLedger(tmp_path / "ledger.json"))


def artifact(path="report.json", **conditions):
    return {"kind": "artifact", "path": path, "format": "json", **conditions}


def record(tool="run_command", success=True, code=0, **params):
    return ToolCallRecord(tool, params, {"success": success, "data": {"exit_code": code}}, 1, 1, 2)


@pytest.mark.parametrize("checks", [
    [{"kind": []}], [{"kind": "artifact", "path": "x", "format": []}],
    [artifact("../outside.json")], [artifact("/tmp/outside.json")],
    [artifact("C:/secret.json")], [artifact("a\\b")],
    [artifact(contains="not allowed for JSON")], [artifact(required_keys=[3])],
    [artifact(min_bytes=True)], [artifact(changed="true")],
    [{"kind": "tool", "tool": "run_command"}],
    [{"kind": "command", "command": "test", "cwd": "../outside"}],
    [artifact(extra=True)], [artifact()] * 17,
])
def test_invalid_contracts_fail_before_spawning(checks):
    result = sa.tool_spawn_task({"prompt": "inspect", "checks": checks})
    assert not result["success"]
    assert not sa._TASKS


def test_artifact_formats_and_conditions(tmp_path):
    (tmp_path / "report.json").write_text('{"audience":"adult learners"}')
    (tmp_path / "survey.csv").write_text("user,score\na,4\nb,5\n")
    (tmp_path / "brief.md").write_text("Adult language learners")
    checks = [artifact(required_keys=["audience"]),
              {"kind": "artifact", "path": "survey.csv", "format": "csv", "required_keys": ["user", "score"], "min_rows": 2},
              {"kind": "artifact", "path": "brief.md", "format": "text", "contains": "language", "min_bytes": 10}]
    report = verify_checks(str(tmp_path), checks, [])
    assert report["verified"] is True
    assert report["checks"][1]["rows"] == 2
    assert "adult learners" not in json.dumps(report)


@pytest.mark.parametrize("content", ["not json", '{"value":NaN}', "[]", '{"missing": 1}', '{"audience":1,"audience":2}', '{"value":1e999}'])
def test_invalid_json_or_missing_fields_cannot_pass(tmp_path, content):
    (tmp_path / "report.json").write_text(content)
    assert verify_checks(str(tmp_path), [artifact(required_keys=["audience"])], [])["verified"] is False


@pytest.mark.parametrize("content", ["a,a\n1,2\n", "a,b\n1\n", "a,b\n1,2,3\n", "a,b\n"])
def test_csv_structure_and_row_counts(tmp_path, content):
    (tmp_path / "data.csv").write_text(content)
    check = {"kind": "artifact", "path": "data.csv", "format": "csv", "min_rows": 1}
    assert verify_checks(str(tmp_path), [check], [])["verified"] is False


def test_digest_and_change_require_actual_bytes(tmp_path):
    path = tmp_path / "report.json"
    path.write_text('{"ok":1}')
    checks = [artifact(changed=True)]
    baseline = capture_baseline(str(tmp_path), checks)
    assert verify_checks(str(tmp_path), checks, [], baseline)["verified"] is False
    path.write_text('{"ok":2}')
    assert verify_checks(str(tmp_path), checks, [], baseline)["verified"] is True
    assert verify_checks(str(tmp_path), [artifact(sha256="0" * 64)], [])["verified"] is False


def test_new_file_baseline_and_size_limit(tmp_path):
    check = artifact(changed=True)
    baseline = capture_baseline(str(tmp_path), [check])
    assert baseline == {"report.json": None}
    (tmp_path / "report.json").write_text("{}")
    assert verify_checks(str(tmp_path), [check], [], baseline)["verified"] is True
    (tmp_path / "huge.bin").write_bytes(b"a" * (MAX_BYTES + 1))
    assert not verify_checks(str(tmp_path), [{"kind": "artifact", "path": "huge.bin"}], [])["verified"]


def test_symlink_files_and_directories_are_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.json").write_text("{}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        (workspace / "link").symlink_to(outside, target_is_directory=True)
        (workspace / "report.json").symlink_to(outside / "report.json")
    except OSError:
        pytest.skip("Symlink creation unavailable on this host")
    for name in ["link/report.json", "report.json"]:
        assert verify_checks(str(workspace), [artifact(name)], [])["verified"] is False


def test_command_needs_explicit_exit_code_and_latest_receipt(tmp_path):
    check = {"kind": "command", "command": "test-command", "cwd": "."}
    good = receipt_from_record(record(command="test-command", cwd=str(tmp_path)), str(tmp_path))
    bad = receipt_from_record(record(code=1, command="test-command", cwd=str(tmp_path)), str(tmp_path))
    missing = receipt_from_record(record(code=None, command="test-command", cwd=str(tmp_path)), str(tmp_path))
    assert verify_checks(str(tmp_path), [check], [good])["verified"] is True
    for receipts in [[], [missing], [good, bad], [{**good, "source": "model_report"}]]:
        assert verify_checks(str(tmp_path), [check], receipts)["verified"] is False
    assert verify_checks(str(tmp_path), [check], [bad, good])["verified"] is True
    assert verify_checks(str(tmp_path), [check], [good], receipts_truncated=True)["verified"] is False
    assert "test-command" not in json.dumps(good)


@pytest.mark.asyncio
async def test_prose_is_not_execution_evidence(tmp_path):
    async def runner(prompt, task):
        return "VERIFICATION: all tests passed. Everything is complete."

    sa.register_runner(runner)
    spawned = sa.tool_spawn_task({"prompt": "check tests", "_workspace": str(tmp_path),
                                  "checks": [{"kind": "command", "command": "pytest"}]})
    task = sa._TASKS[spawned["task_id"]]
    await task.async_task
    assert task.status == "done"
    result = sa.tool_task_result({"task_id": task.task_id})
    assert not result["success"] and result["verified"] is False
    assert "all tests passed" in result["result"]  # claims preserved, not endorsed


@pytest.mark.asyncio
async def test_real_gateway_receipts_flow_to_task_acceptance(tmp_path, monkeypatch):
    (tmp_path / "brief.md").write_text("Education product for adult learners")
    count = 0

    async def provider(message, history, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return {"success": True, "response": "", "tool_calls_pending": [
                {"tool": "read_file", "params": {"path": "brief.md"}}]}
        return {"success": True, "response": "FINDINGS: education product; VERIFICATION: read brief.md", "tool_calls_pending": []}

    monkeypatch.setattr(bridge, "make_provider_fn", lambda **kw: provider)
    files = WorkspaceFiles(WorkspaceSecurity(tmp_path, allow_home=False))
    tools = {"read_file": (lambda p: {"success": True, "data": asdict(files.read_file(p["path"]))}, "")}

    async def runner(prompt, task):
        return await bridge.run_subagent_turn(prompt, task, local_tools=tools,
                                              tool_schemas=[{"name": "read_file"}],
                                              config={"acceptance_gate": False}, api_url=None)

    sa.register_runner(runner)
    spawned = sa.tool_spawn_task({"prompt": "research this education product", "agent": "researcher",
                                  "_workspace": str(tmp_path), "checks": [
                                      {"kind": "tool", "tool": "read_file", "path": "brief.md"},
                                      {"kind": "artifact", "path": "brief.md", "format": "text", "contains": "Education"}]})
    task = sa._TASKS[spawned["task_id"]]
    await task.async_task
    result = sa.tool_task_result({"task_id": task.task_id})
    assert result["verified"] is True, result
    assert task.evidence[0]["source"] == "runtime_tool_executor"
    assert "adult learners" not in json.dumps(task.evidence)
    assert result["verification"]["coverage"] == "declared_predicates_only"

    sa._TASKS.clear()
    sa.restore_tasks()
    assert sa._TASKS[task.task_id].verification["status"] == "stale"
    (tmp_path / "brief.md").write_text("Content no longer satisfies the predicate")
    changed = sa.tool_task_result({"task_id": task.task_id})
    assert changed["verified"] is False
    (tmp_path / "brief.md").unlink()
    assert sa.tool_task_result({"task_id": task.task_id})["verified"] is False


def test_legacy_done_without_checks_stays_explicitly_unverified(tmp_path):
    task = sa.SubagentTask("old", "old task", status="done", result="done", workspace=str(tmp_path))
    sa._TASKS[task.task_id] = task
    result = sa.tool_task_result({"task_id": task.task_id})
    assert result["success"] is True
    assert result["verified"] is None
    assert result["verification"]["status"] == "not_requested"


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 1])
async def test_actual_approved_command_exit_is_captured(tmp_path, monkeypatch, exit_code):
    from aria_code.apps.cli.headless import _headless_approval
    from aria_code.apps.cli.tools.system_tools import tool_run_command

    (tmp_path / "check.py").write_text(f"import sys\nprint('checked')\nsys.exit({exit_code})\n")
    argv = [sys.executable, "check.py"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    records = []
    turns = 0

    async def provider(message, history, **kwargs):
        nonlocal turns
        turns += 1
        if turns == 1:
            return {"success": True, "response": "", "tool_calls_pending": [
                {"tool": "run_command", "params": {"command": command}}]}
        return {"success": True, "response": "The delegate claims completion", "tool_calls_pending": []}

    monkeypatch.setattr(bridge, "make_provider_fn", lambda **kw: provider)
    config = {"acceptance_gate": False, "command_policy": "safe", "network_enabled": False,
              "permission_mode": "workspace-write", "_session_workspace_root": str(tmp_path)}
    await bridge.run_chat_via_runtime(
        prompt="Run the requested local check", history=[],
        local_tools={"run_command": (lambda p: tool_run_command(p, has_rich=False), "")},
        tool_schemas=[{"name": "run_command"}], model="test", config=config,
        api_url=None, ollama_url="http://unused", tool_record_sink=records.append,
        confirm_tools=("run_command",),
        approval_callback=_headless_approval(config, lambda: True, lambda: set()),
    )
    assert len(records) == 1
    receipt = receipt_from_record(records[0], str(tmp_path))
    assert receipt["exit_code"] == exit_code
    # Acceptance only inspects the receipt, even when the delegate claimed a pass.
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("Verifier ran a command"))
    report = verify_checks(str(tmp_path), [{"kind": "command", "command": command}], [receipt])
    assert report["verified"] is (exit_code == 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("final,cancelled", [
    (SimpleNamespace(success=False, acceptance=None), False),
    (SimpleNamespace(success=True, acceptance={"verified": False}), False),
    (None, True),
])
async def test_partial_runtime_failure_is_not_accepted(monkeypatch, final, cancelled):
    async def fake_turn(**kwargs):
        return SimpleNamespace(text="I completed it", error=None, final=final, cancelled=cancelled)

    monkeypatch.setattr(bridge, "run_chat_via_runtime", fake_turn)
    with pytest.raises(RuntimeError, match="accepted execution"):
        await bridge.run_subagent_turn("inspect", sa.SubagentTask("one", "inspect"),
                                       local_tools={}, tool_schemas=[], config={}, api_url=None)
