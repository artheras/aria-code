"""Aria working with other models: a different model reviews, and /consult asks one."""
import asyncio
import io
from types import SimpleNamespace

import pytest

from aria_code.runtime import consult
from aria_code.runtime.review import ReviewGate, ReviewReport, parse_review

PASS = '{"verdict": "pass", "summary": "ok", "findings": []}'
BLOCK = '{"verdict": "blocking", "summary": "x", "findings": [{"severity": "blocking", "issue": "race", "file": "a.py", "line": 3}]}'


def run(coro):
    return asyncio.run(coro)


# ── review by a different model ──────────────────────────────────────────────

def test_a_separate_reviewer_is_named_in_the_report_and_the_delivery_lines():
    gate = ReviewGate(reviewer=lambda prompt: PASS, reviewer_name="anthropic/claude-sonnet-5-5")
    report = run(gate.run(goal="g", diff="+x"))
    assert report.reviewer == "anthropic/claude-sonnet-5-5"
    assert report.lines()[0] == "✓ no blocking findings (reviewed by anthropic/claude-sonnet-5-5)"
    assert report.as_dict()["reviewer"] == "anthropic/claude-sonnet-5-5"


def test_the_builders_own_review_says_nothing_extra():
    report = run(ReviewGate(reviewer=lambda prompt: PASS).run(goal="g", diff="+x"))
    assert report.reviewer == "" and "reviewed by" not in report.lines()[0]
    assert "reviewer" not in report.as_dict(), "the Go runtime's report has no such field"


def test_a_reviewer_that_fails_is_named_too():
    def broken(prompt):
        raise RuntimeError("no key")

    report = run(ReviewGate(reviewer=broken, reviewer_name="openai/m").run(goal="g", diff="+x"))
    assert report.verdict == "error" and report.reviewer == "openai/m"


def test_build_review_gate_uses_review_model_with_its_own_provider_and_no_builder_token(monkeypatch):
    from aria_code.apps.cli.providers import runtime_bridge

    seen = {}

    def fake_make(**kwargs):
        seen.update(kwargs)

        async def call(prompt, history):
            return {"success": True, "response": PASS}
        return call

    monkeypatch.setattr(runtime_bridge, "make_provider_fn", fake_make)
    cfg = {"review_gate": True, "review_model": "anthropic/claude-sonnet-5-5"}
    gate = runtime_bridge.build_review_gate(cfg, model="google/gemini-3.8-flash", api_url="https://aria.example",
                                            ollama_url="http://x", auth_token="builder-secret")
    assert seen["model"] == "anthropic/claude-sonnet-5-5"
    assert seen["api_url"] is None and seen["auth_token"] is None, "the builder's backend and token must not follow the diff"
    assert gate.reviewer_name == "anthropic/claude-sonnet-5-5"
    # Unset, or the same model: the builder reviews, with its own route.
    seen.clear()
    same = runtime_bridge.build_review_gate({"review_gate": True}, model="google/gemini-3.8-flash",
                                            api_url="https://aria.example", ollama_url="http://x", auth_token="t")
    assert seen["model"] == "google/gemini-3.8-flash" and seen["auth_token"] == "t" and same.reviewer_name == ""


# ── /consult ─────────────────────────────────────────────────────────────────

def test_resolve_model_accepts_aliases_and_qualified_ids_only():
    assert consult.resolve_model("claude") == "anthropic/claude-sonnet-5-5"
    assert consult.resolve_model("CLAUDE") == "anthropic/claude-sonnet-5-5"
    assert consult.resolve_model("openai/some-model") == "openai/some-model"
    assert consult.resolve_model("google/gemini-3.8-flash") == "google/gemini-3.8-flash"
    assert consult.resolve_model("gpt") is None and consult.resolve_model("qwen2.5:7b") is None
    assert consult.resolve_model("chatgpt", {"consult_aliases": {"chatgpt": "openai/m"}}) == "openai/m"


def test_split_request():
    assert consult.split_request("claude why is this slow?") == ("claude", "why is this slow?")
    assert consult.split_request("claude") == ("claude", "") and consult.split_request("") == ("", "")


def test_ask_gives_the_other_model_only_the_question(monkeypatch):
    from aria_code.apps.cli.providers import runtime_bridge

    seen = {}

    def fake_make(**kwargs):
        seen.update(kwargs)

        async def call(prompt, history):
            seen["prompt"], seen["history"] = prompt, history
            return {"success": True, "response": "  use a lock  "}
        return call

    monkeypatch.setattr(runtime_bridge, "make_provider_fn", fake_make)
    answer = run(consult.ask("anthropic/claude-sonnet-5-5", "is this racy?", {"local_provider": "ollama"}))
    assert answer == "use a lock"
    assert seen["prompt"] == "is this racy?" and seen["history"] == [], "no conversation goes with it"
    assert seen["tool_schemas"] == [] and seen["api_url"] is None and seen["system_override"] == consult.CONSULT_SYSTEM


@pytest.mark.parametrize("result", [{"success": False, "error": "no key"}, {"success": True, "response": "  "}])
def test_ask_raises_when_the_model_does_not_answer(monkeypatch, result):
    from aria_code.apps.cli.providers import runtime_bridge

    async def call(prompt, history):
        return result

    monkeypatch.setattr(runtime_bridge, "make_provider_fn", lambda **kw: call)
    with pytest.raises(RuntimeError):
        run(consult.ask("openai/m", "q", {}))


class _Fake:
    from aria_code.apps.cli.commands.engineering_cmds import EngineeringCommandsMixin as _M
    cmd_consult = _M.cmd_consult
    _eng_say = _M._eng_say
    _eng_note_shadow = _M._eng_note_shadow

    def __init__(self):
        self.context = SimpleNamespace(has_rich=False, console=None)
        self.terminal = SimpleNamespace(config={}, conversation=[])

    def _workflow_named(self, cmd):
        return False


def test_the_command_files_the_answer_in_the_conversation(monkeypatch, capsys):
    async def fake_ask(model, question, config, ollama_url=""):
        return "use a lock"

    monkeypatch.setattr(consult, "ask", fake_ask)
    f = _Fake()
    run(f.cmd_consult("claude is this racy?"))
    out = capsys.readouterr().out
    assert "goes to anthropic" in out and "use a lock" in out
    (note,) = f.terminal.conversation
    assert note["role"] == "user" and note["content"].startswith("[second opinion from anthropic/claude-sonnet-5-5]")
    assert "is this racy?" in note["content"] and "use a lock" in note["content"]


def test_the_command_reports_usage_unknown_models_and_failures(monkeypatch, capsys):
    f = _Fake()
    run(f.cmd_consult(""))
    run(f.cmd_consult("claude"))
    assert capsys.readouterr().out.count("Usage: /consult") == 2
    run(f.cmd_consult("gpt what now?"))
    assert "is not a model" in capsys.readouterr().out

    async def failing(model, question, config, ollama_url=""):
        raise RuntimeError("no API key")

    monkeypatch.setattr(consult, "ask", failing)
    run(f.cmd_consult("claude q"))
    assert "did not answer: no API key" in capsys.readouterr().out
    assert f.terminal.conversation == [], "nothing is filed when there was no answer"


def test_config_set_review_model_validates(monkeypatch):
    import sys
    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli
    from rich.console import Console

    terminal = cli.ArtheraTerminal(dict(cli.DEFAULT_CONFIG))
    buffer = io.StringIO()
    monkeypatch.setattr(terminal.commands.context, "console", Console(file=buffer, color_system=None, width=200))
    monkeypatch.setattr(cli, "save_config", lambda *a, **k: None, raising=False)
    terminal.commands.cmd_config("set review_model=anthropic/claude-sonnet-5-5")
    assert terminal.config["review_model"] == "anthropic/claude-sonnet-5-5"
    terminal.commands.cmd_config("set review_model=claude")
    assert terminal.config["review_model"] == "anthropic/claude-sonnet-5-5", "a bad value changes nothing"
    assert "must name a provider and model" in buffer.getvalue()
    terminal.commands.cmd_config("set review_model=off")
    assert terminal.config["review_model"] == ""
