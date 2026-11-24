"""The daily shipper digest: right content, right group, nothing else's.

apps/channels/digest.py pushes each group bound to a shipper what needs
attention. These pin what a 3PL would be embarrassed by: one shipper's lines
in another's group, a digest built from a file nobody can attribute, a
configuration error shown to a client, and a "sent" that was never delivered.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
from pathlib import Path
from unittest import mock

import pytest

from aria_code.apps.channels.conversation import ConversationStore
from aria_code.apps.channels.digest import build_digest, load_feeds, run_digests
from aria_bots.erp_logistics import OWNER_SCOPE_ENV

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "evals" / "fixtures" / "inventory_reorder" / "skus.csv"     # all ACME
WAYBILLS = ROOT / "evals" / "fixtures" / "freight_audit" / "waybills.csv"     # no owner_id


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for key in (OWNER_SCOPE_ENV, "ARIA_SHIPPER_FEEDS", "FEISHU_ALLOWED_USER_IDS", "ARIA_CHANNEL_ADMINS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ARIA_CONVERSATIONS_DB", str(tmp_path / "bot.db"))


def _shared_export(tmp_path) -> dict:
    """One multi-shipper WMS/TMS export each — the common 3PL setup."""
    inventory = tmp_path / "all_skus.csv"
    rows = list(csv.DictReader(INVENTORY.open()))
    beta = [{**r, "owner_id": "BETA", "sku": "B" + r["sku"][1:]} for r in rows]
    with inventory.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows + beta)
    waybills = tmp_path / "all_waybills.csv"
    rows = list(csv.DictReader(WAYBILLS.open()))
    with waybills.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["owner_id", *rows[0]])
        writer.writeheader()
        writer.writerows({"owner_id": "ACME", **r} for r in rows)
    return {"*": {"inventory": str(inventory), "waybills": str(waybills)}}


class TestBuildDigest:
    def test_a_shipper_sees_its_reorders_thin_history_and_freight_issues(self, tmp_path):
        digest = build_digest("ACME", _shared_export(tmp_path))
        assert digest.worth_sending and not digest.problems
        for expected in ("A100", "A200", "A600", "A400", "W1007", "W1093", "YTO → JD"):
            assert expected in digest.body
        assert "A500" not in digest.body, "covered by an open purchase order"

    def test_a_shared_export_yields_only_that_shippers_lines(self, tmp_path):
        digest = build_digest("BETA", _shared_export(tmp_path))
        assert "B100" in digest.body
        assert "A100" not in digest.body
        assert "W1007" not in digest.body, "the waybills are all ACME's"

    def test_a_file_that_cannot_be_attributed_is_refused_not_guessed(self):
        digest = build_digest("ACME", {"ACME": {"waybills": str(WAYBILLS)}})
        assert not digest.worth_sending
        assert digest.problems and "owner_id" in digest.problems[0]

    def test_no_feeds_is_a_problem_not_an_empty_success(self):
        digest = build_digest("ACME", {})
        assert not digest.worth_sending and digest.problems

    def test_the_scope_does_not_outlive_the_digest(self, tmp_path):
        build_digest("ACME", _shared_export(tmp_path))
        assert OWNER_SCOPE_ENV not in os.environ

    def test_feeds_come_from_the_operators_file(self, tmp_path, monkeypatch):
        path = tmp_path / "feeds.json"
        path.write_text(json.dumps({"ACME": {"inventory": "/data/a.csv"}}))
        monkeypatch.setenv("ARIA_SHIPPER_FEEDS", str(path))
        assert load_feeds() == {"ACME": {"inventory": "/data/a.csv"}}
        monkeypatch.delenv("ARIA_SHIPPER_FEEDS")
        assert load_feeds() == {}


class TestRunDigests:
    def test_each_bound_group_gets_its_own_shippers_digest(self, tmp_path):
        store = ConversationStore(tmp_path / "c.db")
        store.bind_owner("feishu:oc_acme", "ACME", bound_by="t")
        store.bind_owner("feishu:oc_gamma", "GAMMA", bound_by="t")   # nothing in the export
        store.bind_owner("telegram:42", "ACME", bound_by="t")        # no telegram sender here
        sent = []

        async def send(conversation_id, title, body):
            sent.append((conversation_id, body))
            return True

        report = asyncio.run(run_digests(store, {"feishu": send}, _shared_export(tmp_path)))
        statuses = {r["conversation"]: r["status"] for r in report}
        assert statuses == {"feishu:oc_acme": "sent", "feishu:oc_gamma": "nothing_to_report",
                            "telegram:42": "no_sender"}
        assert [c for c, _ in sent] == ["oc_acme"]
        assert "A100" in sent[0][1]

    def test_a_failed_send_is_reported_as_failed(self, tmp_path):
        store = ConversationStore(tmp_path / "c.db")
        store.bind_owner("feishu:oc_acme", "ACME", bound_by="t")

        async def send(*args):
            return False

        report = asyncio.run(run_digests(store, {"feishu": send}, _shared_export(tmp_path)))
        assert report == [{"conversation": "feishu:oc_acme", "status": "send_failed"}]

    def test_problems_stay_in_the_log(self, tmp_path):
        store = ConversationStore(tmp_path / "c.db")
        store.bind_owner("feishu:oc_acme", "ACME", bound_by="t")
        sent = []

        async def send(*args):
            sent.append(args)
            return True

        asyncio.run(run_digests(store, {"feishu": send}, {"ACME": {"waybills": str(WAYBILLS)}}))
        assert sent == []


# ── Feishu ────────────────────────────────────────────────────────────────────

import aria_bots.feishu_bot as bot  # noqa: E402


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload, posts):
        self.payload, self.posts = payload, posts

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self.posts.append(kwargs["json"])
        return _Response(self.payload)


def _send(payload):
    posts = []

    async def token():
        return "t"

    async def go():
        with mock.patch.object(bot, "_get_access_token", token), \
             mock.patch("httpx.AsyncClient", lambda **kw: _Client(payload, posts)):
            return await bot.send_card_to_chat("oc_acme", "title", "body")

    return asyncio.run(go()), posts


class TestFeishuSend:
    def test_the_content_is_the_card_itself(self):
        ok, posts = _send({"code": 0})
        assert ok
        card = json.loads(posts[0]["content"])
        assert "card" not in card, "the {'card': ...} wrapper is the webhook format; the API rejects it"
        assert card["header"]["title"]["content"] == "title"

    def test_a_rejected_send_is_not_reported_as_delivered(self):
        ok, _ = _send({"code": 230002, "msg": "bot not in chat"})
        assert ok is False


class TestDigestCommand:
    def _run(self, monkeypatch, tmp_path, *, bound):
        monkeypatch.setenv("FEISHU_ALLOWED_USER_IDS", "ou_ops")
        monkeypatch.setenv("ARIA_SHIPPER_FEEDS", str(tmp_path / "feeds.json"))
        (tmp_path / "feeds.json").write_text(json.dumps(_shared_export(tmp_path)))
        if bound:
            bot._conversation_store().bind_owner("feishu:oc_acme", "ACME", bound_by="t")
        out = {"text": [], "cards": []}

        async def reply_text(message_id, text):
            out["text"].append(text)

        async def reply_card(message_id, title, body, *args, **kwargs):
            out["cards"].append((title, body))

        event = {"header": {"event_type": "im.message.receive_v1"}, "event": {
            "sender": {"sender_id": {"open_id": "ou_ops"}},
            "message": {"message_id": "om_1", "chat_id": "oc_acme", "chat_type": "group",
                        "message_type": "text", "content": json.dumps({"text": "@_user_1 /digest"}),
                        "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}]}}}

        async def go():
            with mock.patch.object(bot, "reply_text", reply_text), \
                 mock.patch.object(bot, "reply_card", reply_card):
                await bot.dispatch_event(event)
                for _ in range(3):
                    await asyncio.sleep(0)

        asyncio.run(go())
        return out

    def test_in_a_bound_group_it_replies_with_the_digest(self, monkeypatch, tmp_path):
        out = self._run(monkeypatch, tmp_path, bound=True)
        assert len(out["cards"]) == 1 and "A100" in out["cards"][0][1]

    def test_in_an_unbound_group_it_says_how_to_bind(self, monkeypatch, tmp_path):
        out = self._run(monkeypatch, tmp_path, bound=False)
        assert out["cards"] == [] and "/owner" in out["text"][0]
