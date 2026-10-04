"""Exercise real provider adapters, including the legacy CLI import root."""
import importlib
import pytest

from aria_code.apps.cli.providers.llm import sse_stream


@pytest.mark.parametrize("root", ["apps.cli", "aria_code.apps.cli"])
@pytest.mark.parametrize("streamed", [True, False])
async def test_backend_answer_survives_real_adapter_and_sdk(monkeypatch, root, streamed):
    answer = "你好，这是云端模型的真实回答。"

    async def backend(*args, on_token=None, **kwargs):
        if streamed and on_token:
            on_token(answer)
        return {"success": True, "response": answer, "usage": {"completion_tokens": 7}}

    monkeypatch.setattr(sse_stream, "stream_chat", backend)
    make_provider_fn = importlib.import_module(root + ".providers.runtime_bridge").make_provider_fn
    provider = make_provider_fn(
        model="gemini-3.8-flash", config={"backend_chat": True},
        api_url="https://api.arthera.finance", ollama_url="http://unused",
        tool_schemas=[],
    )
    tokens = []
    result = await provider("你好", [], on_token=tokens.append)
    assert result["success"]
    assert result["response"] == answer
    assert result["provider"] == "aria_sse"
    assert result["usage"]["completion_tokens"] == 7
    assert tokens == ([answer] if streamed else [])
