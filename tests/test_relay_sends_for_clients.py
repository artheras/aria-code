"""Relay mode can answer: the client's bot hands each message back to the relay.

Before 2026-10-03 relay mode dropped every answer: the relay forwarded each
event, and the client's bot tried to reply with app credentials that relay
mode deliberately keeps off the user's machine. Now the client hands its
message back over the WebSocket and the relay sends it. What the relay
refuses to send for a client is tested against the Go relay in
tests/test_relay_contract.py; these tests cover the client side.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import aria_code.aria_feishu_bot as bot
from aria_code.clients import aria_relay_client as relay_client


def _reply(target="om_1", **kw):
    return {"op": "reply", "target": target, "msg_type": "text", "content": "{}", **kw}



@pytest.fixture
def relay_mode(monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    sent = []

    async def relay_send(request):
        sent.append(request)
        return {"code": 0, "data": {"message_id": "om_new"}}

    bot.set_relay_sender(relay_send)
    yield sent
    bot.set_relay_sender(None)


class TestTheBotInRelayMode:
    def test_replies_go_through_the_relay(self, relay_mode):
        asyncio.run(bot.reply_text("om_1", "hello"))
        assert relay_mode == [{"op": "reply", "target": "om_1", "msg_type": "text",
                               "content": json.dumps({"text": "hello"}), "receive_id_type": "chat_id"}]

    def test_proactive_cards_go_through_the_relay(self, relay_mode):
        assert asyncio.run(bot.send_card_to_chat("oc_acme", "t", "b")) is True
        assert relay_mode[0]["op"] == "send" and relay_mode[0]["target"] == "oc_acme"
        assert bot.can_send()

    def test_with_app_credentials_it_sends_directly(self, relay_mode, monkeypatch):
        posts = []

        async def token():
            return "t"

        async def post(url, token, payload):
            posts.append(url)
            return {"code": 0}

        monkeypatch.setattr(bot, "_get_access_token", token)
        monkeypatch.setattr(bot, "_feishu_post", post)
        asyncio.run(bot.reply_text("om_1", "hello"))
        assert posts and relay_mode == []

    def test_without_either_it_cannot_send_and_says_so(self, monkeypatch):
        monkeypatch.delenv("FEISHU_APP_ID", raising=False)
        bot.set_relay_sender(None)
        assert not bot.can_send()
        assert asyncio.run(bot.send_card_to_chat("oc_acme", "t", "b")) is False

    def test_an_attachment_is_declined_with_an_explanation(self, relay_mode, monkeypatch, tmp_path):
        monkeypatch.setenv("ARIA_CONVERSATIONS_DB", str(tmp_path / "c.db"))
        event = {"header": {"event_type": "im.message.receive_v1"}, "event": {
            "sender": {"sender_id": {"open_id": "ou_alice"}},
            "message": {"message_id": "om_1", "chat_id": "oc_dm", "chat_type": "p2p",
                        "message_type": "image", "content": "{}"}}}
        asyncio.run(bot.dispatch_event(event, authorized_by_binding=True))
        assert "不支持" in json.loads(relay_mode[0]["content"])["text"]


class TestRelayClientSender:
    def test_a_send_waits_for_the_relays_confirmation(self):
        frames = []

        class FakeSocket:
            async def send(self, raw):
                frames.append(json.loads(raw))
                relay_client._settle_send({"id": frames[-1]["id"], "result": {"code": 0}})

        result = asyncio.run(relay_client._relay_sender(FakeSocket())(_reply()))
        assert result == {"code": 0}
        assert frames[0]["type"] == "send" and frames[0]["target"] == "om_1"
        assert relay_client._pending_sends == {}
