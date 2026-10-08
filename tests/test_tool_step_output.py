"""Each tool step reads as one block, as Codex shows it.

Recorded on a real coding turn: the command header said "executing command
shell tool"; two "✓ writing file (13910ms)" lines named no file and counted
the person reading the diff; answered approval menus stayed on screen in
full; `python3 -m unittest -v` showed no output (its report is on stderr),
so the model pasted the results into its answer; code blocks were white
slabs in a dark terminal.
"""

from __future__ import annotations

import io
import sys

import pytest
from rich.console import Console


def _console(width=100):
    return Console(file=io.StringIO(), width=width, color_system=None, highlight=False)


def test_command_outcome_shows_the_tail_of_stdout_and_stderr():
    from aria_code.apps.cli.tools.system_tools import print_command_outcome

    console = _console()
    report = "\n".join(f"test_{i} ... ok" for i in range(8)) + "\n" + "-" * 20 + "\nRan 8 tests in 0.001s\n\nOK"
    print_command_outcome(console, 0, "", report, "/Users/someone/.aria/out/abc.log")
    lines = console.file.getvalue().splitlines()
    assert lines[0].strip() == "⎿  exit 0 · 12 lines"
    assert "… +7 lines · full output saved: abc.log" in lines[1]
    assert lines[-1].strip() == "OK" and len(lines) == 2 + 5
    assert "/Users/someone" not in console.file.getvalue()


def test_program_output_is_not_markup():
    from aria_code.apps.cli.tools.system_tools import print_command_outcome

    console = _console()
    print_command_outcome(console, 1, "[bold]not bold[/bold] and a stray [/dim]", "")
    text = console.file.getvalue()
    assert "exit 1" in text and "[bold]not bold[/bold] and a stray [/dim]" in text


@pytest.mark.parametrize("raw, hidden", [
    ("curl -H 'Authorization: Bearer abc.def.ghi' https://x", "abc.def.ghi"),
    ("git clone https://user:hunter2@github.com/o/r", "hunter2"),
    ("OPENAI_API_KEY=sk-live1234567890abcd python run.py", "sk-live1234567890abcd"),
    ("export GITHUB_TOKEN=ghp_abcdefghijklmnop", "ghp_abcdefghijklmnop"),
    ("gemini --key AIzaSyA1234567890abcdefghijk", "AIzaSyA1234567890abcdefghijk"),
])
def test_commands_are_shown_with_secrets_masked(raw, hidden):
    from aria_code.apps.cli.runtime_consumer import _redact_activity_text

    shown = _redact_activity_text(raw, limit=200)
    assert hidden not in shown and "***" in shown


def test_the_step_header_names_the_command():
    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli

    assert cli._format_tool_params("run_command", {"command": "python3 -m unittest -v"}) == "python3 -m unittest -v"
    assert "s3cr3t" not in cli._format_tool_params("run_command", {"command": "mysql --password s3cr3t"})


def _consumer(calls):
    from aria_code.apps.cli.runtime_consumer import TerminalRuntimeEventConsumer

    done = []
    consumer = TerminalRuntimeEventConsumer.__new__(TerminalRuntimeEventConsumer)
    consumer.tool_start_times = {"write_file": [100.0] * len(calls)}
    consumer.tool_params = {"write_file": [{"path": path} for path in calls]}
    consumer.print_tool_done = lambda tool, ms, success=True, summary="": done.append((summary, ms))
    consumer.terminal = type("T", (), {"_transcript_log": []})()
    consumer.tool_spinner = None
    return consumer, done


def test_the_done_line_names_its_target_and_shows_the_measured_time(monkeypatch):
    """Two writes announced together, approved one after the other, reported
    together: each line shows its own file and the time the runtime measured,
    not the time since it was announced."""
    from aria_code.apps.cli.runtime_consumer import with_measured_elapsed

    monkeypatch.setattr("aria_code.apps.cli.runtime_consumer.time.time", lambda: 113.9)
    consumer, done = _consumer(["src/fx.py", "test_fx.py"])
    consumer.on_tool_result("write_file", with_measured_elapsed({"success": True}, 0.009))
    consumer.on_tool_result("write_file", with_measured_elapsed({"success": True}, 0.012))
    assert done == [("fx.py", 9), ("test_fx.py", 12)]


def test_without_a_measurement_the_announcement_time_is_used(monkeypatch):
    monkeypatch.setattr("aria_code.apps.cli.runtime_consumer.time.time", lambda: 100.25)
    consumer, done = _consumer(["fx.py"])
    consumer.on_tool_result("write_file", {"success": True})
    assert done == [("fx.py", 250)]


def test_the_gateway_passes_the_measured_time_on():
    import asyncio
    from aria_code.runtime.gateway import run_turn
    from aria_code.runtime.tool_executor import ToolExecutor

    rounds = iter([
        {"success": True, "tool_calls_pending": [{"tool": "ping", "params": {}}]},
        {"success": True, "response": "done"},
    ])

    async def provider(*args, **kwargs):
        return next(rounds)

    seen = []
    original = {"success": True, "data": {"message": "pong"}}
    result = asyncio.run(run_turn("ping", [], provider_fn=provider,
                     tool_executor=ToolExecutor({"ping": (lambda p: original, "")}),
                     on_tool_result=lambda tool, data: seen.append(data)))
    assert result.ok
    assert isinstance(seen[0]["_elapsed_s"], float) and seen[0]["_elapsed_s"] >= 0
    assert "_elapsed_s" not in original


def test_an_approval_collapses_to_what_was_approved(monkeypatch):
    """Through aria_cli, which rebinds the approval function to its own
    globals: a helper defined in tool_executor was not visible there, and the
    first recorded run crashed every approval with a NameError."""
    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli

    seen = {}

    def fake_select(options, selected=0, title="", **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_arrow_select", fake_select)
    monkeypatch.setattr(cli, "HAS_RICH", False)
    decision = cli._confirm_tool_execution_decision(
        "write_file", {"path": "/tmp/w/fx.py", "content": "x = 1\n"}, config_policy="safe")
    assert decision.approved and seen["collapse_to"] == "fx.py"
    seen.clear()
    cli._confirm_tool_execution_decision(
        "run_command", {"command": "TOKEN=abc123 python3 -m unittest -v"}, config_policy="safe")
    assert "abc123" not in seen.get("collapse_to", "") and "unittest" in seen.get("collapse_to", "")


def test_the_answer_rules_say_not_to_paste_back_tool_output():
    from aria_code.apps.cli.prompts.system_prompts import build_response_style_rule

    assert "do not paste back" in build_response_style_rule("en")
    assert "不要整段重贴" in build_response_style_rule("zh")


def test_the_coding_prompt_asks_for_a_short_summary():
    """A coding turn gets CODING_SYSTEM_PROMPT, not the answer-style rule: the
    recorded turn pasted both files and the whole test run back."""
    from aria_code.apps.cli.prompts.coding import CODING_SYSTEM_PROMPT

    assert "## FINAL SUMMARY" in CODING_SYSTEM_PROMPT
    assert "Do NOT paste file contents" in CODING_SYSTEM_PROMPT
