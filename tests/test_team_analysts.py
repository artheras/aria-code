""""/team AAPL" runs the whole team, on the configured model, in the UI's language.

Recorded on Gemini: "4 analysts in parallel", then only the technical one,
a 0.1-second template synthesis, and every label in Chinese in the English UI.

- The agent registry imported its built-ins as "aria_code.agents.…" while the
  CLI loads it as "agents.…": the second copy's classes subclass another
  BaseAgent, issubclass() said no, and macro, fundamental and risk were
  dropped without a word.
- The team was always given an Ollama provider at localhost:11434, whatever
  model was configured.
- The synthesis agent was built without the team's language.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

sys.argv = ["aria-code"]
import aria_code.aria_cli  # noqa: E402,F401 — the CLI's import state, both roots in play


@pytest.mark.parametrize("module", ["agents.registry", "aria_code.agents.registry"])
def test_every_default_analyst_is_registered_under_either_root(module):
    import importlib

    registry = importlib.import_module(module).get_registry()
    for name in ("coder", "tester", "portfolio", "supervisor"):
        assert registry.get(name) is not None, (module, name)


def test_a_cloud_model_gets_the_configured_provider_and_a_local_one_ollama():
    from aria_code.apps.cli.commands.team import ConfiguredTeamProvider, build_team_llm_provider

    assert isinstance(build_team_llm_provider({"model": "google/gemini-2.5-pro"}), ConfiguredTeamProvider)
    local = build_team_llm_provider({"model": "qwen2.5:7b", "local_provider": "ollama"})
    assert type(local).__name__ == "OllamaProvider"


def test_the_configured_provider_speaks_the_agents_stream(monkeypatch):
    import aria_code.apps.cli.providers.base as base
    from aria_code.apps.cli.commands.team import ConfiguredTeamProvider
    from aria_code.providers.llm.base import Message

    class Token:
        def __init__(self, text):
            self.text = text

    class Done:
        def __init__(self, success, error=""):
            self.success, self.error = success, error

    seen = {}

    class Fake:
        def __init__(self, config, model):
            seen["model"] = model

        async def stream(self, messages, tools=None):
            seen["messages"] = messages
            yield Token("HOLD ")
            yield Token("for now")
            yield Done(True)

    monkeypatch.setattr(base, "ConfiguredProvider", Fake)
    monkeypatch.setattr(base, "event_kind", lambda e: {"Token": "LLMToken", "Done": "LLMDone"}[type(e).__name__])

    async def run():
        provider = ConfiguredTeamProvider({"model": "google/gemini-2.5-pro"}, "google/gemini-2.5-pro")
        return [e async for e in provider.stream([Message(role="user", content="AAPL?")])]

    events = asyncio.run(run())
    assert [e["text"] for e in events if e["type"] == "token"] == ["HOLD ", "for now"]
    assert seen["messages"] == [{"role": "user", "content": "AAPL?"}]


def test_the_synthesis_agent_gets_the_team_language():
    import inspect

    from agents.team import AgentTeam

    source = inspect.getsource(AgentTeam.run)
    synth = source[source.index("synth_cls("):source.index(")", source.index("synth_cls("))]
    assert "lang=self.lang" in synth


def test_the_panel_reads_in_the_ui_language():
    from aria_code.apps.cli.commands.team import build_team_terminal_summary, clean_team_synthesis_text
    from agents.team import _template_synthesis

    assert "Data:" in build_team_terminal_summary(None, lang="en")
    assert "数据:" in build_team_terminal_summary(None, lang="zh")
    assert clean_team_synthesis_text("## 团队分析汇总\n**TECHNICAL** ok") == "团队分析汇总\nTECHNICAL ok"
    assert clean_team_synthesis_text("> \u26a0\ufe0f 1 of 4 analysts did not finish") == "\u26a0 1 of 4 analysts did not finish"
    assert _template_synthesis([], lang="en") == "Analysis finished with no results."
