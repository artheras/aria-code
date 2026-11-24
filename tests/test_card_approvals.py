"""Approvals: the bot proposes, an authorised person decides, exactly once.

Every refusal here is a way a real approval flow goes wrong: approved by the
wrong person, in the wrong chat, twice, too late, for a shipper the chat no
longer belongs to, or executing something other than what was shown.
"""

from __future__ import annotations

import asyncio
import csv
import json
import shutil
import threading
from pathlib import Path
from unittest import mock

import pytest

from aria_code.apps.channels.approvals import (
    APPROVED, EXPIRED, FAILED, PENDING, REJECTED, EXECUTORS, ApprovalStore, can_decide, propose_reorder,
)
from aria_bots.erp_logistics import OWNER_SCOPE_ENV

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "evals" / "fixtures" / "inventory_reorder" / "skus.csv"   # ACME; reorders A100 A200 A600
CHAT = "feishu:oc_acme"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for key in (OWNER_SCOPE_ENV, "ARIA_CHANNEL_APPROVERS", "ARIA_CHANNEL_ADMINS",
                "FEISHU_ALLOWED_USER_IDS", "ARIA_SHIPPER_FEEDS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ARIA_CONVERSATIONS_DB", str(tmp_path / "bot.db"))
    monkeypatch.setenv("ARIA_APPROVAL_OUTBOX", str(tmp_path / "outbox"))


def _store(tmp_path, **kwargs):
    return ApprovalStore(tmp_path / "bot.db", **kwargs)


def _request(store, lines=({"sku": "A100", "qty": 335},)):
    return store.request(conversation=CHAT, owner_id="ACME", kind="purchase_order_draft",
                         payload={"lines": list(lines)}, summary="- A100：335", requested_by="feishu:ou_ops")


def _decide(store, approval, **overrides):
    kwargs = dict(conversation=CHAT, current_owner="ACME", decided_by="feishu:ou_boss",
                  approve=True, authorised=True)
    kwargs.update(overrides)
    return store.decide(approval.id, **kwargs)


def _drafts(tmp_path):
    return sorted((tmp_path / "outbox").rglob("*.csv"))


class TestWhoMayDecide:
    def test_nobody_by_default(self):
        assert not can_decide("feishu", ("ou_boss",), env={})

    def test_approvers_and_admins_per_channel(self):
        env = {"ARIA_CHANNEL_APPROVERS": "feishu:ou_boss", "ARIA_CHANNEL_ADMINS": "feishu:ou_ops"}
        assert can_decide("feishu", ("ou_boss",), env)
        assert can_decide("feishu", ("ou_ops",), env)
        assert not can_decide("telegram", ("ou_boss",), env)
        assert not can_decide("feishu", ("ou_member",), env)


class TestDecisions:
    def test_approval_writes_exactly_the_approved_lines(self, tmp_path):
        store = _store(tmp_path)
        decision = _decide(store, _request(store, [{"sku": "A100", "qty": 335}, {"sku": "A600", "qty": 161}]))
        assert decision.ok and decision.approval.status == APPROVED
        (draft,) = _drafts(tmp_path)
        rows = list(csv.DictReader(draft.open()))
        assert [(r["sku"], r["order_qty"]) for r in rows] == [("A100", "335"), ("A600", "161")]
        assert {r["approved_by"] for r in rows} == {"feishu:ou_boss"}
        assert oct(draft.parent.stat().st_mode & 0o777) == "0o700"

    def test_the_payload_is_frozen_at_request_time(self, tmp_path):
        inventory = tmp_path / "skus.csv"
        shutil.copy(INVENTORY, inventory)
        summary, payload = propose_reorder("ACME", str(inventory))
        store = _store(tmp_path)
        approval = store.request(conversation=CHAT, owner_id="ACME", kind="purchase_order_draft",
                                 payload=payload, summary=summary, requested_by="t")
        inventory.write_text("owner_id,sku\n")  # the data changes before anyone approves
        assert _decide(store, approval).ok
        (draft,) = _drafts(tmp_path)
        assert [r["sku"] for r in csv.DictReader(draft.open())] == ["A100", "A200", "A600"]

    def test_rejection_executes_nothing(self, tmp_path):
        store = _store(tmp_path)
        assert _decide(store, _request(store), approve=False).approval.status == REJECTED
        assert _drafts(tmp_path) == []

    def test_an_unauthorised_press_changes_nothing(self, tmp_path):
        store = _store(tmp_path)
        approval = _request(store)
        assert not _decide(store, approval, authorised=False).ok
        assert store.get(approval.id).status == PENDING

    def test_another_chat_cannot_approve_and_learns_nothing(self, tmp_path):
        store = _store(tmp_path)
        approval = _request(store)
        forwarded = _decide(store, approval, conversation="feishu:oc_other")
        missing = store.decide("no-such-id", conversation=CHAT, current_owner="ACME",
                               decided_by="x", approve=True, authorised=True)
        assert not forwarded.ok and forwarded.message == missing.message
        assert store.get(approval.id).status == PENDING

    def test_only_one_decision_counts(self, tmp_path):
        store = _store(tmp_path)
        approval = _request(store)
        assert _decide(store, approval).ok
        second = _decide(store, approval, decided_by="feishu:ou_other")
        assert not second.ok and "ou_boss" in second.message
        assert len(_drafts(tmp_path)) == 1

    def test_two_simultaneous_approvals_execute_once(self, tmp_path):
        store = _store(tmp_path)
        approval = _request(store)
        barrier, results = threading.Barrier(2), []

        def press(who):
            barrier.wait()
            results.append(_decide(ApprovalStore(store.path), approval, decided_by=who).ok)

        threads = [threading.Thread(target=press, args=(f"feishu:ou_{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == [False, True]
        assert len(_drafts(tmp_path)) == 1

    def test_a_stale_request_expires(self, tmp_path):
        now = [1000.0]
        store = _store(tmp_path, ttl_seconds=60, clock=lambda: now[0])
        approval = _request(store)
        now[0] += 61
        assert not _decide(store, approval).ok
        assert store.get(approval.id).status == EXPIRED
        assert _drafts(tmp_path) == []

    def test_a_rebound_conversation_cannot_execute_the_old_shippers_request(self, tmp_path):
        store = _store(tmp_path)
        approval = _request(store)
        assert not _decide(store, approval, current_owner="BETA").ok
        assert store.get(approval.id).status == PENDING

    def test_a_failure_is_recorded_but_not_shown(self, tmp_path):
        store = _store(tmp_path)
        approval = _request(store)

        def broken(_):
            raise PermissionError("/srv/secret/outbox is not writable")

        with mock.patch.dict(EXECUTORS, {"purchase_order_draft": broken}):
            decision = _decide(store, approval)
        assert not decision.ok and "/srv" not in decision.message
        assert store.get(approval.id).status == FAILED
        assert "/srv/secret" in store.get(approval.id).result

    def test_the_executor_runs_under_the_shippers_scope(self, tmp_path):
        import os
        store = _store(tmp_path)
        seen = {}

        def spy(approval):
            seen["scope"] = os.environ.get(OWNER_SCOPE_ENV)
            return "ok"

        with mock.patch.dict(EXECUTORS, {"purchase_order_draft": spy}):
            _decide(store, _request(store))
        assert seen["scope"] == "ACME"
        assert OWNER_SCOPE_ENV not in os.environ

    def test_unknown_actions_cannot_be_requested(self, tmp_path):
        with pytest.raises(ValueError):
            _store(tmp_path).request(conversation=CHAT, owner_id="ACME", kind="wire_money",
                                     payload={}, summary="", requested_by="t")

    def test_ids_are_unguessable(self, tmp_path):
        assert len(_request(_store(tmp_path)).id) >= 16


# ── Through Feishu ───────────────────────────────────────────────────────────

import aria_bots.feishu_bot as bot  # noqa: E402


@pytest.fixture
def feishu(monkeypatch, tmp_path):
    monkeypatch.setenv("FEISHU_ALLOWED_USER_IDS", "ou_ops,ou_boss,ou_member")
    monkeypatch.setenv("ARIA_CHANNEL_APPROVERS", "feishu:ou_boss")
    feeds = tmp_path / "feeds.json"
    feeds.write_text(json.dumps({"ACME": {"inventory": str(INVENTORY)}}))
    monkeypatch.setenv("ARIA_SHIPPER_FEEDS", str(feeds))
    bot._conversation_store().bind_owner(CHAT, "ACME", bound_by="t")
    posts = []

    async def token():
        return "t"

    async def post(url, token, payload):
        posts.append(payload)
        return {"code": 0}

    async def reply_text(message_id, text):
        posts.append({"text": text})

    with mock.patch.object(bot, "_get_access_token", token), \
         mock.patch.object(bot, "_feishu_post", post), \
         mock.patch.object(bot, "reply_text", reply_text):
        yield posts


def _ask_for_reorder():
    event = {"header": {"event_type": "im.message.receive_v1"}, "event": {
        "sender": {"sender_id": {"open_id": "ou_ops"}},
        "message": {"message_id": "om_1", "chat_id": "oc_acme", "chat_type": "group",
                    "message_type": "text", "content": json.dumps({"text": "@_user_1 /补货"}),
                    "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}]}}}

    async def go():
        await bot.dispatch_event(event)
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(go())


def _press(approval_id, *, who="ou_boss", decision="approve", chat="oc_acme"):
    event = {"header": {"event_type": "card.action.trigger"}, "event": {
        "operator": {"open_id": who},
        "action": {"value": {"aria_approval": approval_id, "decision": decision}},
        "context": {"open_chat_id": chat}}}
    return asyncio.run(bot.dispatch_event(event))


def _card_buttons(post):
    card = json.loads(post["content"])
    return [a for e in card["elements"] if e.get("tag") == "action" for a in e["actions"]]


class TestFeishuApprovals:
    def test_reorder_posts_a_card_with_approve_and_reject(self, feishu):
        _ask_for_reorder()
        (post,) = [p for p in feishu if "content" in p]
        buttons = _card_buttons(post)
        assert [b["value"]["decision"] for b in buttons] == ["approve", "reject"]
        text = json.dumps(json.loads(post["content"]), ensure_ascii=False)
        assert "A100" in text and "不会自动下单" in text

    def test_an_approver_approves_and_the_card_shows_the_outcome(self, feishu, tmp_path):
        _ask_for_reorder()
        approval_id = _card_buttons([p for p in feishu if "content" in p][0])[0]["value"]["aria_approval"]
        response = _press(approval_id)
        assert response["toast"]["type"] == "success"
        card_text = json.dumps(response["card"]["data"], ensure_ascii=False)
        assert "已批准" in card_text and "PO-ACME-" in card_text
        assert str(tmp_path) not in card_text, "the host path must not reach the chat"
        assert len(_drafts(tmp_path)) == 1

    def test_a_member_who_is_not_an_approver_is_refused(self, feishu, tmp_path):
        _ask_for_reorder()
        approval_id = _card_buttons([p for p in feishu if "content" in p][0])[0]["value"]["aria_approval"]
        assert _press(approval_id, who="ou_member")["toast"]["type"] == "error"
        assert _drafts(tmp_path) == []

    def test_someone_not_allowed_to_use_the_bot_is_refused(self, feishu, tmp_path):
        _ask_for_reorder()
        approval_id = _card_buttons([p for p in feishu if "content" in p][0])[0]["value"]["aria_approval"]
        assert _press(approval_id, who="ou_outsider")["toast"]["type"] == "error"
        assert _drafts(tmp_path) == []

    def test_a_card_forwarded_to_another_chat_cannot_be_approved(self, feishu, tmp_path):
        _ask_for_reorder()
        approval_id = _card_buttons([p for p in feishu if "content" in p][0])[0]["value"]["aria_approval"]
        assert _press(approval_id, chat="oc_elsewhere")["toast"]["type"] == "error"
        assert _drafts(tmp_path) == []

    def test_an_unbound_group_is_told_how_to_bind(self, feishu):
        bot._conversation_store().unbind_owner(CHAT)
        _ask_for_reorder()
        assert any("/owner" in p.get("text", "") for p in feishu)
