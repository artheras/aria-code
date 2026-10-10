"""The relay keeps routing through reconnects, retries and token failures.

Four faults fixed together, before the relay moves to Go behind the same
behaviour (docs/rust-cli-migration.md):

  1. A machine that reconnected stopped receiving messages: the old socket's
     disconnect removed the new registration from routing. So did a refused
     registration under the same client_id.
  2. /feishu/event answered only after the machine did (up to 90 s). Feishu
     waits about 3 s, then retries the event, and nothing de-duplicated it.
  3. A failed tenant-token fetch cached an empty token for two hours.
  4. Any connected client could answer a request sent to another.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import time
from unittest import mock

import pytest

if os.environ.get("GITHUB_ACTIONS"):
    import fastapi  # noqa: F401  — CI must run these, not skip them

fastapi_testclient = pytest.importorskip("fastapi.testclient", reason="relay server dependency")
TestClient = fastapi_testclient.TestClient

TOKEN, CODE = "a" * 43, "ABCDEFGHJKLM"


@pytest.fixture
def relay(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "relay.db"))
    for key in ("FEISHU_ENCRYPT_KEY", "RELAY_ALLOW_UNVERIFIED_EVENTS", "RELAY_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "tok")
    import aria_relay_server
    return importlib.reload(aria_relay_server)


def _connect(stack, client, client_id="aria-a", token=TOKEN):
    ws = stack.enter_context(client.websocket_connect("/ws"))
    ws.send_text(json.dumps({"type": "register", "client_id": client_id, "token": token, "bind_code": CODE}))
    return ws, json.loads(ws.receive_text())


def _eventually(condition, seconds=3.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class TestReconnecting:
    def test_the_old_sockets_disconnect_keeps_the_new_registration(self, relay):
        client = TestClient(relay.app)
        with contextlib.ExitStack() as first, contextlib.ExitStack() as second:
            old, answer = _connect(first, client)
            assert answer["ok"]
            _new, answer = _connect(second, client)
            assert answer["ok"]
            first.close()   # the machine's previous connection finally drops
            # Give the server time to run the old socket's cleanup, then check
            # that it did not take the live registration with it.
            time.sleep(0.3)
            assert "aria-a" in relay._connections

    def test_a_refused_registration_does_not_evict_the_machine(self, relay):
        client = TestClient(relay.app)
        with contextlib.ExitStack() as live, contextlib.ExitStack() as intruder:
            _ws, answer = _connect(live, client)
            assert answer["ok"]
            _ws, answer = _connect(intruder, client, token="b" * 43)
            assert not answer["ok"]
            intruder.close()
            time.sleep(0.3)
            assert "aria-a" in relay._connections

    def test_the_last_socket_closing_does_unregister(self, relay):
        client = TestClient(relay.app)
        with contextlib.ExitStack() as stack:
            _connect(stack, client)
            assert "aria-a" in relay._connections
        assert _eventually(lambda: "aria-a" not in relay._connections)


class TestAnsweringRequests:
    def test_only_the_client_a_request_was_sent_to_may_answer(self, relay):
        async def scenario():
            future = asyncio.get_running_loop().create_future()
            relay._pending_responses["req_1"] = ("aria-a", future)
            assert not relay._resolve_response("aria-b", {"id": "req_1", "result": {"toast": "forged"}})
            assert not future.done()
            assert relay._resolve_response("aria-a", {"id": "req_1", "result": {"ok": True}})
            return await future

        assert asyncio.run(scenario()) == {"ok": True}

    def test_unknown_and_finished_requests_are_ignored(self, relay):
        async def scenario():
            future = asyncio.get_running_loop().create_future()
            future.set_result("done")
            relay._pending_responses["req_2"] = ("aria-a", future)
            return (relay._resolve_response("aria-a", {"id": "req_missing"}),
                    relay._resolve_response("aria-a", {"id": "req_2", "result": "again"}))

        assert asyncio.run(scenario()) == (False, False)


def _message(event_id="ev_1", text="hello"):
    return {"token": "tok", "header": {"event_type": "im.message.receive_v1", "event_id": event_id},
            "event": {"sender": {"sender_id": {"open_id": "ou_alice"}},
                      "message": {"message_id": "om_1", "chat_id": "oc_1", "message_type": "text",
                                  "content": json.dumps({"text": text})}}}


class TestFeishuEvents:
    def test_a_retried_event_reaches_the_machine_once(self, relay):
        delivered = []

        async def deliver(payload):
            delivered.append(payload["header"]["event_id"])

        with mock.patch.object(relay, "_deliver", deliver):
            client = TestClient(relay.app)
            for _ in range(3):
                assert client.post("/feishu/event", json=_message("ev_1")).json() == {"code": 0}
            client.post("/feishu/event", json=_message("ev_2"))
        assert delivered == ["ev_1", "ev_2"]

    def test_an_event_without_an_id_is_not_dropped(self, relay):
        delivered = []

        async def deliver(payload):
            delivered.append(1)

        payload = _message()
        payload["header"].pop("event_id")
        with mock.patch.object(relay, "_deliver", deliver):
            client = TestClient(relay.app)
            client.post("/feishu/event", json=payload)
            client.post("/feishu/event", json=payload)
        assert delivered == [1, 1]

    def test_feishu_gets_its_answer_even_when_handling_fails(self, relay):
        async def deliver(payload):
            raise RuntimeError("Feishu API down")

        with mock.patch.object(relay, "_deliver", deliver):
            response = TestClient(relay.app).post("/feishu/event", json=_message())
        assert response.status_code == 200 and response.json() == {"code": 0}

    def test_the_answer_does_not_wait_for_the_machine(self, relay):
        # The handler schedules the work and returns: a slow machine is not
        # awaited inside the request. (TestClient itself waits for background
        # tasks, so this checks the route's own body.)
        scheduled = []

        class Background:
            def add_task(self, func, *args):
                scheduled.append((func, args))

        from starlette.requests import Request

        body = json.dumps(_message()).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request({"type": "http", "method": "POST", "path": "/feishu/event",
                           "headers": [(b"content-type", b"application/json")]}, receive)
        result = asyncio.run(relay.feishu_event(request, Background()))
        assert result == {"code": 0}
        assert scheduled and scheduled[0][0] is relay._handle_message


class TestTenantToken:
    def _feishu(self, status=200, payload=None):
        response = mock.Mock(status_code=status)
        response.json.return_value = payload if payload is not None else {}
        client = mock.AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = response
        return mock.patch.object(relay_module(), "httpx", mock.Mock(AsyncClient=mock.Mock(return_value=client)))

    def test_a_failed_fetch_is_not_cached(self, relay):
        with self._feishu(payload={"code": 10003, "msg": "invalid app_secret"}):
            with pytest.raises(RuntimeError):
                asyncio.run(relay._get_tenant_token())
        assert relay._feishu_token_cache == {}
        with self._feishu(payload={"code": 0, "tenant_access_token": "t-1", "expire": 7200}):
            assert asyncio.run(relay._get_tenant_token()) == "t-1"

    def test_a_send_without_a_token_is_answered_not_dropped(self, relay):
        relay._remember_forward("aria-a", "om_1", "oc_1")
        with self._feishu(status=500, payload={"code": 99991663}):
            result = asyncio.run(relay._send_for_client(
                "aria-a", {"op": "reply", "target": "om_1", "msg_type": "text", "content": "{}"}))
        assert result == {"code": -1, "msg": "relay could not reach Feishu"}


def relay_module():
    import aria_relay_server
    return aria_relay_server
