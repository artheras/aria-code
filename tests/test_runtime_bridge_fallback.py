"""Unit tests for runtime_bridge.run_with_fallback (P0 runtime-cutover parity).

These cover the cloud→Ollama fallback DECISIONS the runtime path needs to match
``send_message``'s inline loop, using injected fake provider runners — no real
providers, no network. Async tests run under pytest-asyncio (asyncio_mode=auto).
"""

from aria_code.apps.cli.providers.runtime_bridge import run_with_fallback


def _ok(text: str) -> dict:
    return {"success": True, "response": text, "cancelled": False}


def _make_runner(result: dict, *, stream: str = ""):
    """Build an async (on_token)->result runner that records its calls."""
    calls = {"n": 0}

    async def _runner(on_token):
        calls["n"] += 1
        for ch in stream:
            if on_token is not None:
                on_token(ch)
        return result

    return _runner, calls


async def test_skip_route_goes_straight_to_ollama():
    cloud, c = _make_runner(_ok("cloud"))
    ollama, o = _make_runner(_ok("ollama"))
    res = await run_with_fallback("skip", run_cloud=cloud, run_ollama=ollama)
    assert res["response"] == "ollama"
    assert c["n"] == 0 and o["n"] == 1  # cloud never called


async def test_ollama_route_never_calls_cloud():
    cloud, c = _make_runner(_ok("cloud"))
    ollama, o = _make_runner(_ok("ollama"))
    res = await run_with_fallback("ollama", run_cloud=cloud, run_ollama=ollama)
    assert res["response"] == "ollama"
    assert c["n"] == 0 and o["n"] == 1


async def test_cloud_success_no_fallback():
    answer = "a real, substantive cloud answer that actually streamed tokens"
    cloud, c = _make_runner(_ok(answer), stream=answer)  # well-streamed → not placeholder
    ollama, o = _make_runner(_ok("ollama"))
    res = await run_with_fallback("cloud", run_cloud=cloud, run_ollama=ollama)
    assert res["response"] == answer
    assert c["n"] == 1 and o["n"] == 0  # kept cloud, no fallback


async def test_cloud_failure_falls_back_to_ollama():
    cloud, c = _make_runner({"success": False, "response": "", "cancelled": False})
    ollama, o = _make_runner(_ok("ollama recovered"))
    res = await run_with_fallback("cloud", run_cloud=cloud, run_ollama=ollama)
    assert res["response"] == "ollama recovered"
    assert c["n"] == 1 and o["n"] == 1


async def test_cloud_short_answer_is_kept():
    # A brief answer is still a successful model answer.
    cloud, c = _make_runner(_ok("ok"))
    ollama, o = _make_runner(_ok("ollama produced the real, full answer here"))
    res = await run_with_fallback("cloud", run_cloud=cloud, run_ollama=ollama)
    assert res["response"] == "ok"
    assert c["n"] == 1 and o["n"] == 0


async def test_cloud_final_event_answer_is_kept():
    # Some gateways return an entire valid answer in a final SSE event.
    stub = "This is a long canned backend help message that was not streamed. " * 3
    cloud, c = _make_runner(_ok(stub), stream="")  # 0 tokens streamed
    ollama, o = _make_runner(_ok("ollama real generation here"))
    res = await run_with_fallback("cloud", run_cloud=cloud, run_ollama=ollama)
    assert res["response"] == stub
    assert c["n"] == 1 and o["n"] == 0


async def test_cloud_cancelled_does_not_fall_back():
    cloud, c = _make_runner({"success": False, "response": "partial", "cancelled": True})
    ollama, o = _make_runner(_ok("ollama"))
    res = await run_with_fallback("cloud", run_cloud=cloud, run_ollama=ollama)
    assert res["cancelled"] is True
    assert o["n"] == 0  # user cancel is honoured, never silently re-run


async def test_on_token_forwarded_for_cloud_success():
    answer = "streamed cloud answer that is long enough to not be a placeholder"
    cloud, c = _make_runner(_ok(answer), stream=answer)
    ollama, o = _make_runner(_ok("ollama"))
    seen: list[str] = []
    await run_with_fallback(
        "cloud", run_cloud=cloud, run_ollama=ollama, on_token=seen.append
    )
    assert "".join(seen) == answer  # caller's streaming callback still fires


async def test_make_provider_fn_threads_system_override(monkeypatch):
    """system_override reaches Ollama as a ctor arg and cloud via user_context."""
    import aria_code.apps.cli.providers.base as base_mod
    import aria_code.packages.aria_sdk.streaming as streaming_mod

    seen: dict = {}

    class _FakeOllama:
        def __init__(self, url, model, system_override=None, **kw):
            seen["ollama_system_override"] = system_override

    class _FakeAriaSSE:
        def __init__(self, api_url, model, *, thinking_mode=None, user_context=None,
                     auth_token=None, project_context=None, **kw):
            seen["cloud_user_context"] = user_context

    async def _fake_stream(provider, prompt, history, **kw):
        return {"success": True, "response": "x" * 50, "cancelled": False}

    monkeypatch.setattr(base_mod, "OllamaProvider", _FakeOllama)
    monkeypatch.setattr(base_mod, "AriaSSEProvider", _FakeAriaSSE)
    monkeypatch.setattr(streaming_mod, "stream_provider_result", _fake_stream)

    from aria_code.apps.cli.providers.runtime_bridge import make_provider_fn

    # local_mode → ollama route → override goes to the provider constructor
    pf_local = make_provider_fn(
        model="qwen2.5:7b", config={"local_mode": True}, api_url=None,
        ollama_url="http://x", tool_schemas=[], system_override="ROLE-X",
    )
    await pf_local("hi", [])
    assert seen["ollama_system_override"] == "ROLE-X"

    # backend_chat explicitly selects Aria SSE → override rides in user_context
    pf_cloud = make_provider_fn(
        model="anthropic/claude", config={"backend_chat": True}, api_url="http://api",
        ollama_url="http://x", tool_schemas=[], system_override="ROLE-Y",
    )
    await pf_cloud("hi", [])
    assert seen["cloud_user_context"]["system_role_override"] == "ROLE-Y"
