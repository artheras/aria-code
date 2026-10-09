"""The transcript as actions: Explored / Ran / Edit cells, and approvals that talk back."""

import asyncio
import io
import sys

from aria_code.runtime import AgentEventComplete, AgentOptions, ApprovalDecision, ToolExecutor, run_agent
from aria_code.ui.render.actions import ActionView, explore_item


def _texts(lines):
    return [text for _, text in lines]


def test_reads_and_searches_coalesce_into_one_explored_cell():
    view = ActionView()
    out = []
    for tool, params in [("read_file", {"path": "src/a.py"}), ("read_file", {"path": "src/b.py"}),
                         ("read_file", {"path": "src/a.py"}), ("search_code", {"query": "refresh"}),
                         ("read_file", {"path": "src/c.py"})]:
        out += view.start(tool, params) + view.done(tool, params, {"success": True})
    assert out == [], "nothing prints while exploring; the spinner covers it"
    assert _texts(view.flush()) == [
        "⏺  Explored",
        "   └ Read a.py, b.py",
        '     Search "refresh"',
        "     Read c.py",
    ]
    assert view.flush() == []


def test_the_next_action_flushes_exploration_first():
    view = ActionView()
    view.start("read_file", {"path": "a.py"})
    view.done("read_file", {"path": "a.py"}, {"success": True})
    lines = _texts(view.start("edit_file", {"path": "src/a.py"}))
    assert lines == ["⏺  Explored", "   └ Read a.py", "⏺  Edit src/a.py"]
    done = view.done("edit_file", {"path": "src/a.py"},
                     {"success": True, "data": {"diff": "--- a\n+++ b\n-x\n+y\n+z\n"}}, 0.012)
    assert _texts(done) == ["   └ ✓ +2 -1 · 12ms"]


def test_commands_and_failures():
    view = ActionView()
    assert _texts(view.start("run_command", {"command": "python3 -m pytest -q"})) == ["⏺  Ran python3 -m pytest -q"]
    assert _texts(view.done("run_command", {}, {"success": True}, 2.1)) == ["   └ ✓ 2.1s"]
    failed = view.done("run_command", {}, {"success": False, "error": "Command blocked by policy"})
    assert failed == [("red", "   └ ✗ Command blocked by policy")]


def test_a_failed_read_is_kept_in_the_cell():
    view = ActionView()
    view.done("read_file", {"path": "missing.py"}, {"success": False, "error": "not found"})
    assert "     ✗ Read missing.py — not found" in _texts(view.flush())


def test_read_only_commands_explore_and_secrets_are_masked():
    assert explore_item("run_command", {"command": "git status"}) == ("Run", "git status")
    assert explore_item("run_command", {"command": "ls > out.txt"}) is None
    view = ActionView()
    assert "***" in view.start("run_command", {"command": "API_TOKEN=abc123 ./deploy.sh"})[0][1]


def test_the_consumer_prints_cells_and_flushes_before_the_answer():
    from aria_code.apps.cli.runtime_consumer import TerminalRuntimeEventConsumer

    class Terminal:
        _transcript_log = []
        _task_list = []

    printed = []
    consumer = TerminalRuntimeEventConsumer(
        terminal=Terminal(), console=None, has_rich=False, markdown_cls=None, live_cls=None,
        strip_latex=lambda s: s, action_view=ActionView(),
        print_tool_call=lambda *a: printed.append("old-call"),
        print_tool_done=lambda *a, **k: printed.append("old-done"),
    )
    out = io.StringIO()
    sys_stdout, sys.stdout = sys.stdout, out
    try:
        consumer.on_tool_call("read_file", {"path": "a.py"})
        consumer.on_tool_result("read_file", {"success": True})
        consumer.flush_actions()
    finally:
        sys.stdout = sys_stdout
    assert printed == [], "the per-call printers are not used"
    assert "⏺  Explored" in out.getvalue() and "└ Read a.py" in out.getvalue()


def test_picker_shortcuts_without_a_tty(monkeypatch):
    from aria_code.ui import picker

    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    options = [("Yes", ""), ("Always", ""), ("No, tell Aria what to do instead", "")]
    assert picker.arrow_select(options, shortcuts={"y": 0, "n": 2}, numbered=True) == 2


def test_a_denial_with_feedback_continues_the_turn():
    prompts = []
    rounds = {"n": 0}

    async def provider_fn(message, history, **kwargs):
        prompts.append(message)
        rounds["n"] += 1
        if rounds["n"] == 1:
            return {"success": True, "response": "", "provider": "fake", "tool_calls_pending": [
                {"tool": "run_command", "params": {"command": "npm install left-pad"}}]}
        return {"success": True, "response": "Used the stdlib instead.", "provider": "fake"}

    ran = []
    executor = ToolExecutor({"run_command": (lambda p: ran.append(p) or {"success": True}, "run")})

    async def collect():
        return [e async for e in run_agent(
            "pad a string", [], provider_fn=provider_fn, tool_executor=executor,
            options=AgentOptions(
                confirm_tools=frozenset({"run_command"}),
                approval_callback=lambda tool, params: ApprovalDecision.deny(
                    "user denied", feedback="no new dependencies, use str.rjust"),
            ))]

    events = asyncio.run(collect())
    assert ran == []
    assert "no new dependencies, use str.rjust" in prompts[1]
    assert isinstance(events[-1], AgentEventComplete) and not events[-1].result.cancelled


def test_a_plain_denial_still_stops():
    async def provider_fn(message, history, **kwargs):
        return {"success": True, "response": "", "provider": "fake", "tool_calls_pending": [
            {"tool": "run_command", "params": {"command": "rm -rf x"}}]}

    async def collect():
        return [e async for e in run_agent(
            "x", [], provider_fn=provider_fn,
            tool_executor=ToolExecutor({"run_command": (lambda p: {"success": True}, "run")}),
            options=AgentOptions(confirm_tools=frozenset({"run_command"}),
                                 approval_callback=lambda t, p: ApprovalDecision.deny("user denied")))]

    events = asyncio.run(collect())
    assert type(events[-1]).__name__ == "AgentEventCancelled"


def _ran(code, stdout="", stderr=""):
    return {"success": True, "data": {"command": "x", "exit_code": code, "stdout": stdout, "stderr": stderr}}


def test_a_passing_command_shows_the_tail_of_its_output():
    view = ActionView()
    out = view.done("run_command", {"command": "pytest -q"}, _ran(0, "a\nb\nc\n5 passed in 0.1s\n"), 0.4)
    assert _texts(out) == ["   └ ✓ 400ms · 4 lines", "     … +2 lines (ctrl+o)", "     c", "     5 passed in 0.1s"]


def test_a_failing_command_is_red_with_more_of_the_tail():
    view = ActionView()
    out = view.done("run_command", {}, _ran(1, "", "E1\nE2\nE3\nE4\nE5\n"), 1.0)
    assert out[0] == ("red", "   └ ✗ exit 1 · 1.0s · 5 lines")
    assert [t for _, t in out[2:]] == ["     E2", "     E3", "     E4", "     E5"]


def test_the_detail_view_has_everything():
    from aria_code.ui.render.actions import format_action_details

    view = ActionView()
    view.done("read_file", {"path": "a.py"}, {"success": True})
    view.done("edit_file", {"path": "a.py"}, {"success": True, "data": {"diff": "--- a\n+++ b\n-x\n+y\n"}}, 0.01)
    view.done("run_command", {"command": "pytest -q"}, _ran(1, "\n".join(f"line {i}" for i in range(200))), 2.0)
    lines = format_action_details(view.details, max_lines=150)
    texts = _texts(lines)
    assert "✓ Read a.py" in texts and "✓ Edit a.py  · 10ms" in texts
    assert ("red", "    -x") in lines and ("green", "    +y") in lines
    assert "✓ $ pytest -q  · 2.0s" in texts and "    exit 1" in texts
    assert "    line 0" in texts and "    … +50 lines" in texts


def test_run_command_can_leave_its_outcome_to_the_transcript(capsys):
    from aria_code.apps.cli.tools.system_tools import tool_run_command

    result = tool_run_command({"command": "echo hi", "policy": "balanced", "user_approved": True},
                              console=None, has_rich=False, quiet=True)
    assert result["success"] and "hi" in result["data"]["stdout"]
    assert "Command exit" not in capsys.readouterr().out
