import asyncio
import inspect
import unittest

import aria_code.apps.cli.providers.runtime_bridge as rb
from aria_code.apps.cli.providers.runtime_bridge import build_tool_executor, make_provider_fn


def test_run_chat_via_runtime_streams_tokens_and_returns_text(monkeypatch):
    """Verify the run_agent driver consumes token events and accumulates text,
    independent of a real LLM (the live REPL path differs only in the provider)."""
    async def fake_provider_fn(message, history, *, on_token=None, **kwargs):
        for tok in ("Hello", " ", "world"):
            if on_token:
                on_token(tok)
        return {"success": True, "response": "Hello world", "provider": "fake"}

    monkeypatch.setattr(rb, "make_provider_fn", lambda **kw: fake_provider_fn)

    seen: list = []
    text = asyncio.run(rb.run_chat_via_runtime(
        prompt="hi", history=[], local_tools={}, tool_schemas=[],
        model="m", config={}, api_url=None, ollama_url="http://x",
        on_token=seen.append, max_rounds=2,
    ))
    # The driver returns the turn's text (streamed tokens, or the result's
    # authoritative final_text when the provider returns a whole response).
    assert text == "Hello world"


def test_run_chat_via_runtime_can_return_gateway_metadata(monkeypatch):
    async def fake_provider_fn(message, history, *, on_token=None, **kwargs):
        if on_token:
            on_token("answer")
        return {
            "success": True,
            "response": "answer",
            "provider": "ollama",
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }

    monkeypatch.setattr(rb, "make_provider_fn", lambda **kw: fake_provider_fn)

    result = asyncio.run(rb.run_chat_via_runtime(
        prompt="hi", history=[], local_tools={}, tool_schemas=[],
        model="m", config={}, api_url=None, ollama_url="http://x",
        return_result=True,
    ))

    assert result.text == "answer"
    assert result.final.provider == "ollama"
    assert result.final.metadata.prompt_tokens == 2


def test_run_chat_via_runtime_executes_a_tool_then_finishes(monkeypatch):
    """The bridge must run a requested tool through ToolExecutor and continue —
    the agentic path every send_message turn now runs through."""
    calls = {"n": 0}

    async def fake_provider_fn(message, history, *, on_token=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"success": True, "response": "", "provider": "fake",
                    "tool_calls_pending": [{"tool": "ping", "params": {"x": 7}}]}
        return {"success": True, "response": "done after tool", "provider": "fake"}

    monkeypatch.setattr(rb, "make_provider_fn", lambda **kw: fake_provider_fn)

    ran: dict = {}

    def ping(params):
        ran.update(params)
        return {"success": True, "pong": params.get("x")}

    seen_calls: list = []
    seen_results: list = []
    text = asyncio.run(rb.run_chat_via_runtime(
        prompt="Call ping with x=7", history=[], local_tools={"ping": (ping, "Ping")}, tool_schemas=[],
        model="m", config={}, api_url=None, ollama_url="http://x",
        on_tool_call=lambda t, p: seen_calls.append(t),
        on_tool_result=lambda t, r: seen_results.append(t),
        max_rounds=3,
    ))
    assert text == "done after tool"          # finished after the tool round
    assert ran.get("x") == 7                   # the tool actually executed
    assert "ping" in seen_calls and "ping" in seen_results  # both events surfaced


def test_runtime_tool_round_preserves_original_prompt_and_tool_context(monkeypatch):
    histories = []

    async def fake_provider_fn(message, history, *, on_token=None, **kwargs):
        histories.append((message, list(history)))
        if len(histories) == 1:
            return {
                "success": True,
                "response": "",
                "provider": "fake",
                "tool_calls_pending": [{"tool": "ping", "params": {}}],
            }
        return {"success": True, "response": "done", "provider": "fake"}

    monkeypatch.setattr(rb, "make_provider_fn", lambda **kw: fake_provider_fn)

    asyncio.run(rb.run_chat_via_runtime(
        prompt="original request",
        history=[{"role": "assistant", "content": "earlier"}],
        local_tools={"ping": (lambda _params: {"success": True}, "Ping")},
        tool_schemas=[], model="m", config={}, api_url=None,
        ollama_url="http://x", max_rounds=3,
    ))

    second_message, second_history = histories[1]
    assert second_message.startswith("## Tool Results")
    assert {"role": "user", "content": "original request"} in second_history
    assert any(item.get("role") == "assistant" and item.get("tool_calls") for item in second_history)
    assert any(item.get("role") == "tool" and item.get("name") == "ping" for item in second_history)


class RuntimeBridgeTests(unittest.TestCase):
    def test_tool_executor_dispatches_local_tool(self):
        registry = {"echo": (lambda p: {"success": True, "echo": p.get("x")}, {})}
        ex = build_tool_executor(registry)
        self.assertEqual(ex.execute_local("echo", {"x": 7}), {"success": True, "echo": 7})

    def test_tool_executor_unknown_tool_is_graceful(self):
        ex = build_tool_executor({})
        result = ex.execute_local("nope", {})
        self.assertFalse(result["success"])

    def test_make_provider_fn_returns_coroutine_fn(self):
        pf = make_provider_fn(model="m", config={}, api_url=None,
                              ollama_url="http://x", tool_schemas=[])
        self.assertTrue(inspect.iscoroutinefunction(pf))


if __name__ == "__main__":
    unittest.main()
