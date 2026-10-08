import asyncio
from dataclasses import asdict

import pytest

from aria_code.apps.cli import model_probe
from aria_code.apps.cli.providers import base


def fake_provider(monkeypatch, rounds, *, route="ConfiguredProvider"):
    requests = []
    class Fake:
        def __init__(self, *args, **kwargs):
            pass
        async def stream(self, messages, tools, **kwargs):
            requests.append((messages, tools))
            for event in rounds[len(requests)-1]:
                yield event
    monkeypatch.setattr(base, route, Fake)
    return requests


@pytest.mark.asyncio
async def test_visible_text_can_arrive_only_in_final_event(monkeypatch):
    requests = fake_provider(monkeypatch, [[base.LLMDone("READY", provider="vertexai")]])
    cfg = {"model": "google/gemini-2.5-flash", "auth_token": "secret"}
    snapshot = dict(cfg)
    report = await model_probe.probe_model(cfg)
    assert report.success and report.text_verified
    assert not report.tools_verified
    assert report.provider == "vertexai"
    assert requests[0][1] == []
    assert cfg == snapshot
    assert "secret" not in str(asdict(report))


@pytest.mark.asyncio
async def test_empty_backend_does_not_fall_back_to_another_provider(monkeypatch):
    requests = fake_provider(monkeypatch, [[base.LLMDone("", provider="backend")]], route="AriaSSEProvider")
    monkeypatch.setattr(base, "OllamaProvider", lambda *a, **kw: pytest.fail("must not fall back"))
    report = await model_probe.probe_model({"model": "google/gemini-2.5-flash", "backend_chat": True}, "https://example.test")
    assert not report.success and not report.text_verified
    assert report.route == "cloud"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_diagnostic_tool_round_trip_without_real_tool_execution(monkeypatch):
    requests = fake_provider(monkeypatch, [
        [base.LLMToken("READY"), base.LLMDone("READY")],
        [base.LLMToolCall("aria_probe_ping", {"echo": "READY"}), base.LLMDone("")],
        [base.LLMDone("READY")],
    ])
    report = await model_probe.probe_model({"model": "google/gemini-2.5-flash"}, tools=True)
    assert report.success and report.tools_verified
    assert report.first_token_ms is not None
    assert [tool["function"]["name"] for tool in requests[1][1]] == ["aria_probe_ping"]
    assert requests[2][0][-2]["role"] == "tool"
    assert requests[2][0][-2]["tool_call_id"] == "aria-probe-1"


@pytest.mark.asyncio
async def test_model_ignoring_tools_is_not_reported_as_capable(monkeypatch):
    fake_provider(monkeypatch, [[base.LLMDone("READY")], [base.LLMDone("READY")]])
    report = await model_probe.probe_model({"model": "google/gemini-2.5-flash"}, tools=True)
    assert report.text_verified and not report.success and not report.tools_verified
    assert report.category == "tool_protocol"


@pytest.mark.asyncio
async def test_failure_report_hides_raw_provider_error(monkeypatch):
    fake_provider(monkeypatch, [[base.LLMDone("", success=False, error="403 API key invalid for generateContent token=secret")]])
    report = await model_probe.probe_model({"model": "google/gemini-2.5-flash"})
    assert report.category == "auth"
    assert "secret" not in str(report.to_dict())


@pytest.mark.asyncio
async def test_total_probe_timeout_cancels_the_transport(monkeypatch):
    cancelled = []
    class Slow:
        def __init__(self, *args, **kwargs):
            pass
        async def stream(self, *args, **kwargs):
            try:
                await asyncio.sleep(60)
                yield base.LLMDone("unexpected")
            finally:
                cancelled.append(True)
    monkeypatch.setattr(base, "ConfiguredProvider", Slow)
    report = await model_probe.probe_model({"model": "google/gemini-2.5-flash"}, timeout=0.01)
    assert report.category == "timeout" and not report.success
    assert cancelled == [True]
