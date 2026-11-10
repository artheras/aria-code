"""Approvals: a bot may propose an action with consequences, a person decides.

Bot runs cannot use confirmation-required tools, because nobody in a chat can
answer a terminal prompt. That stops harm and also stops the useful half of
the job — a reorder list someone still has to retype. This is the other way
through: the bot writes down exactly what it proposes, an authorised person
approves or rejects it in the chat, and only then does it happen, with a
record of who decided what and when.

Platform-neutral like the rest of apps/channels. A channel renders a request
with approve/reject buttons and calls `decide` when one is pressed; the rules
below hold whichever platform the button was on:

  - Who may decide: ARIA_CHANNEL_APPROVERS, plus ARIA_CHANNEL_ADMINS, per
    channel ("feishu:ou_x"). Nobody, when neither is set.
  - Where: only in the conversation the request was raised in. A card
    forwarded to another chat cannot be approved there.
  - Once: the transition out of "pending" is a single conditional UPDATE, so
    two clicks, or two approvers at the same moment, cannot both execute.
  - Not late: a request expires after `ttl_seconds` (24 h).
  - Not for someone else: the conversation must still be bound to the shipper
    the request was made for, and the action runs under ARIA_OWNER_SCOPE.
  - What was approved is what runs: the payload is frozen at request time and
    executed as stored, never recomputed — the approver saw these lines.

Actions are registered in EXECUTORS. The first, "purchase_order_draft", writes
a CSV draft to an outbox directory (ARIA_APPROVAL_OUTBOX) for the 3PL's own
purchasing system to pick up. Aria does not place orders: it has no
integration that could, and pretending otherwise would be the wrong default.
"""

from __future__ import annotations

import csv
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from aria_bots.channels.conversation import ADMINS_ENV, default_db_path
from aria_bots.logistics.tenancy import OWNER_SCOPE_ENV

APPROVERS_ENV = "ARIA_CHANNEL_APPROVERS"
OUTBOX_ENV = "ARIA_APPROVAL_OUTBOX"

PENDING, APPROVED, REJECTED, EXPIRED, FAILED = "pending", "approved", "rejected", "expired", "failed"


def can_decide(channel: str, sender_ids: Tuple[str, ...], env: Optional[Dict[str, str]] = None) -> bool:
    env = env if env is not None else os.environ
    allowed = set()
    for key in (APPROVERS_ENV, ADMINS_ENV):
        allowed |= {item.strip() for item in env.get(key, "").split(",") if item.strip()}
    return any(f"{channel}:{sid}" in allowed for sid in sender_ids if sid)


@dataclass(frozen=True)
class Approval:
    id: str
    conversation: str
    owner_id: str
    kind: str
    payload: Dict[str, Any]
    summary: str
    requested_by: str
    requested_at: float
    status: str
    decided_by: str = ""
    decided_at: float = 0.0
    result: str = ""


@dataclass(frozen=True)
class Decision:
    ok: bool
    message: str
    approval: Optional[Approval] = None


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


# ── Executors ────────────────────────────────────────────────────────────────

def outbox_dir() -> Path:
    configured = os.environ.get(OUTBOX_ENV, "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".aria" / "outbox"


def execute_purchase_order_draft(approval: Approval) -> str:
    """Write the approved lines, exactly as approved, as a CSV draft."""
    lines = approval.payload.get("lines") or []
    if not lines:
        raise ValueError("nothing to order")
    folder = outbox_dir() / approval.owner_id
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    stamp = time.strftime("%Y%m%d", time.localtime(approval.decided_at or time.time()))
    path = folder / f"PO-{approval.owner_id}-{stamp}-{approval.id[:8]}.csv"
    with path.open("x", newline="", encoding="utf-8") as handle:  # never overwrite a draft
        writer = csv.writer(handle)
        writer.writerow(["owner_id", "sku", "order_qty", "approval_id", "approved_by", "approved_at"])
        decided = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(approval.decided_at))
        for line in lines:
            writer.writerow([approval.owner_id, line["sku"], line["qty"], approval.id,
                             approval.decided_by, decided])
    return str(path)


EXECUTORS: Dict[str, Callable[[Approval], str]] = {
    "purchase_order_draft": execute_purchase_order_draft,
}


def propose_reorder(owner_id: str, inventory_path: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """A purchase-order draft from the shipper's reorder list, or None if nothing to order."""
    from aria_bots.logistics.inventory import tool_plan_inventory_policy

    with _confined_to(owner_id):
        result = tool_plan_inventory_policy({"file_path": inventory_path})
    if not result.get("success"):
        raise ValueError(result.get("error") or "inventory analysis failed")
    lines = [{"sku": i["sku"], "qty": int(i["suggested_order_qty"])}
             for i in result["data"]["items"]
             if i.get("action") == "reorder" and i.get("suggested_order_qty")]
    if not lines:
        return None
    summary = "\n".join(f"- {line['sku']}：{line['qty']}" for line in lines)
    return summary, {"lines": lines}


# ── Store ────────────────────────────────────────────────────────────────────

class ApprovalStore:
    """Requests and decisions, in the same SQLite file as the conversations."""

    def __init__(self, path: Optional[Path | str] = None, *, ttl_seconds: float = 24 * 3600,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        with self._db() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, conversation TEXT NOT NULL, owner_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, summary TEXT NOT NULL,
                    requested_by TEXT NOT NULL, requested_at REAL NOT NULL,
                    status TEXT NOT NULL, decided_by TEXT NOT NULL DEFAULT '',
                    decided_at REAL NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '')"""
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def request(self, *, conversation: str, owner_id: str, kind: str, payload: Dict[str, Any],
                summary: str, requested_by: str) -> Approval:
        if kind not in EXECUTORS:
            raise ValueError(f"unknown action {kind!r}")
        approval_id = secrets.token_urlsafe(12)  # unguessable: it travels in a card button
        with self._db() as db:
            db.execute(
                "INSERT INTO approvals (id, conversation, owner_id, kind, payload, summary, "
                "requested_by, requested_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (approval_id, conversation, owner_id, kind, json.dumps(payload, ensure_ascii=False),
                 summary, requested_by, self._clock(), PENDING),
            )
        return self.get(approval_id)  # type: ignore[return-value]

    def get(self, approval_id: str) -> Optional[Approval]:
        with self._db() as db:
            row = db.execute(
                "SELECT id, conversation, owner_id, kind, payload, summary, requested_by, requested_at, "
                "status, decided_by, decided_at, result FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if not row:
            return None
        values = list(row)
        values[4] = json.loads(values[4])
        return Approval(*values)

    def recent(self, conversation: str, limit: int = 5) -> List[Approval]:
        with self._db() as db:
            ids = [r[0] for r in db.execute(
                "SELECT id FROM approvals WHERE conversation = ? ORDER BY requested_at DESC LIMIT ?",
                (conversation, limit))]
        return [a for a in (self.get(i) for i in ids) if a]

    def _transition(self, approval_id: str, status: str, by: str, result: str = "",
                    expect: str = PENDING) -> bool:
        with self._db() as db:
            cursor = db.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, result = ? "
                "WHERE id = ? AND status = ?",
                (status, by, self._clock(), result, approval_id, expect),
            )
            return cursor.rowcount == 1

    def decide(self, approval_id: str, *, conversation: str, current_owner: Optional[str],
               decided_by: str, approve: bool, authorised: bool) -> Decision:
        """Apply one approve/reject press. Every refusal says why, and changes nothing."""
        approval = self.get(approval_id)
        if approval is None or approval.conversation != conversation:
            # The same answer for "no such request" and "not this chat's": a
            # forwarded card learns nothing about where it came from.
            return Decision(False, "找不到这条审批，或它不属于本会话。")
        if not authorised:
            return Decision(False, "你没有审批权限。", approval)
        if approval.status != PENDING:
            who = f"（{approval.decided_by}）" if approval.decided_by else ""
            return Decision(False, f"这条审批已经是「{_STATUS_LABEL.get(approval.status, approval.status)}」{who}。", approval)
        if self._clock() - approval.requested_at > self.ttl_seconds:
            self._transition(approval_id, EXPIRED, "system")
            return Decision(False, "这条审批已过期，请重新发起。", self.get(approval_id))
        if current_owner != approval.owner_id:
            return Decision(False, "本会话绑定的货主已变更，这条审批不能再执行。", approval)

        if not approve:
            if not self._transition(approval_id, REJECTED, decided_by):
                return Decision(False, "这条审批刚刚已被处理。", self.get(approval_id))
            return Decision(True, "已驳回。", self.get(approval_id))

        # Claim it first; only the press that wins the claim executes.
        if not self._transition(approval_id, APPROVED, decided_by, result="executing"):
            return Decision(False, "这条审批刚刚已被处理。", self.get(approval_id))
        claimed = self.get(approval_id)
        try:
            with _confined_to(approval.owner_id):
                outcome = EXECUTORS[approval.kind](claimed)  # type: ignore[arg-type]
        except Exception as exc:
            # The reason is kept in the record for the operator; the chat only
            # learns that it failed, since an exception can name host paths.
            self._transition(approval_id, FAILED, decided_by, result=str(exc)[:500], expect=APPROVED)
            return Decision(False, "已批准，但执行失败，原因已记录。", self.get(approval_id))
        with self._db() as db:
            db.execute("UPDATE approvals SET result = ? WHERE id = ?", (outcome, approval_id))
        return Decision(True, "已批准并执行。", self.get(approval_id))


_STATUS_LABEL = {PENDING: "待审批", APPROVED: "已批准", REJECTED: "已驳回", EXPIRED: "已过期", FAILED: "执行失败"}


def status_label(status: str) -> str:
    return _STATUS_LABEL.get(status, status)
