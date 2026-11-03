import pytest

import apps.cli.commands.model_cmds as model_cmds


class _Terminal:
    def __init__(self):
        self.config = {
            "backend_chat": True,
            "local_provider": "ollama",
            "model": "old-model",
        }
        self._actual_model = "old-model"


class _Commands(model_cmds.ModelCommandsMixin):
    def __init__(self):
        self.terminal = _Terminal()
        from apps.cli.context import AriaContext
        self.context = AriaContext(console=None, config=self.terminal.config, save_config_cb=lambda c: None, load_config_cb=lambda: {})



def _install_model_globals(monkeypatch):
    from aria_code.providers import ollama_models
    monkeypatch.setattr(ollama_models, "resolve_model_sync", lambda url, model: model)
    async def installed(url, model):
        return model
    monkeypatch.setattr(ollama_models, "resolve_model_async", installed)
    monkeypatch.setattr(
        model_cmds,
        "MODELS",
        {"local": {"id": "qwen-test:7b", "name": "Qwen", "version": "7B", "tag": "Local"}},
        raising=False,
    )
    monkeypatch.setattr(model_cmds, "save_config", lambda config: None, raising=False)
    monkeypatch.setattr(model_cmds, "HAS_RICH", False, raising=False)


def test_model_id_selection_clears_stale_backend_override(monkeypatch):
    _install_model_globals(monkeypatch)
    commands = _Commands()

    commands._set_model_by_id("qwen-test:7b", provider="ollama")

    assert commands.terminal.config["local_provider"] == "ollama"
    assert commands.terminal.config["local_mode"] is True
    assert commands.terminal.config["backend_chat"] is False


@pytest.mark.asyncio
async def test_provider_model_selection_clears_stale_backend_override(monkeypatch):
    _install_model_globals(monkeypatch)
    commands = _Commands()

    await commands.cmd_model("ollama/qwen-test:7b")

    assert commands.terminal.config["model"] == "qwen-test:7b"
    assert commands.terminal.config["backend_chat"] is False
