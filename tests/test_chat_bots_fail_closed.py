"""Chat bots must refuse by default and must not approve tools on anyone's behalf.

Before 2026-10-03 three things combined on the Feishu bot:

  1. FEISHU_ALLOWED_USER_IDS empty meant "allow everybody";
  2. the bot ran aria with ARIA_BOT_MODE=1, which started the session with
     every tool approved — run_command, write_file, edit_file, multi_edit;
  3. the standalone server dispatched any POST without checking it came from
     Feishu, so the sender (allowlisted or not) was whatever the POST said.

Together: anyone who could reach the port, or message an unconfigured bot,
could run shell commands on its host. The Telegram bot had (1) too. Each test
here pins one link of that chain.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import aria_feishu_bot as bot
from aria_telegram_bot import TelegramBot

ROOT = Path(__file__).resolve().parents[1]

ENV_KEYS = ("FEISHU_ALLOWED_USER_IDS", "FEISHU_ENCRYPT_KEY", "FEISHU_VERIFICATION_TOKEN",
            "FEISHU_ALLOW_UNVERIFIED_EVENTS", "ARIA_BOT_ALLOW_TOOLS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Served messages are recorded per conversation; keep that out of ~/.aria.
    monkeypatch.setenv("ARIA_CONVERSATIONS_DB", str(tmp_path / "conversations.db"))


def _event(text="/run /price AAPL", *, open_id="ou_stranger", user_id="", chat_type="p2p"):
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id, "user_id": user_id}},
            "message": {"message_id": "om_1", "chat_id": "oc_1", "chat_type": chat_type,
                        "message_type": "text", "content": json.dumps({"text": text})},
        },
    }


def _dispatch(event, **kwargs):
    """Run dispatch_event and report (replies sent, commands started)."""
    replies, commands = [], []

    async def fake_reply(message_id, text):
        replies.append(text)

    async def fake_command(text, message_id, user_id, chat_id):
        commands.append(text)

    async def run():
        with mock.patch.object(bot, "reply_text", fake_reply), \
             mock.patch.object(bot, "_handle_command", fake_command):
            await bot.dispatch_event(event, **kwargs)
            await asyncio.sleep(0)  # let create_task'd handlers run

    asyncio.run(run())
    return replies, commands


class TestFeishuAllowlist:
    def test_an_empty_allowlist_allows_nobody(self):
        assert not bot._is_allowed_user("ou_anyone", "u_anyone")

    def test_either_sender_id_can_be_listed(self, monkeypatch):
        monkeypatch.setenv("FEISHU_ALLOWED_USER_IDS", "ou_alice, u_bob")
        assert bot._is_allowed_user("", "ou_alice")
        assert bot._is_allowed_user("u_bob", "ou_other")
        assert not bot._is_allowed_user("u_eve", "ou_eve")

    def test_an_unlisted_sender_runs_nothing_and_is_told_their_id(self):
        replies, commands = _dispatch(_event())
        assert commands == []
        assert len(replies) == 1 and "ou_stranger" in replies[0]

    def test_in_a_group_an_unlisted_sender_is_ignored_silently(self):
        replies, commands = _dispatch(_event(chat_type="group"))
        assert (replies, commands) == ([], [])

    def test_a_listed_sender_is_served(self, monkeypatch):
        monkeypatch.setenv("FEISHU_ALLOWED_USER_IDS", "ou_alice")
        _, commands = _dispatch(_event(open_id="ou_alice"))
        assert commands == ["/run /price AAPL"]

    def test_the_relay_binding_authorizes_its_sender(self):
        # The relay only forwards to the machine the sender bound.
        _, commands = _dispatch(_event(), authorized_by_binding=True)
        assert commands == ["/run /price AAPL"]

    def test_the_relay_client_says_so(self):
        source = (ROOT / "services" / "aria-bots" / "src" / "aria_bots" / "relay_client.py").read_text(encoding="utf-8")
        assert "dispatch_event(payload, authorized_by_binding=True)" in source


class TestFeishuEventVerification:
    BODY = json.dumps({"token": "tok", "challenge": "c"}).encode()

    def test_nothing_configured_refuses_everything(self):
        trusted, reason = bot.verify_feishu_request({}, self.BODY, json.loads(self.BODY))
        assert not trusted and "refused" in reason

    def test_the_local_testing_escape_hatch_is_explicit(self, monkeypatch):
        monkeypatch.setenv("FEISHU_ALLOW_UNVERIFIED_EVENTS", "1")
        assert bot.verify_feishu_request({}, self.BODY, {})[0]

    def test_verification_token(self, monkeypatch):
        monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "tok")
        assert bot.verify_feishu_request({}, self.BODY, {"token": "tok"})[0]
        assert not bot.verify_feishu_request({}, self.BODY, {"token": "forged"})[0]

    def test_signature(self, monkeypatch):
        monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "key")
        good = hashlib.sha256(b"17" + b"n" + b"key" + self.BODY).hexdigest()
        headers = {"X-Lark-Request-Timestamp": "17", "X-Lark-Request-Nonce": "n"}
        assert bot.verify_feishu_request({**headers, "X-Lark-Signature": good}, self.BODY, {})[0]
        assert not bot.verify_feishu_request({**headers, "X-Lark-Signature": "0" * 64}, self.BODY, {})[0]
        assert not bot.verify_feishu_request(headers, self.BODY, {})[0]

    def test_the_standalone_server_refuses_a_forged_event(self, monkeypatch):
        aiohttp_test = pytest.importorskip("aiohttp.test_utils")
        monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "tok")
        monkeypatch.setenv("FEISHU_ALLOWED_USER_IDS", "ou_admin")
        dispatched = []

        async def fake_dispatch(body, **kwargs):
            dispatched.append(body)
            return {"code": 0}

        async def run():
            with mock.patch.object(bot, "dispatch_event", fake_dispatch):
                async with aiohttp_test.TestClient(aiohttp_test.TestServer(bot._standalone_app())) as client:
                    forged = _event(open_id="ou_admin")  # claims to be the admin, has no token
                    refused = await client.post("/feishu/event", data=json.dumps(forged))
                    genuine = await client.post("/feishu/event", data=json.dumps({**forged, "token": "tok"}))
                    return refused.status, genuine.status

        refused, genuine = asyncio.run(run())
        assert refused == 401
        assert genuine == 200
        assert len(dispatched) == 1


class TestBotsDoNotApproveTools:
    def test_bot_mode_starts_with_nothing_approved(self):
        # pyproject's pytest `pythonpath` applies to this process only. The
        # Python-package workflow does not install the package, so without
        # this the child could not import aria_code at all — which is how this
        # test failed there on every run since it was added, while passing
        # anywhere the package was pip-installed.
        paths = [str(ROOT / "src"), str(ROOT / "src" / "aria_code"), os.environ.get("PYTHONPATH", "")]
        proc = subprocess.run(
            [sys.executable, "-c", "import aria_code.aria_cli as c; print(c._auto_approve_session)"],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "ARIA_BOT_MODE": "1", "PYTHONPATH": os.pathsep.join(p for p in paths if p)},
        )
        assert proc.returncode == 0, proc.stderr[-1000:]
        assert proc.stdout.strip().splitlines()[-1] == "False"

    def test_no_tools_are_allowed_unless_the_operator_names_them(self, monkeypatch):
        assert bot._bot_cli_flags() == []
        monkeypatch.setenv("ARIA_BOT_ALLOW_TOOLS", "write_file, edit_file")
        assert bot._bot_cli_flags() == ["--allow-tools", "write_file,edit_file"]


class TestTelegramAllowlist:
    def test_an_empty_allowlist_allows_nobody(self):
        assert not TelegramBot(token="t").is_allowed(12345)

    def test_a_listed_chat_is_allowed(self):
        assert TelegramBot(token="t", allowed_chat_ids={12345}).is_allowed(12345)
        assert not TelegramBot(token="t", allowed_chat_ids={12345}).is_allowed(999)
