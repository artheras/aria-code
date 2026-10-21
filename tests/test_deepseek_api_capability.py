"""DeepSeek's own API models must be recognised, or every agent turn on them runs without tools."""

import pytest

from aria_code.model_capability import get_model_capability, is_unknown_model


@pytest.mark.parametrize("model", [
    "deepseek/deepseek-v4-pro", "deepseek-v4-pro", "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-flash",
])
def test_deepseek_api_models_can_call_tools(model):
    capability = get_model_capability(model)
    assert not is_unknown_model(capability)
    assert capability.tool_calls and capability.context_window >= 8192


def test_the_local_reasoning_models_are_unchanged():
    assert get_model_capability("deepseek-r1:14b").tool_calls is False
    assert get_model_capability("deepseek-v3.1").format == "ollama_native"


def test_a_workspace_task_on_deepseek_is_no_longer_refused():
    from aria_code.apps.cli.workspace_route import route_has_tools

    assert route_has_tools("deepseek/deepseek-v4-pro", {"provider": "deepseek"}, None)
