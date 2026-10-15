from __future__ import annotations

import json

import httpx
import pytest

from aria_code.clients.platform_agent_runtime import PlatformAgentRuntimeClient


@pytest.mark.asyncio
async def test_platform_agent_runtime_client_uses_v3_and_unwraps_envelope():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v3/agent-runs" and request.method == "POST":
            return httpx.Response(201, json={"success": True, "data": {
                "id": "run_1", "status": "queued", "title": "Check build",
            }})
        if request.url.path == "/api/v3/agent-runs/run_1/events":
            return httpx.Response(201, json={"success": True, "data": {"id": "event_1"}})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PlatformAgentRuntimeClient(
            "https://api.arthera.test/",
            access_token="firebase-token",
            client=http_client,
        )
        run = await client.create_run(title="Check build", mode="code")
        await client.append_event(run.id, "status", {"phase": "planning"})

    assert run.id == "run_1"
    assert json.loads(requests[0].content)["workspace_id"] == "unscoped"
    assert requests[0].headers["authorization"] == "Bearer firebase-token"
    assert requests[1].url.path == "/api/v3/agent-runs/run_1/events"
