"""`aria-code -p` for scripts and CI: JSON only on stdout, events as they happen, exit 1 on failure.

With --json, tool steps and warnings went to stdout ahead of the JSON, so
`aria-code -p … --json | jq` could not parse it; a failed turn exited 0 unless
the format was table; and nothing was visible until the turn ended.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aria_code.apps.cli.exec_events import ExecEvents, json_safe


def test_events_are_one_json_object_per_line():
    out = io.StringIO()
    events = ExecEvents(out)
    events.emit("turn.started", prompt="hi", model="m")
    events.tool_started("run_command", {"command": "curl -H 'Authorization: Bearer abc123def456' x",
                                        "_run_id": "r1"})
    events.tool_completed("run_command", {"success": False, "error": "boom",
                                          "data": {"exit_code": 2}, "_elapsed_s": 1.23456})
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [e["type"] for e in lines] == ["turn.started", "tool.started", "tool.completed"]
    assert "abc123def456" not in lines[1]["params"]["command"]
    assert "_run_id" not in lines[1]["params"]
    assert lines[2] | {"ts": 0} == {"type": "tool.completed", "tool": "run_command", "success": False,
                                     "elapsed_s": 1.235, "error": "boom", "exit_code": 2, "ts": 0}


def test_a_file_body_is_reported_by_size():
    out = io.StringIO()
    ExecEvents(out).tool_started("write_file", {"path": "a.py", "content": "x" * 5000})
    assert json.loads(out.getvalue())["params"] == {"path": "a.py", "content": "<5000 chars>"}


def test_no_stream_writes_nothing_and_odd_values_still_serialise():
    ExecEvents(None).emit("turn.started", prompt="x")
    assert json_safe({"p": Path("/a"), "s": {1}}) == {"p": "/a", "s": "{1}"}


def test_visible_answer_streaming_is_opt_in_and_status_is_redacted(monkeypatch):
    monkeypatch.delenv("ARIA_EVENTS_STREAM", raising=False)
    out = io.StringIO()
    old = ExecEvents(out)
    old.text_delta("invisible until completion")
    old.status("thinking", "status")
    assert not old.streaming and out.getvalue() == ""
    monkeypatch.setenv("ARIA_EVENTS_STREAM", "1")
    events = ExecEvents(out)
    assert events.streaming and not ExecEvents(None).streaming
    events.text_delta("你好\n```python\nprint(1)\n```\n")
    events.status("working", "Authorization: Bearer abc123def456")
    records = [json.loads(line) for line in out.getvalue().splitlines()]
    assert records[0]["type"] == "answer.delta" and records[0]["text"].startswith("你好\n```")
    assert records[1]["type"] == "turn.status" and "abc123def456" not in records[1]["message"]


@pytest.mark.asyncio
async def test_headless_runtime_streams_visible_text_and_retains_approval(monkeypatch):
    from aria_code.aria_cli import ArtheraTerminal
    from aria_code.apps.cli.providers import runtime_bridge

    monkeypatch.setenv("ARIA_EVENTS_STREAM", "1")
    terminal = ArtheraTerminal.__new__(ArtheraTerminal)
    terminal.config = {"model": "google/gemini-3.5-flash", "permission_mode": "read-only"}
    terminal.api_url = None
    terminal.commands = SimpleNamespace(is_command=lambda _: False)
    terminal._maybe_show_intent_preflight = lambda *a, **k: False
    async def no_intercept(_):
        return False
    terminal._try_football_nl_intercept = no_intercept
    namespace = terminal._run_prompt_turn.__func__.__globals__
    monkeypatch.setitem(namespace, "_run_deterministic_chain", lambda *a, **k: {"success": False})
    monkeypatch.setitem(namespace, "HAS_RICH", False)
    monkeypatch.setitem(namespace, "_auto_approve_session", False)
    monkeypatch.setitem(namespace, "_session_always_allow", set())
    async def turn(**kwargs):
        assert not kwargs["approval_callback"]("write_file", {"path": "a.py"}).approved
        assert kwargs.get("on_thinking") is None
        kwargs["on_token"]("你好")
        kwargs["on_status"]("working", "Reading a project")
        kwargs["on_token"]("世界")
        return SimpleNamespace(ok=True, text="你好世界", error="", final=SimpleNamespace(provider="fake"))
    monkeypatch.setattr(runtime_bridge, "run_chat_via_runtime", turn)
    out = io.StringIO()
    events = ExecEvents(out)
    result = await terminal._run_prompt_turn("hello", quiet=True, events=events)
    assert result["success"] and result["response"] == "你好世界"
    records = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [e["type"] for e in records] == ["turn.status", "answer.delta", "turn.status", "answer.delta"]
    assert records[0]["state"] == "approval_denied" and "write_file" in records[0]["message"]


class _Terminal:
    """Just enough of ArtheraTerminal for _finish_prompt."""

    config = {"ui_lang": "en"}

    def __init__(self):
        from aria_code.aria_cli import ArtheraTerminal
        self._finish = ArtheraTerminal._finish_prompt.__get__(self)


@pytest.mark.parametrize("fmt, json_output", [("table", True), ("json", False), ("jsonl", False)])
def test_a_failed_turn_exits_1_in_every_machine_format(fmt, json_output, capsys):
    out = io.StringIO()
    events = ExecEvents(out if fmt == "jsonl" else None)
    with pytest.raises(SystemExit) as exited:
        _Terminal()._finish({"success": False, "response": "", "error": "no model"},
                            json_output=json_output, fmt=fmt, output_file=None, quiet=True,
                            machine_out=out, events=events)
    assert exited.value.code == 1
    body = out.getvalue()
    parsed = [json.loads(l) for l in body.splitlines()] if fmt == "jsonl" else [json.loads(body)]
    assert parsed[-1]["success"] is False and parsed[-1]["error"] == "no model"


def test_a_successful_json_turn_writes_only_json(capsys):
    out = io.StringIO()
    _Terminal()._finish({"success": True, "response": "done", "tools_used": ["edit_file"]},
                        json_output=True, fmt="table", output_file=None, quiet=True,
                        machine_out=out, events=ExecEvents(None))
    assert json.loads(out.getvalue())["response"] == "done"
    assert capsys.readouterr().out == ""


def test_the_cli_keeps_stdout_for_json(tmp_path):
    """A slash command end to end: its own output goes to stderr, the events to stdout."""
    aria = Path(sys.executable).parent / "aria-code"
    if not aria.exists():
        pytest.skip("aria-code entry point not installed in this environment")
    done = subprocess.run([str(aria), "-p", "/help", "--format", "jsonl"], cwd=tmp_path,
                          capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    events = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    assert [e["type"] for e in events] == ["turn.started", "turn.completed"]
    assert events[-1]["success"] is True and done.returncode == 0
    assert done.stderr.strip()          # /help printed, just not on stdout
