"""The daily shipper digest: what needs attention, pushed to the shipper's group.

A bot that only answers questions waits to be asked about the SKU that ran out
yesterday. This sends each shipper's group, unprompted, what the 3PL would
otherwise find out too late: what to reorder, what history is too thin to
plan from, what stock has stopped moving, which waybills to query with the
carrier, and where a cheaper carrier is at least as reliable.

Platform-neutral like the rest of apps/channels: it walks the conversations
bound to a shipper (ConversationStore) and hands each digest to that
channel's sender. Feishu is the first sender; another channel registers one.

Where the data comes from is the operator's decision, not the chat's:
ARIA_SHIPPER_FEEDS names a JSON file mapping each shipper to its exports —

    {"ACME": {"inventory": "/data/acme/skus.csv", "waybills": "/data/acme/waybills.csv"},
     "*":    {"inventory": "/data/wms/all_skus.csv"}}

"*" is the fallback for shippers without their own entry, typically one
multi-shipper export. Letting a chat command set a path would let anyone in
the group read any CSV on the host.

Every number comes from plan_inventory_policy and score_carriers, run with
ARIA_OWNER_SCOPE set to the shipper — the same tool-level confinement a bound
conversation gets — so a shared multi-shipper export yields only that
shipper's lines, and a file without owner_id is refused rather than guessed.
Digests are built one at a time: the scope is process environment, and the
tools are synchronous, so nothing else runs while it is set.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, Iterator, List, Optional

from aria_bots.channels.conversation import ConversationStore
from aria_bots.logistics.tenancy import OWNER_SCOPE_ENV

logger = logging.getLogger(__name__)

FEEDS_ENV = "ARIA_SHIPPER_FEEDS"
MAX_LINES = 8  # per section; a digest is a prompt to look, not the report

# (conversation_id, title, body) -> delivered?
Sender = Callable[[str, str, str], Awaitable[bool]]


@dataclass
class Digest:
    owner_id: str
    title: str
    body: str = ""
    items: int = 0
    problems: List[str] = field(default_factory=list)

    @property
    def worth_sending(self) -> bool:
        return self.items > 0


def load_feeds(path: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """The operator's shipper → export files map; {} when not configured."""
    source = path or os.environ.get(FEEDS_ENV, "").strip()
    if not source:
        return {}
    data = json.loads(Path(source).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{FEEDS_ENV} must be a JSON object of shipper -> feeds")
    return {str(k): {kind: str(v) for kind, v in (feeds or {}).items()} for k, feeds in data.items()}


def feeds_for(owner_id: str, feeds: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    return feeds.get(owner_id) or feeds.get("*") or {}


@contextmanager
def _confined_to(owner_id: str) -> Iterator[None]:
    previous = os.environ.get(OWNER_SCOPE_ENV)
    os.environ[OWNER_SCOPE_ENV] = owner_id
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(OWNER_SCOPE_ENV, None)
        else:
            os.environ[OWNER_SCOPE_ENV] = previous


def _section(title: str, lines: List[str]) -> str:
    shown = lines[:MAX_LINES]
    more = f"\n- …另有 {len(lines) - MAX_LINES} 项" if len(lines) > MAX_LINES else ""
    return f"**{title}（{len(lines)}）**\n" + "\n".join(f"- {line}" for line in shown) + more


def build_digest(owner_id: str, feeds: Dict[str, Dict[str, str]]) -> Digest:
    from aria_bots.logistics.carriers import tool_score_carriers
    from aria_bots.logistics.inventory import tool_plan_inventory_policy

    digest = Digest(owner_id=owner_id, title=f"📦 {owner_id} 今日库存与运费提醒")
    sources = feeds_for(owner_id, feeds)
    sections: List[str] = []

    with _confined_to(owner_id):
        if sources.get("inventory"):
            result = tool_plan_inventory_policy({"file_path": sources["inventory"]})
            if result.get("success"):
                items = result["data"]["items"]
                reorder = [i for i in items if i.get("action") == "reorder"]
                thin = [i for i in items if i.get("action") == "insufficient_history"]
                dead = [i for i in items if i.get("movement") == "dead"]
                if reorder:
                    sections.append(_section("建议补货", [
                        f"{i['sku']}：订 {i['suggested_order_qty']}（现有可用 {i['inventory_position']:g}，"
                        f"补货点 {i['reorder_point']}，约可撑 {i['days_of_cover']} 天）" for i in reorder])
                        + "\n在群里 @Aria 发送 /补货，可发起采购单草稿审批。")
                if thin:
                    sections.append(_section("历史不足、未给建议", [
                        f"{i['sku']}：仅 {i['history_days']} 天出库记录" for i in thin]))
                if dead:
                    sections.append(_section("呆滞库存", [
                        f"{i['sku']}：在库 {i['on_hand']:g}" for i in dead]))
                digest.items += len(reorder) + len(thin) + len(dead)
            else:
                digest.problems.append(f"inventory: {result.get('error')}")

        if sources.get("waybills"):
            result = tool_score_carriers({"file_path": sources["waybills"]})
            if result.get("success"):
                data = result["data"]
                anomalies, savings = data.get("anomalies") or [], data.get("savings") or []
                if anomalies:
                    sections.append(_section("运费待核对", [
                        f"{a['waybill_no']}（{a['carrier']} {a['lane']}）：{a['detail']}" for a in anomalies]))
                if savings:
                    sections.append(_section("可节省的线路", [
                        f"{s['lane']}：{s['from_carrier']} → {s['to_carrier']}，"
                        f"按近期运量约省 {s['estimated_saving']:g}（准时率不低于现承运商）" for s in savings]))
                digest.items += len(anomalies) + len(savings)
            else:
                digest.problems.append(f"waybills: {result.get('error')}")

    if not sources:
        digest.problems.append(f"no feeds configured for {owner_id}")
    digest.body = "\n\n".join(sections) + (
        "\n\n建议数量未计入起订量、箱规和供应商条款；运费异常请先与承运商核对再下结论。" if sections else "")
    return digest


async def run_digests(store: ConversationStore, senders: Dict[str, Sender],
                      feeds: Optional[Dict[str, Dict[str, str]]] = None) -> List[Dict[str, str]]:
    """Send today's digest to every bound conversation that has something to say.

    Returns one record per conversation for the log. Problems — a missing
    feed, a file the tenancy check refused — go to the log only: a client's
    group is not where a 3PL's configuration errors belong.
    """
    feeds = load_feeds() if feeds is None else feeds
    report: List[Dict[str, str]] = []
    built: Dict[str, Digest] = {}
    for conversation, owner_id in store.bindings():
        channel, _, conversation_id = conversation.partition(":")
        send = senders.get(channel)
        if send is None:
            report.append({"conversation": conversation, "status": "no_sender"})
            continue
        if owner_id not in built:
            try:
                built[owner_id] = build_digest(owner_id, feeds)
            except Exception as exc:  # one bad feed must not stop the others
                logger.warning("digest for %s failed: %s", owner_id, exc)
                report.append({"conversation": conversation, "status": "error"})
                continue
        digest = built[owner_id]
        for problem in digest.problems:
            logger.warning("digest %s: %s", owner_id, problem)
        if not digest.worth_sending:
            report.append({"conversation": conversation, "status": "nothing_to_report"})
            continue
        delivered = await send(conversation_id, digest.title, digest.body)
        report.append({"conversation": conversation, "status": "sent" if delivered else "send_failed"})
    return report
