"""Gemini 3.x and Gemma on Google Cloud, without a Gemini API key.

- `/model google/<anything>` refused without a Gemini API key, though the
  model runs on Vertex AI with Google Cloud credentials.
- The native Vertex path defaulted to us-central1; the Gemini 3 previews are
  served only from the global endpoint.
- Gemma ids matched no capability entry (no tools, a 4K window) and went to
  the Gemini API, which does not serve them.
"""

from __future__ import annotations

import asyncio
import io
import sys
from types import SimpleNamespace

import pytest

from aria_code.apps.cli.providers import base
from aria_code.model_capability import get_model_capability


@pytest.mark.parametrize("model", ["google/gemma-4-26b-a4b-it-maas", "gemma-3-27b-it", "vertexai/gemma-4-31b-it"])
def test_gemma_on_vertex_has_tools_and_a_real_window(model):
    capability = get_model_capability(model)
    assert capability.tool_calls and capability.context_window >= 131072


def test_ollama_gemma_keeps_its_own_entry():
    assert get_model_capability("gemma3:27b").format == "ollama_native"


@pytest.mark.parametrize("model, open_model", [
    ("gemma-4-26b-a4b-it-maas", True), ("gemma-3-27b-it", True), ("llama-4-maverick-17b-128e-instruct-maas", True),
    ("gemini-3.5-flash", False), ("gemini-3.1-pro-preview", False),
])
def test_which_models_take_the_openai_compatible_endpoint(model, open_model):
    assert base.is_vertex_open_model(model) is open_model


def test_gemma_streams_through_vertex_openai_compatible_endpoint(monkeypatch):
    import aria_code.local_llm_provider as local_llm

    monkeypatch.setattr(base, "vertex_openai_endpoint", lambda config: {
        "token": "tok", "project": "p", "location": "global",
        "base_url": "https://aiplatform.googleapis.com/v1/projects/p/locations/global/endpoints/openapi"})
    seen = {}

    class FakeProvider:
        async def stream(self, messages, tools=None, cancel_event=None):
            yield {"type": "tool_call", "name": "read_file", "arguments": {"path": "a.py"}}
            yield {"type": "done", "text": "ok"}

    def from_config(cfg):
        seen.update(cfg)
        return FakeProvider()

    monkeypatch.setattr(local_llm.LocalLLMProvider, "from_config", staticmethod(from_config))
    provider = base.ConfiguredProvider({"local_provider": "google"}, "google/gemma-4-26b-a4b-it-maas")

    async def run():
        return [e async for e in provider.stream([{"role": "user", "content": "hi"}], tools=[])]

    events = asyncio.run(run())
    assert seen["custom_model"] == "google/gemma-4-26b-a4b-it-maas"
    assert seen["custom_endpoint"].endswith("/locations/global/endpoints/openapi")
    assert [type(e).__name__ for e in events] == ["LLMToolCall", "LLMDone"]
    assert events[-1].success


def test_gemma_without_credentials_says_how_to_sign_in(monkeypatch):
    monkeypatch.setattr(base, "vertex_openai_endpoint", lambda config: {})
    provider = base.ConfiguredProvider({}, "google/gemma-4-26b-a4b-it-maas")

    async def run():
        return [e async for e in provider.stream([{"role": "user", "content": "hi"}], tools=[])]

    done = asyncio.run(run())[-1]
    assert not done.success and "application-default login" in done.error


def test_native_vertex_defaults_to_the_global_endpoint(monkeypatch):
    from aria_code.apps.cli.providers.vertexai_stream import VertexAIProvider

    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    made = {}
    genai = pytest.importorskip("google.genai")
    monkeypatch.setattr(genai, "Client", lambda **kw: made.update(kw) or object())
    VertexAIProvider(model="gemini-3-flash-preview", config={})._get_client()
    assert made["vertexai"] is True and made["location"] == "global"


def test_model_command_accepts_google_models_with_cloud_credentials(monkeypatch):
    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli
    from rich.console import Console

    monkeypatch.setattr(base, "google_cloud_available", lambda: True)
    terminal = cli.ArtheraTerminal(dict(cli.DEFAULT_CONFIG))
    monkeypatch.setattr(terminal.commands.context, "console", Console(file=io.StringIO(), color_system=None))
    asyncio.run(terminal.commands.cmd_model("google/gemini-3.8-flash"))
    assert terminal.config["model"] == "gemini-3.8-flash" and terminal.config["local_provider"] == "google"


def test_model_command_without_credentials_names_both_ways(monkeypatch):
    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli
    from rich.console import Console

    monkeypatch.setattr(base, "google_cloud_available", lambda: False)
    terminal = cli.ArtheraTerminal(dict(cli.DEFAULT_CONFIG, ui_lang="en"))
    console = Console(file=io.StringIO(), color_system=None, width=200)
    monkeypatch.setattr(terminal.commands.context, "console", console)
    import aria_code.apps.cli.commands.model_cmds as model_cmds
    monkeypatch.setattr(model_cmds, "_get_provider_key", lambda provider: "", raising=False)
    before = terminal.config["model"]
    asyncio.run(terminal.commands.cmd_model("google/gemini-3.8-flash"))
    assert terminal.config["model"] == before
    text = console.file.getvalue()
    assert "application-default login" in text and "/apikey set google" in text
