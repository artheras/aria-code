"""Arthera Agent Runtime v3 control-plane client.

This bridge gives the local Aria Code runtime a durable cross-device run record
without moving shell access, workspace files, or model credentials into the
platform API.  The API is a control plane; this process remains the sandboxed
worker and may continue operating from its local RunStore when offline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

import httpx

from aria_code.runtime.gateway import TurnResult, run_turn


class PlatformAgentRuntimeError(RuntimeError):
    """The remote control plane rejected or could not accept a lifecycle write."""


@dataclass(frozen=True)
class PlatformRun:
    id: str
    status: str
    raw: Mapping[str, Any]


class PlatformAgentRuntimeClient:
    """Small authenticated client for the versioned Arthera run control plane."""

    def __init__(
        self,
        base_url: str,
        *,
        access_token: str | None = None,
        api_key: str | None = None,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._api_key = api_key
        self._timeout = timeout
        self._client = client

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.request(method, f"{self._base_url}{path}", headers=self._headers(), **kwargs)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or not body.get("success", False):
                raise PlatformAgentRuntimeError("Arthera Agent Runtime returned an invalid response.")
            data = body.get("data")
            if not isinstance(data, dict):
                raise PlatformAgentRuntimeError("Arthera Agent Runtime response has no data object.")
            return data
        except httpx.HTTPError as exc:
            raise PlatformAgentRuntimeError(f"Arthera Agent Runtime request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def create_run(
        self,
        *,
        title: str,
        workspace_id: str = "unscoped",
        mode: str = "code",
        request: Mapping[str, Any] | None = None,
        client_run_id: str | None = None,
    ) -> PlatformRun:
        data = await self._request("POST", "/api/v3/agent-runs", json={
            "title": title,
            "workspace_id": workspace_id,
            "mode": mode,
            "request": dict(request or {}),
            "client_run_id": client_run_id,
        })
        return PlatformRun(id=str(data["id"]), status=str(data["status"]), raw=data)

    async def update_run(self, run_id: str, **update: Any) -> PlatformRun:
        data = await self._request("PATCH", f"/api/v3/agent-runs/{run_id}", json=update)
        return PlatformRun(id=str(data["id"]), status=str(data["status"]), raw=data)

    async def append_event(self, run_id: str, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        await self._request("POST", f"/api/v3/agent-runs/{run_id}/events", json={
            "type": event_type,
            "payload": dict(payload or {}),
        })


async def run_turn_with_platform_record(
    client: PlatformAgentRuntimeClient,
    *,
    title: str,
    workspace_id: str,
    mode: str,
    request: Mapping[str, Any] | None,
    client_run_id: str | None,
    prompt: str,
    history: list,
    provider_fn: Callable[..., Awaitable[Any]],
    tool_executor: Any,
    tool_schemas: list[dict[str, Any]] | None = None,
    on_token: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_call: Callable[[str, dict], None] | None = None,
    on_tool_result: Callable[[str, dict], None] | None = None,
    **run_turn_options: Any,
) -> tuple[PlatformRun, TurnResult]:
    """Execute the shared gateway while mirroring lifecycle events to Arthera.

    Event delivery is ordered by a single queue.  A telemetry outage never
    terminates a local task, but terminal state is retried once before return so
    clients cannot remain in a phantom `Thinking…` state after a completed turn.
    """
    remote_run = await client.create_run(
        title=title,
        workspace_id=workspace_id,
        mode=mode,
        request=request,
        client_run_id=client_run_id,
    )
    await client.update_run(remote_run.id, status="planning")
    await client.update_run(remote_run.id, status="running")

    events: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
    delivery_errors: list[Exception] = []

    async def deliver_events() -> None:
        while True:
            item = await events.get()
            if item is None:
                return
            event_type, payload = item
            try:
                await client.append_event(remote_run.id, event_type, payload)
            except PlatformAgentRuntimeError as exc:
                # Keep the local runtime authoritative for execution. A later
                # checkpoint/status write makes the remote state recoverable.
                delivery_errors.append(exc)

    sender = asyncio.create_task(deliver_events())

    def emit(event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        events.put_nowait((event_type, dict(payload or {})))

    def emit_token(token: str) -> None:
        emit("turn_delta", {"text": token})
        if on_token is not None:
            on_token(token)

    def emit_thinking(text: str) -> None:
        emit("thinking", {"text": text})
        if on_thinking is not None:
            on_thinking(text)

    def emit_tool_call(tool: str, params: dict) -> None:
        emit("tool_call", {"tool": tool, "params": params})
        if on_tool_call is not None:
            on_tool_call(tool, params)

    def emit_tool_result(tool: str, output: dict) -> None:
        emit("tool_result", {"tool": tool, "result": output})
        if on_tool_result is not None:
            on_tool_result(tool, output)

    try:
        result = await run_turn(
            prompt,
            history,
            provider_fn=provider_fn,
            tool_executor=tool_executor,
            tool_schemas=tool_schemas,
            on_token=emit_token,
            on_thinking=emit_thinking,
            on_tool_call=emit_tool_call,
            on_tool_result=emit_tool_result,
            on_status=lambda phase, message: emit("status", {"phase": phase, "message": message}),
            **run_turn_options,
        )
    except Exception as exc:
        emit("error", {"message": str(exc)})
        await events.put(None)
        await sender
        await client.update_run(remote_run.id, status="failed", error=str(exc))
        raise

    emit("turn_complete", {
        "success": result.ok,
        "cancelled": result.cancelled,
        "error": result.error,
    })
    await events.put(None)
    await sender

    if result.cancelled:
        final_status, error = "cancelled", None
    elif result.error:
        final_status, error = "failed", result.error
    else:
        final_status, error = "succeeded", None
    remote_run = await client.update_run(
        remote_run.id,
        status=final_status,
        result={"text": result.text, "delivery_degraded": bool(delivery_errors)},
        error=error,
    )
    return remote_run, result
