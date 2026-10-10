"""-p with --dangerously-skip-permissions can run the scripts it writes.

Those flags used to take the tools out of the confirm set, so no approval
ever happened, and an approval is what lifts run_command past the default
"safe" policy. In the Vertex evals the agent wrote calculate_reorder.py, was
told "blocked by the security policy", and then computed by hand (a wrong
total) or stopped (no output file).
"""

from __future__ import annotations

import inspect
import os
import shlex
import subprocess
import sys

from aria_code.apps.cli.headless import _apply_approval_decision, _headless_approval


def test_a_granted_session_approves_commands_with_the_upgrade():
    decide = _headless_approval({"command_policy": "safe"}, lambda: True, lambda: set())
    decision = decide("run_command", {"command": "python3 calc.py"})
    params = {"command": "python3 calc.py"}
    _apply_approval_decision(params, decision)
    assert decision.approved and params["user_approved"] is True and params["policy"] == "safe"


def test_allow_tools_grants_only_what_it_names():
    decide = _headless_approval({}, lambda: False, lambda: {"run_command"})
    assert decide("run_command", {"command": "pytest"}).approved
    assert not decide("write_file", {"path": "a.py"}).approved


def test_nothing_granted_is_refused_with_the_way_to_grant_it():
    decision = _headless_approval({}, lambda: False, lambda: set())("run_command", {"command": "ls"})
    assert not decision.approved and "--allow-tools" in decision.reason


def test_an_approved_script_runs_under_the_safe_policy(tmp_path, monkeypatch):
    from aria_code.apps.cli.tools.system_tools import tool_run_command

    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    (tmp_path / "calc.py").write_text("print(2 + 3)\n")
    argv = [sys.executable, "calc.py"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    params = {"command": command, "policy": "safe", "cwd": str(tmp_path),
              "permission_mode": "workspace-write", "network_enabled": False}
    blocked = tool_run_command(dict(params), has_rich=False)
    assert not blocked["success"] and "blocked by policy" in blocked["error"]
    decision = _headless_approval({"command_policy": "safe"}, lambda: True, lambda: set())("run_command", params)
    _apply_approval_decision(params, decision)
    assert tool_run_command(params, has_rich=False)["data"]["stdout"].strip() == "5"


def test_the_headless_turn_keeps_the_confirm_set_and_answers_it():
    import aria_code.aria_cli as cli

    source = inspect.getsource(cli.ArtheraTerminal._run_prompt_turn)
    assert "confirm_tools=frozenset(_CONFIRM_TOOLS)" in source
    assert "approval_callback=_headless_approval(" in source
    assert "lambda: _auto_approve_session" in source
