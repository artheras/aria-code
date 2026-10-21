"""Token usage travels from the provider, through a headless run, into the eval report."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from aria_code.evals import TaskSpec, run_task
from aria_code.evals.harness import SuiteResult, TaskResult


# ── provider: ask for usage and read the last chunk ────────────────────────────

def _sse(chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


@pytest.fixture
def fake_openai(monkeypatch):
    from aiohttp import web

    # A machine-wide proxy would otherwise be used even for 127.0.0.1.
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)

    seen = {}
    chunks = [
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 30,
                                  "completion_tokens_details": {"reasoning_tokens": 10}}},
    ]

    async def handler(request):
        seen["body"] = await request.json()
        return web.Response(text=_sse(chunks), content_type="text/event-stream")

    async def start():
        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return runner, f"http://127.0.0.1:{port}"

    return start, seen


@pytest.mark.parametrize("cls_name, asks", [("OpenAIProvider", True), ("DeepSeekProvider", True), ("GroqProvider", False)])
def test_known_endpoints_ask_for_usage_and_report_it(fake_openai, cls_name, asks):
    from aria_code.providers.llm import openai_compat
    from aria_code.providers.llm.base import Message, ProviderConfig

    start, seen = fake_openai

    async def scenario():
        runner, base = await start()
        try:
            provider = getattr(openai_compat, cls_name)(ProviderConfig(name=cls_name, api_key="k", base_url=base, model="m"))
            return [event async for event in provider.stream([Message(role="user", content="x")])]
        finally:
            await runner.cleanup()

    events = asyncio.run(scenario())
    done = [e for e in events if e["type"] == "done"]
    assert done, events
    assert ("stream_options" in seen["body"]) is asks
    if asks:
        # completion excludes the reasoning tokens, which are reported apart
        assert done[0]["usage"] == {"prompt_tokens": 120, "completion_tokens": 20, "thinking_tokens": 10}
    else:
        assert done[0]["usage"] in ({}, {"prompt_tokens": 120, "completion_tokens": 20, "thinking_tokens": 10})


# ── headless: report usage and leave it where the harness asked ────────────────

def test_a_headless_run_reports_usage_and_writes_it_where_asked(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from aria_code.apps.cli.exec_events import ExecEvents
    from aria_code.aria_cli import ArtheraTerminal

    target = tmp_path / "usage.json"
    monkeypatch.setenv("ARIA_USAGE_FILE", str(target))
    terminal = SimpleNamespace(config={"model": "gpt-x", "local_provider": "openai", "ui_lang": "en"},
                               _session_input_tokens=5, _session_output_tokens=7,
                               _session_thinking_tokens=2, _session_turns=1)
    result = {"success": True, "response": "done"}
    ArtheraTerminal._finish_prompt(terminal, result, json_output=False, fmt="jsonl", output_file=None,
                                 quiet=True, machine_out=None, events=ExecEvents(None))
    written = json.loads(target.read_text())
    assert written == result["usage"]
    assert (written["input_tokens"], written["output_tokens"], written["thinking_tokens"]) == (5, 7, 2)
    assert written["model"] == "gpt-x" and written["provider"] == "openai"


# ── harness: carry the solver's usage into the result and the report ───────────

def _fixture(root: Path) -> Path:
    fixtures = root / "fixtures"
    (fixtures / "demo").mkdir(parents=True)
    (fixtures / "demo" / "check.py").write_text(
        "import pathlib, sys\nsys.exit(0 if (pathlib.Path(__file__).parent / 'fixed.txt').exists() else 1)\n")
    return fixtures


class _Done:
    returncode = 0
    stdout = ""
    stderr = ""


def test_the_report_carries_usage_and_says_how_many_tasks_reported_it(tmp_path):
    fixtures = _fixture(tmp_path)
    task = TaskSpec(id="demo", prompt="fix", verify=f"{sys.executable} check.py", fixture="demo")

    def solver(prompt, workspace):
        (workspace / "fixed.txt").write_text("ok")
        done = _Done()
        done.usage = {"model": "m", "input_tokens": 100, "output_tokens": 40, "thinking_tokens": 5}
        return done

    result = run_task(task, solver=solver, fixtures_root=fixtures)
    assert result.usage["input_tokens"] == 100
    assert result.to_dict()["usage"]["output_tokens"] == 40
    silent = TaskResult(task_id="other", outcome="fail")
    totals = SuiteResult(name="s", results=[result, silent]).usage_totals()
    assert totals["reported"] == 1 and totals["tasks"] == 2
    assert (totals["input_tokens"], totals["output_tokens"], totals["thinking_tokens"]) == (100, 40, 5)
    assert "usage" not in silent.to_dict()


def test_the_runner_reads_and_removes_the_usage_file(tmp_path):
    from aria_code.evals.runner import _read_usage

    path = tmp_path / ".usage-x.json"
    path.write_text(json.dumps({"input_tokens": 3}))
    assert _read_usage(path) == {"input_tokens": 3} and not path.exists()
    assert _read_usage(path) == {}
