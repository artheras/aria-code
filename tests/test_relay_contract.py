"""The relay's contract, black-box: any implementation must pass these.

The relay is moving from Python to Go (docs/rust-cli-migration.md). These
tests start a real relay process, talk to it only over HTTP and WebSocket,
and stand in for Feishu with a local fake, so the same tests hold either
implementation to the same behaviour:

    ARIA_RELAY_COMMAND="go/relay/bin/aria-relay" pytest tests/test_relay_contract.py

Without ARIA_RELAY_COMMAND they run the Python relay (`python -m
aria_code.aria_relay_server`). Each test gets a fresh process and database.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import pytest

if os.environ.get("GITHUB_ACTIONS"):
    import fastapi  # noqa: F401  — CI must run these, not skip them

httpx = pytest.importorskip("httpx")
ws_client = pytest.importorskip("websockets.sync.client", reason="relay client dependency")
pytest.importorskip("fastapi", reason="relay server dependency")

REPO = Path(__file__).resolve().parents[1]
TOKEN_A, TOKEN_B = "a" * 43, "b" * 43
CODE_A = "ABCDEFGHJKLM"
VERIFY = "tok"


# ── a fake Feishu ───────────────────────────────────────────────────────────

class FakeFeishu:
    """Records what the relay sends to Feishu and answers like Feishu does."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — http.server's naming
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                path, _, query = self.path.partition("?")
                with fake.lock:
                    fake.requests.append({"path": path, "query": query, "body": body,
                                          "auth": self.headers.get("Authorization", "")})
                    sent = len(fake.requests)
                if path.endswith("/auth/v3/tenant_access_token/internal"):
                    answer = {"code": 0, "msg": "ok", "tenant_access_token": "t-test", "expire": 7200}
                else:
                    answer = {"code": 0, "msg": "success", "data": {"message_id": f"om_sent_{sent}"}}
                data = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/open-apis"

    def messages(self) -> list[dict]:
        with self.lock:
            return [r for r in self.requests if "/im/v1/messages" in r["path"]]

    def wait_for(self, predicate: Callable[[dict], bool], seconds: float = 5.0) -> dict:
        deadline = time.time() + seconds
        while time.time() < deadline:
            for request in self.messages():
                if predicate(request):
                    return request
            time.sleep(0.05)
        raise AssertionError(f"Feishu never received the expected message; got {self.messages()}")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ── a relay process ────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Relay:
    def __init__(self, tmp_path: Path, feishu: FakeFeishu, env: dict[str, str]) -> None:
        self.port = _free_port()
        self.feishu = feishu
        command = os.environ.get("ARIA_RELAY_COMMAND")
        argv = shlex.split(command) if command else [sys.executable, "-m", "aria_code.aria_relay_server"]
        base_env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("FEISHU_", "RELAY_", "K_SERVICE"))}
        self.env = {
            **base_env,
            "PORT": str(self.port),
            "DB_PATH": str(tmp_path / "relay.db"),
            "FEISHU_APP_ID": "cli_test",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_API_BASE": feishu.base,
            "FEISHU_VERIFICATION_TOKEN": VERIFY,
            "MESSAGE_TIMEOUT": "3",
            "PYTHONPATH": str(REPO / "src"),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            **env,
        }
        self.env = {k: v for k, v in self.env.items() if v is not None}
        self.log = tmp_path / "relay.log"
        self.process = subprocess.Popen(argv, env=self.env, cwd=tmp_path,
                                        stdout=self.log.open("wb"), stderr=subprocess.STDOUT)
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", trust_env=False, timeout=10)
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"relay exited: {self.log.read_text(errors='replace')}")
            try:
                if self.http.get("/status").status_code == 200:
                    return
            except httpx.TransportError:
                time.sleep(0.1)
        raise AssertionError(f"relay did not start: {self.log.read_text(errors='replace')}")

    def connect(self, client_id="aria-a", token=TOKEN_A, code=CODE_A, **extra):
        ws = ws_client.connect(f"ws://127.0.0.1:{self.port}/ws", proxy=None, open_timeout=10)
        ws.send(json.dumps({"type": "register", "client_id": client_id, "token": token,
                            "bind_code": code, **extra}))
        return ws, json.loads(ws.recv(timeout=10))

    def event(self, payload: dict, **kw) -> httpx.Response:
        return self.http.post("/feishu/event", json=payload, **kw)

    def close(self) -> None:
        self.http.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


@pytest.fixture
def feishu():
    fake = FakeFeishu()
    yield fake
    fake.close()


@pytest.fixture
def start(tmp_path, feishu):
    relays: list[Relay] = []

    def _start(**env) -> Relay:
        relay = Relay(tmp_path, feishu, env)
        relays.append(relay)
        return relay

    yield _start
    for relay in relays:
        relay.close()


@pytest.fixture
def relay(start) -> Relay:
    return start()


def _message(open_id="ou_alice", text="hello", event_id="ev_1", message_id="om_1", chat_id="oc_1"):
    return {"token": VERIFY, "header": {"event_type": "im.message.receive_v1", "event_id": event_id},
            "event": {"sender": {"sender_id": {"open_id": open_id}},
                      "message": {"message_id": message_id, "chat_id": chat_id, "message_type": "text",
                                  "content": json.dumps({"text": text})}}}


def _press(open_message_id: str, event_id="ev_press") -> dict:
    return {"token": VERIFY, "header": {"event_type": "card.action.trigger", "event_id": event_id},
            "event": {"operator": {"open_id": "ou_alice"}, "action": {"value": {"approve": True}},
                      "context": {"open_message_id": open_message_id}}}


def _frame(ws, kind: str, seconds: float = 5.0) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        frame = json.loads(ws.recv(timeout=max(0.1, deadline - time.time())))
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"no {kind} frame")


def _said(request: dict) -> str:
    """Everything a message to Feishu says, with its JSON-in-a-string content decoded."""
    body = request["body"]
    text = json.dumps(body, ensure_ascii=False)
    try:
        text += json.dumps(json.loads(body.get("content") or "{}"), ensure_ascii=False)
    except (TypeError, ValueError):
        pass
    return text


def _bind(relay: Relay, open_id="ou_alice") -> None:
    relay.event(_message(open_id, f"/bind ARIA-BIND-{CODE_A}", event_id=f"ev_bind_{open_id}"))
    relay.feishu.wait_for(lambda r: "绑定成功" in _said(r))


# ── status ─────────────────────────────────────────────────────────────────

class TestStatus:
    def test_reports_the_relay_state(self, relay):
        status = relay.http.get("/status").json()
        assert status["connected_clients"] == 0 and status["total_bindings"] == 0
        assert status["store"] == "sqlite" and status["feishu_app_configured"] is True
        assert status["event_verification"] == "verification_token"
        assert "client_ids" not in status

    def test_lists_clients_only_with_the_deployment_secret(self, start):
        relay = start(RELAY_SECRET="deploy-secret")
        ws, _ = relay.connect(secret="deploy-secret")
        with ws:
            assert "client_ids" not in relay.http.get("/status").json()
            listed = relay.http.get("/status", headers={"X-Relay-Secret": "deploy-secret"}).json()
            assert listed["client_ids"] == ["aria-a"] and listed["connected_clients"] == 1


# ── event authenticity ─────────────────────────────────────────────────────

class TestVerification:
    def test_malformed_json_is_rejected_before_verification(self, relay):
        response = relay.http.post("/feishu/event", content=b"{not json",
                                   headers={"Content-Type": "application/json"})
        assert response.status_code == 400

    def test_a_wrong_token_is_rejected(self, relay):
        assert relay.event({"token": "nope", "challenge": "c"}).status_code == 401

    def test_the_url_challenge_is_echoed_once_verified(self, relay):
        assert relay.event({"token": VERIFY, "challenge": "c-123"}).json() == {"challenge": "c-123"}

    def test_without_any_key_every_event_is_rejected(self, start):
        relay = start(FEISHU_VERIFICATION_TOKEN=None)
        assert relay.event({"challenge": "c"}).status_code == 401
        assert relay.http.get("/status").json()["event_verification"].startswith("enforced")

    def test_signature_mode(self, start):
        relay = start(FEISHU_ENCRYPT_KEY="ek", FEISHU_VERIFICATION_TOKEN=None)
        body = json.dumps({"challenge": "c-sig"}).encode()
        signature = hashlib.sha256(b"1700000000" + b"nonce" + b"ek" + body).hexdigest()
        headers = {"Content-Type": "application/json", "X-Lark-Request-Timestamp": "1700000000",
                   "X-Lark-Request-Nonce": "nonce"}
        good = relay.http.post("/feishu/event", content=body, headers={**headers, "X-Lark-Signature": signature})
        bad = relay.http.post("/feishu/event", content=body, headers={**headers, "X-Lark-Signature": "0" * 64})
        assert good.json() == {"challenge": "c-sig"} and bad.status_code == 401


# ── registration ───────────────────────────────────────────────────────────

class TestRegistration:
    def test_the_first_message_must_be_register(self, relay):
        with ws_client.connect(f"ws://127.0.0.1:{relay.port}/ws", proxy=None) as ws:
            ws.send(json.dumps({"type": "hello"}))
            assert json.loads(ws.recv(timeout=5)) == {"ok": False, "reason": "first message must be register"}

    def test_a_machine_registers_and_reconnects(self, relay):
        ws, answer = relay.connect()
        ws.close()
        assert answer == {"ok": True, "client_id": "aria-a"}
        ws, answer = relay.connect()
        ws.close()
        assert answer["ok"]

    def test_another_installation_cannot_take_the_client_id(self, relay):
        relay.connect()[0].close()
        ws, answer = relay.connect(token=TOKEN_B)
        ws.close()
        assert not answer["ok"] and "another installation" in answer["reason"]

    def test_an_old_client_is_told_to_upgrade(self, relay):
        ws, answer = relay.connect(token="short", code="")
        ws.close()
        assert not answer["ok"] and "upgrade" in answer["reason"]

    def test_the_deployment_secret_gates_registration(self, start):
        relay = start(RELAY_SECRET="deploy-secret")
        ws, answer = relay.connect(secret="wrong")
        ws.close()
        assert answer == {"ok": False, "reason": "invalid secret"}


# ── binding and forwarding ─────────────────────────────────────────────────

class TestMessages:
    def test_binding_with_the_machines_code(self, relay):
        ws, _ = relay.connect()
        with ws:
            _bind(relay)
            assert relay.http.get("/status").json()["total_bindings"] == 1

    def test_an_unknown_bind_code_is_refused(self, relay):
        relay.event(_message(text="/bind ARIA-BIND-ZZZZZZZZZZZZ"))
        relay.feishu.wait_for(lambda r: "绑定码无效" in _said(r))
        assert relay.http.get("/status").json()["total_bindings"] == 0

    def test_a_message_reaches_the_bound_machine_and_feishu_is_answered_at_once(self, relay):
        ws, _ = relay.connect()
        with ws:
            _bind(relay)
            started = time.time()
            response = relay.event(_message(text="what is my balance", event_id="ev_msg"))
            assert response.json() == {"code": 0}
            assert time.time() - started < 2, "Feishu waits about 3 s; the answer must not wait for the machine"
            frame = _frame(ws, "message")
            assert frame["id"].startswith("req_") and frame["payload"]["header"]["event_id"] == "ev_msg"

    def test_a_retried_event_reaches_the_machine_once(self, relay):
        ws, _ = relay.connect()
        with ws:
            _bind(relay)
            for _ in range(3):
                relay.event(_message(event_id="ev_retry"))
            first = _frame(ws, "message")
            ws.send(json.dumps({"type": "response", "id": first["id"], "result": {}}))
            with pytest.raises(TimeoutError):
                ws.recv(timeout=1.5)

    def test_an_unbound_user_is_told_how_to_start(self, relay):
        relay.event(_message(open_id="ou_new"))
        greeting = relay.feishu.wait_for(lambda r: r["body"].get("receive_id") == "ou_new")
        assert "receive_id_type=open_id" in greeting["query"] and greeting["auth"] == "Bearer t-test"

    def test_a_bound_user_whose_machine_is_offline_is_told_so(self, relay):
        ws, _ = relay.connect()
        with ws:
            _bind(relay)
        relay.event(_message(event_id="ev_offline", message_id="om_offline"))
        notice = relay.feishu.wait_for(lambda r: r["path"].endswith("/om_offline/reply"))
        assert "未连接" in _said(notice)

    def test_the_old_sockets_disconnect_keeps_the_reconnected_machine_routed(self, relay):
        old, _ = relay.connect()
        _bind(relay)
        new, answer = relay.connect()
        assert answer["ok"]
        old.close()
        time.sleep(0.5)
        with new:
            relay.event(_message(event_id="ev_after_reconnect"))
            assert _frame(new, "message")["payload"]["header"]["event_id"] == "ev_after_reconnect"


# ── sending on a client's behalf ───────────────────────────────────────────

def _forwarded(relay: Relay, ws, event_id="ev_fwd", message_id="om_1", chat_id="oc_1") -> dict:
    relay.event(_message(event_id=event_id, message_id=message_id, chat_id=chat_id))
    frame = _frame(ws, "message")
    ws.send(json.dumps({"type": "response", "id": frame["id"], "result": {}}))
    return frame


class TestSending:
    def test_a_reply_to_a_forwarded_message_is_sent(self, relay):
        ws, _ = relay.connect()
        with ws:
            _bind(relay)
            _forwarded(relay, ws)
            ws.send(json.dumps({"type": "send", "id": "s1", "op": "reply", "target": "om_1",
                                "msg_type": "text", "content": json.dumps({"text": "42"})}))
            result = _frame(ws, "send_result")
        assert result["id"] == "s1" and result["result"]["code"] == 0
        assert result["result"]["data"]["message_id"].startswith("om_sent_")
        sent = relay.feishu.wait_for(lambda r: r["path"].endswith("/om_1/reply"))
        assert sent["body"] == {"msg_type": "text", "content": json.dumps({"text": "42"})}

    def test_a_send_to_a_chat_the_user_spoke_in(self, relay):
        ws, _ = relay.connect()
        with ws:
            _bind(relay)
            _forwarded(relay, ws, chat_id="oc_team")
            ws.send(json.dumps({"type": "send", "id": "s2", "op": "send", "target": "oc_team",
                                "msg_type": "text", "content": "{}"}))
            assert _frame(ws, "send_result")["result"]["code"] == 0
        sent = relay.feishu.wait_for(lambda r: r["body"].get("receive_id") == "oc_team")
        assert "receive_id_type=chat_id" in sent["query"]

    def test_anything_else_is_refused_without_reaching_feishu(self, relay):
        ws, _ = relay.connect()
        with ws:
            for request in ({"op": "reply", "target": "om_never_forwarded", "msg_type": "text"},
                            {"op": "send", "target": "oc_stranger", "msg_type": "text"},
                            {"op": "reply", "target": "om_1", "msg_type": "file"},
                            {"op": "delete", "target": "om_1", "msg_type": "text"}):
                ws.send(json.dumps({"type": "send", "id": "x", "content": "{}", **request}))
                result = _frame(ws, "send_result")["result"]
                assert result["code"] == -1 and result["msg"].startswith("relay refused: "), request
        assert relay.feishu.messages() == []


# ── card presses ───────────────────────────────────────────────────────────

class TestCardPresses:
    def test_a_card_the_relay_did_not_post_has_expired(self, relay):
        answer = relay.event(_press("om_unknown")).json()
        assert answer == {"toast": {"type": "error", "content": "这张卡片已失效。"}}

    def _post_card(self, relay, ws) -> str:
        _forwarded(relay, ws)
        ws.send(json.dumps({"type": "send", "id": "card", "op": "reply", "target": "om_1",
                            "msg_type": "interactive", "content": "{}"}))
        return _frame(ws, "send_result")["result"]["data"]["message_id"]

    def test_a_press_is_answered_by_the_machine_that_posted_the_card(self, relay):
        ws, _ = relay.connect()
        with ws:
            _bind(relay)
            card = self._post_card(relay, ws)
            answer: dict = {}
            pressing = threading.Thread(target=lambda: answer.update(relay.event(_press(card)).json()))
            pressing.start()
            frame = _frame(ws, "message")
            assert frame["payload"]["event"]["context"]["open_message_id"] == card
            ws.send(json.dumps({"type": "response", "id": frame["id"],
                                "result": {"toast": {"type": "success", "content": "Approved"}}}))
            pressing.join(10)
        assert answer == {"toast": {"type": "success", "content": "Approved"}}

    def test_a_press_the_machine_does_not_answer_in_time(self, relay):
        ws, _ = relay.connect()
        with ws:
            _bind(relay)
            card = self._post_card(relay, ws)
            started = time.time()
            answer = relay.event(_press(card)).json()
            assert time.time() - started < 3.5
        assert answer == {"toast": {"type": "error", "content": "Aria 本机未及时响应，请稍后再试。"}}
