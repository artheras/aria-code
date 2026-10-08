from aria_code.apps.cli.providers.llm.sse_stream import build_chat_payload
import json
import pytest


@pytest.mark.parametrize("use_react", [False, True])
def test_local_tool_contract_is_explicit_and_legacy_is_unchanged(use_react):
    kwargs = dict(model="gemini", thinking_mode="auto", user_context=None,
                  project_context="project rules", use_react_gateway=use_react)
    _, legacy = build_chat_payload("fix", [], **kwargs)
    assert "tools" not in legacy and "tool_execution" not in legacy
    tools = [{"name": "read_file", "parameters": {"type": "object"}}]
    _, local = build_chat_payload("fix", [], **kwargs, tool_schemas=tools, local_tool_execution=True)
    assert local["tools"] == tools
    assert local["tool_protocol"] == "aria-local-v1"
    assert local["tool_execution"] == "client"


@pytest.mark.asyncio
@pytest.mark.parametrize("events,error", [
    ([{"type": "delta", "text": "claimed done"}], "backend_local_tools_unsupported"),
    ([{"type": "tool_call", "tool": "edit_file", "params": {}}], "backend_local_tools_unsupported"),
    ([{"type": "status", "tool_protocol": "aria-local-v1", "tool_execution": "client"},
      {"type": "tool_result", "tool": "edit_file"}], "backend_executed_client_tool"),
    ([{"type": "status", "tool_protocol": "aria-local-v1", "tool_execution": "client"},
      {"type": "tool_call", "tool": "read_file", "params": {"path": "app.py"}}], None),
])
async def test_local_tools_require_acknowledgement_and_remain_client_executed(monkeypatch, events, error):
    import aiohttp
    from aria_code.apps.cli.providers.llm.sse_stream import stream_chat
    sent = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @property
        def content(self):
            async def chunks():
                for event in events:
                    yield ("data: " + json.dumps(event) + "\n\n").encode()
            return chunks()

    class Session(Response):
        def post(self, url, **kwargs):
            sent.append(kwargs["json"])
            return Response()

    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: Session())
    result = await stream_chat("https://test.invalid", "fix", [], local_tool_execution=True,
                               tool_schemas=[{"name": "read_file"}])
    assert sent[0]["tool_execution"] == "client"
    if error:
        assert result["success"] is False and result["error"] == error
    else:
        assert result["success"] is True
        assert result["tool_calls_pending"] == [{"tool": "read_file", "params": {"path": "app.py"}}]


def test_react_gateway_payload_uses_shared_conversation_contract():
    endpoint, payload = build_chat_payload(
        "Review this repository",
        [{"role": "assistant", "content": "Earlier context"}],
        model="gemini-2.5-flash",
        thinking_mode="medium",
        user_context={"workspace_mode": "code", "locale": "zh-CN"},
        project_context="Follow ARIA.md",
        use_react_gateway=True,
    )

    assert endpoint == "/api/v2/chat/react"
    assert payload["surface"] == "aria_code"
    assert payload["mode"] == "code"
    assert payload["message"]["content"] == [{"type": "text", "text": "Review this repository"}]
    assert payload["model"] == {"id": "gemini-2.5-flash", "effort": "medium"}
    assert payload["context"]["project_context"] == "Follow ARIA.md"
    assert "workspace_mode" not in payload["context"]


def test_legacy_gateway_payload_remains_compatible():
    endpoint, payload = build_chat_payload(
        "hello", [], model="qwen", thinking_mode="auto", user_context=None,
        project_context="", use_react_gateway=False,
    )

    assert endpoint == "/api/v2/ai/chat/stream"
    assert payload["message"] == "hello"
    assert payload["stream"] is True
