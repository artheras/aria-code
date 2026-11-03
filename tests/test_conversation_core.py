"""The platform-neutral conversation layer, and the Feishu adapter on top of it.

apps/channels/conversation.py holds the rules every chat channel shares —
Feishu now, Arthera's own bot later — so they are tested here once, without
any platform, and then through Feishu to show an adapter actually uses them.
"""

from __future__ import annotations

import asyncio
import json
from unittest import mock

import pytest

from aria_code.apps.channels.conversation import (
    ConversationStore,
    InboundMessage,
    handle_owner_command,
    is_admin,
    parse_owner_command,
    prepare_turn,
    should_respond,
)
from aria_code.tools.logistics_tenancy import OWNER_SCOPE_ENV, TenancyError, scope_records


def _msg(text="hello", *, kind="group", sender="ou_ops", channel="feishu", chat="oc_acme", mentioned=True):
    return InboundMessage(channel=channel, conversation_id=chat, kind=kind, sender_id=sender,
                          text=text, mentions_bot=mentioned)


@pytest.fixture
def store(tmp_path):
    return ConversationStore(tmp_path / "c.db")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for key in (OWNER_SCOPE_ENV, "ARIA_CHANNEL_ADMINS", "FEISHU_ALLOWED_USER_IDS", "FEISHU_BOT_OPEN_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ARIA_CONVERSATIONS_DB", str(tmp_path / "bot.db"))


class TestWhenToAnswer:
    def test_direct_chats_always(self):
        assert should_respond(_msg(kind="direct", mentioned=False))

    def test_groups_only_when_addressed(self):
        assert should_respond(_msg(mentioned=True))
        assert not should_respond(_msg(mentioned=False))


class TestAdmins:
    def test_nobody_is_an_admin_by_default(self):
        assert not is_admin(_msg(), env={})

    def test_admin_is_per_channel(self):
        env = {"ARIA_CHANNEL_ADMINS": "feishu:ou_ops, telegram:42"}
        assert is_admin(_msg(sender="ou_ops"), env)
        assert not is_admin(_msg(sender="ou_ops", channel="telegram"), env)
        assert is_admin(_msg(sender="42", channel="telegram"), env)


class TestOwnerBinding:
    ADMIN = {"ARIA_CHANNEL_ADMINS": "feishu:ou_ops"}

    @pytest.mark.parametrize("text, expected", [
        ("/owner", ("show", "")), ("/owner ACME", ("bind", "ACME")), ("/货主 ACME", ("bind", "ACME")),
        ("/owner off", ("unbind", "")), ("/owner 解除", ("unbind", "")), ("what is /owner", None),
        ("/ownership", None),
    ])
    def test_parsing(self, text, expected):
        assert parse_owner_command(text) == expected

    def test_an_admin_binds_a_group(self, store):
        reply = handle_owner_command(_msg("/owner ACME"), store, self.ADMIN)
        assert "ACME" in reply
        assert store.owner_for("feishu:oc_acme") == "ACME"
        assert "ACME" in handle_owner_command(_msg("/owner", sender="ou_anyone"), store, {})

    def test_a_non_admin_cannot(self, store):
        handle_owner_command(_msg("/owner ACME", sender="ou_member"), store, self.ADMIN)
        assert store.owner_for("feishu:oc_acme") is None

    def test_a_direct_chat_cannot_be_bound(self, store):
        handle_owner_command(_msg("/owner ACME", kind="direct"), store, self.ADMIN)
        assert store.owner_for("feishu:oc_acme") is None

    def test_a_malformed_owner_id_is_refused(self, store):
        handle_owner_command(_msg("/owner ACME;DROP"), store, self.ADMIN)
        assert store.owner_for("feishu:oc_acme") is None

    def test_unbinding(self, store):
        handle_owner_command(_msg("/owner ACME"), store, self.ADMIN)
        handle_owner_command(_msg("/owner off"), store, self.ADMIN)
        assert store.owner_for("feishu:oc_acme") is None


class TestHistory:
    def test_conversations_never_share_context(self, store):
        store.record("feishu:oc_acme", "user", "ACME stock is 560 units")
        store.record("feishu:oc_beta", "user", "BETA question")
        assert [h["content"] for h in store.history("feishu:oc_beta")] == ["BETA question"]

    def test_the_same_chat_id_on_two_platforms_is_two_conversations(self, store):
        store.record("feishu:123", "user", "from feishu")
        assert store.history("telegram:123") == []

    def test_bounded_by_count(self, tmp_path):
        small = ConversationStore(tmp_path / "s.db", max_turns=3)
        for i in range(6):
            small.record("k", "user", f"m{i}")
        assert [h["content"] for h in small.history("k")] == ["m3", "m4", "m5"]

    def test_bounded_by_age(self, tmp_path):
        now = [1000.0]
        aged = ConversationStore(tmp_path / "a.db", ttl_seconds=60, clock=lambda: now[0])
        aged.record("k", "user", "old")
        now[0] += 61
        aged.record("k", "user", "new")
        assert [h["content"] for h in aged.history("k")] == ["new"]

    def test_the_file_is_private(self, store):
        assert oct(store.path.stat().st_mode & 0o777) == "0o600"


class TestTurns:
    def test_a_turn_carries_the_conversation_so_far_not_the_prompt_twice(self, store):
        store.record("feishu:oc_acme", "user", "earlier question")
        store.record("feishu:oc_acme", "assistant", "earlier answer")
        turn = prepare_turn(_msg("follow-up"), store)
        assert [h["content"] for h in turn.history] == ["earlier question", "earlier answer"]
        assert turn.prompt == "follow-up"
        assert store.history("feishu:oc_acme")[-1]["content"] == "follow-up"

    def test_a_bound_conversation_scopes_the_turn(self, store):
        store.bind_owner("feishu:oc_acme", "ACME", bound_by="test")
        turn = prepare_turn(_msg("库存怎么样"), store)
        assert turn.owner_scope == "ACME"
        assert turn.env == {OWNER_SCOPE_ENV: "ACME"}
        assert "ACME" in turn.prompt
        assert turn.as_task(_msg())["owner_scope"] == "ACME"

    def test_an_unbound_conversation_is_unscoped(self, store):
        assert prepare_turn(_msg(), store).env == {}


class TestScopeIsEnforcedByTheTools:
    """The binding must hold even if the model ignores the prompt."""

    RECORDS = [{"owner_id": "ACME", "sku": "A1"}, {"owner_id": "BETA", "sku": "B1"}]

    def test_defaults_to_the_bound_shipper(self, monkeypatch):
        monkeypatch.setenv(OWNER_SCOPE_ENV, "ACME")
        records, scope = scope_records(self.RECORDS)
        assert [r["sku"] for r in records] == ["A1"]
        assert scope["conversation_scoped"] is True

    def test_another_shipper_is_refused(self, monkeypatch):
        monkeypatch.setenv(OWNER_SCOPE_ENV, "ACME")
        with pytest.raises(TenancyError):
            scope_records(self.RECORDS, "BETA")

    def test_the_cross_shipper_view_is_refused(self, monkeypatch):
        monkeypatch.setenv(OWNER_SCOPE_ENV, "ACME")
        with pytest.raises(TenancyError):
            scope_records(self.RECORDS, all_owners=True)

    def test_unlabelled_records_are_refused(self, monkeypatch):
        monkeypatch.setenv(OWNER_SCOPE_ENV, "ACME")
        with pytest.raises(TenancyError):
            scope_records([{"sku": "X"}])

    def test_a_real_tool_honours_it(self, monkeypatch):
        from aria_code.tools.logistics_inventory import tool_plan_inventory_policy
        skus = [{"owner_id": o, "sku": s, "on_hand": 10, "lead_time_days": 5,
                 "daily_demand": [3] * 20} for o, s in (("ACME", "A1"), ("BETA", "B1"))]
        monkeypatch.setenv(OWNER_SCOPE_ENV, "ACME")
        result = tool_plan_inventory_policy({"skus": skus})
        assert [i["sku"] for i in result["data"]["items"]] == ["A1"]
        assert not tool_plan_inventory_policy({"skus": skus, "all_owners": True})["success"]


# ── Through the Feishu adapter ───────────────────────────────────────────────

import aria_code.aria_feishu_bot as bot  # noqa: E402


def _feishu(text, *, chat_type="group", open_id="ou_ops", mentions=True, msg_type="text"):
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {
                "message_id": "om_1", "chat_id": "oc_acme", "chat_type": chat_type,
                "message_type": msg_type, "content": json.dumps({"text": text}),
                "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}] if mentions else [],
            },
        },
    }


def _run(event):
    seen = {"replies": [], "nl": [], "commands": []}

    async def reply(message_id, text):
        seen["replies"].append(text)

    async def nl(text, message_id, chat_id="", *, turn=None, conversation=""):
        seen["nl"].append(turn)

    async def command(text, message_id, user_id, chat_id):
        seen["commands"].append((text, bot._TURN_OWNER.get()))

    async def go():
        with mock.patch.object(bot, "reply_text", reply), \
             mock.patch.object(bot, "_handle_nl_query", nl), \
             mock.patch.object(bot, "_handle_command", command):
            await bot.dispatch_event(event)
            await asyncio.sleep(0)

    asyncio.run(go())
    return seen


class TestFeishuAdapter:
    @pytest.fixture(autouse=True)
    def allowed(self, monkeypatch):
        monkeypatch.setenv("FEISHU_ALLOWED_USER_IDS", "ou_ops")
        monkeypatch.setenv("ARIA_CHANNEL_ADMINS", "feishu:ou_ops")

    def test_a_group_message_not_addressed_to_the_bot_is_ignored(self, monkeypatch):
        monkeypatch.setenv("FEISHU_BOT_OPEN_ID", "ou_bot")
        event = _feishu("@_user_1 hi")
        event["event"]["message"]["mentions"][0]["id"]["open_id"] = "ou_someone_else"
        assert _run(event) == {"replies": [], "nl": [], "commands": []}

    def test_an_image_in_a_group_is_ignored(self):
        assert _run(_feishu("", msg_type="image")) == {"replies": [], "nl": [], "commands": []}

    def test_a_mention_is_answered_with_the_mention_removed_and_context_kept(self):
        _run(_feishu("@_user_1 第一个问题"))
        (turn,) = _run(_feishu("@_user_1 接着问"))["nl"]
        assert turn.prompt == "接着问"
        assert [h["content"] for h in turn.history] == ["第一个问题"]

    def test_binding_then_commands_run_under_the_shipper_scope(self):
        seen = _run(_feishu("@_user_1 /owner ACME"))
        assert seen["replies"] and "ACME" in seen["replies"][0]
        assert _run(_feishu("@_user_1 /run /inventory skus.csv"))["commands"] == [
            ("/run /inventory skus.csv", "ACME")]
        assert bot._TURN_OWNER.get() is None, "the scope leaked out of dispatch_event"

    def test_the_aria_subprocess_receives_the_scope(self, monkeypatch, tmp_path):
        # The Python CLI path, taken only when no Go engine is installed.
        monkeypatch.setattr(bot._engine, "find_engine", lambda: None)
        monkeypatch.setattr(bot, "_ARIA_CODE_DIR", tmp_path)
        (tmp_path / "aria_cli.py").write_text("")
        captured = {}

        async def fake_exec(*argv, **kwargs):
            captured.update(kwargs.get("env") or {})
            raise RuntimeError("stop here")

        async def go():
            token = bot._TURN_OWNER.set("ACME")
            try:
                with mock.patch.object(bot.asyncio, "create_subprocess_exec", fake_exec):
                    await bot._query_aria_llm("/inventory", timeout=5)
            finally:
                bot._TURN_OWNER.reset(token)

        asyncio.run(go())
        assert captured.get(OWNER_SCOPE_ENV) == "ACME"
