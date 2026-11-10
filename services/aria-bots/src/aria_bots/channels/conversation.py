"""The platform-neutral half of every chat channel.

A chat bot does the same five things on any platform: decide whether a
message is for it, know which client a conversation belongs to, remember what
was just said there, keep each conversation's memory to itself, and hand the
model a turn. Only parsing the platform's event and sending its reply differ.
This module is the part that does not differ, so Feishu, Telegram, and a bot
on Arthera's own app or website share one implementation of the rules that
matter — and a new channel is an adapter of a few dozen lines:

    msg = InboundMessage(channel="myapp", conversation_id=room_id, kind="group",
                         sender_id=user_id, text=text, mentions_bot=True)
    if not should_respond(msg):
        return
    turn = prepare_turn(msg, store)
    reply = await run_my_model(turn.prompt, history=turn.history, env=turn.env)
    store.record(msg.key, "assistant", reply)

The guarantees, each tested in tests/test_conversation_core.py:

  - In a group the bot answers only when addressed; in a direct chat, always.
  - A conversation bound to a shipper (货主) runs every turn with
    ARIA_OWNER_SCOPE set, which the logistics tools enforce themselves
    (tools/logistics_tenancy.py) — the binding does not rely on the prompt.
  - Only channel admins (ARIA_CHANNEL_ADMINS) can bind or unbind.
  - History is per conversation and never crosses: the context for a turn is
    built from that conversation's own messages and nothing else.
  - History is bounded twice: the last `max_turns` messages, none older than
    `ttl_seconds`. A chat bot that keeps every client message forever is a
    liability, not a feature.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from aria_bots.channels.registry import CHANNEL_TASK_SCHEMA
from aria_bots.logistics.tenancy import OWNER_SCOPE_ENV

ADMINS_ENV = "ARIA_CHANNEL_ADMINS"
DB_ENV = "ARIA_CONVERSATIONS_DB"
_OWNER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


@dataclass(frozen=True)
class InboundMessage:
    """One message, as any channel adapter normalises it."""

    channel: str                 # "feishu", "telegram", "web", ...
    conversation_id: str         # the chat / group / room on that channel
    kind: str                    # "direct" or "group"
    sender_id: str
    text: str
    message_id: str = ""
    mentions_bot: bool = False
    reply_to: str = ""           # the message this one answers, if any
    sender_aliases: Tuple[str, ...] = ()   # other ids for the same person

    @property
    def key(self) -> str:
        """Unique across channels: two platforms may reuse the same chat id."""
        return f"{self.channel}:{self.conversation_id}"

    @property
    def sender_ids(self) -> Tuple[str, ...]:
        return tuple(i for i in (self.sender_id, *self.sender_aliases) if i)


def should_respond(msg: InboundMessage) -> bool:
    """Direct chats always; groups only when the bot is addressed."""
    if msg.kind == "direct":
        return True
    return bool(msg.mentions_bot)


def is_admin(msg: InboundMessage, env: Optional[Dict[str, str]] = None) -> bool:
    """ARIA_CHANNEL_ADMINS="feishu:ou_abc,telegram:12345" — per channel, fail closed."""
    raw = (env if env is not None else os.environ).get(ADMINS_ENV, "")
    admins = {item.strip() for item in raw.split(",") if item.strip()}
    return any(f"{msg.channel}:{sid}" in admins for sid in msg.sender_ids)


# ── Admin commands ───────────────────────────────────────────────────────────

_OWNER_COMMAND = re.compile(r"^/(?:owner|货主)(?:\s+(\S+))?\s*$", re.IGNORECASE)
_UNBIND_WORDS = {"off", "clear", "none", "解除", "取消"}


def parse_owner_command(text: str) -> Optional[Tuple[str, str]]:
    """'/owner' → show; '/owner ACME' → bind; '/owner off' → unbind. None otherwise."""
    match = _OWNER_COMMAND.match((text or "").strip())
    if not match:
        return None
    arg = (match.group(1) or "").strip()
    if not arg:
        return ("show", "")
    if arg.lower() in _UNBIND_WORDS:
        return ("unbind", "")
    return ("bind", arg)


def handle_owner_command(msg: InboundMessage, store: "ConversationStore",
                         env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Run an /owner command and return the reply text, or None if it is not one."""
    command = parse_owner_command(msg.text)
    if command is None:
        return None
    action, owner = command
    current = store.owner_for(msg.key)
    if action == "show":
        return (f"本会话绑定的货主：{current}" if current
                else "本会话未绑定货主。管理员可发送 /owner <货主ID> 绑定。")
    if not is_admin(msg, env):
        return "只有管理员可以修改本会话绑定的货主。"
    if msg.kind != "group":
        # A direct chat is one person, not a client; binding it would confine
        # an operator's own chat to one shipper and surprise them later.
        return "货主只能绑定到群聊：一个群对应一个货主。"
    if action == "unbind":
        store.unbind_owner(msg.key)
        return "已解除本会话的货主绑定。"
    if not _OWNER_ID.match(owner):
        return "货主 ID 只能包含字母、数字、下划线、点和短横线，最长 64 个字符。"
    store.bind_owner(msg.key, owner, bound_by=f"{msg.channel}:{msg.sender_id}")
    return f"已绑定：本会话之后的分析只会使用货主 {owner} 的数据。"


# ── Storage ──────────────────────────────────────────────────────────────────

def default_db_path() -> Path:
    configured = os.environ.get(DB_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".aria" / "conversations.db"


class ConversationStore:
    """Owner bindings and bounded per-conversation history, in one SQLite file."""

    def __init__(self, path: Optional[Path | str] = None, *, max_turns: int = 20,
                 ttl_seconds: float = 7 * 86400, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_turns = max(1, int(max_turns))
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS bindings (
                    conversation TEXT PRIMARY KEY,
                    owner_id     TEXT NOT NULL,
                    bound_by     TEXT NOT NULL,
                    bound_at     REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation TEXT NOT NULL,
                    role         TEXT NOT NULL,
                    sender       TEXT NOT NULL DEFAULT '',
                    content      TEXT NOT NULL,
                    at           REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS history_by_conversation ON history (conversation, id);
                """
            )
        try:
            os.chmod(self.path, 0o600)  # client messages: owner-readable only
        except OSError:
            pass

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    # bindings
    def bind_owner(self, key: str, owner_id: str, *, bound_by: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO bindings (conversation, owner_id, bound_by, bound_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(conversation) DO UPDATE SET owner_id=excluded.owner_id, "
                "bound_by=excluded.bound_by, bound_at=excluded.bound_at",
                (key, owner_id, bound_by, self._clock()),
            )

    def unbind_owner(self, key: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM bindings WHERE conversation = ?", (key,))

    def bindings(self) -> List[Tuple[str, str]]:
        """Every (conversation key, shipper) binding, for jobs that push to them."""
        with self._db() as db:
            return [tuple(row) for row in db.execute(
                "SELECT conversation, owner_id FROM bindings ORDER BY conversation")]

    def owner_for(self, key: str) -> Optional[str]:
        with self._db() as db:
            row = db.execute("SELECT owner_id FROM bindings WHERE conversation = ?", (key,)).fetchone()
        return row[0] if row else None

    # history
    def record(self, key: str, role: str, content: str, *, sender: str = "") -> None:
        if role not in ("user", "assistant") or not (content or "").strip():
            return
        now = self._clock()
        with self._db() as db:
            db.execute("INSERT INTO history (conversation, role, sender, content, at) VALUES (?, ?, ?, ?, ?)",
                       (key, role, sender, content, now))
            self._prune(db, key, now)

    def history(self, key: str) -> List[Dict[str, str]]:
        """Oldest first, in the {role, content} shape model providers take."""
        now = self._clock()
        with self._db() as db:
            self._prune(db, key, now)
            rows = db.execute(
                "SELECT role, content FROM history WHERE conversation = ? ORDER BY id", (key,)
            ).fetchall()
        return [{"role": role, "content": content} for role, content in rows]

    def forget(self, key: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM history WHERE conversation = ?", (key,))

    def _prune(self, db: sqlite3.Connection, key: str, now: float) -> None:
        db.execute("DELETE FROM history WHERE conversation = ? AND at < ?", (key, now - self.ttl_seconds))
        db.execute(
            "DELETE FROM history WHERE conversation = ? AND id NOT IN "
            "(SELECT id FROM history WHERE conversation = ? ORDER BY id DESC LIMIT ?)",
            (key, key, self.max_turns),
        )


# ── Turns ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChannelTurn:
    """Everything a model call needs for one message, platform-free."""

    prompt: str
    history: List[Dict[str, str]] = field(default_factory=list)
    owner_scope: Optional[str] = None

    @property
    def env(self) -> Dict[str, str]:
        """Environment for a subprocess running this turn's tools."""
        return {OWNER_SCOPE_ENV: self.owner_scope} if self.owner_scope else {}

    def as_task(self, msg: InboundMessage) -> Dict[str, object]:
        """The same turn as a structured channel task (aria.channel_task.v1)."""
        return {
            "schema": CHANNEL_TASK_SCHEMA,
            "channel": msg.channel,
            "conversation": msg.key,
            "message_id": msg.message_id,
            "prompt": self.prompt,
            "history": list(self.history),
            "owner_scope": self.owner_scope,
        }


def prepare_turn(msg: InboundMessage, store: ConversationStore) -> ChannelTurn:
    """Record the incoming message and build the turn that answers it.

    History is read before this message is recorded, so the turn carries the
    conversation so far plus the new prompt — not the prompt twice.
    """
    history = store.history(msg.key)
    owner = store.owner_for(msg.key)
    store.record(msg.key, "user", msg.text, sender=msg.sender_id)
    prompt = msg.text
    if owner:
        prompt = (f"[本会话属于货主 {owner}。只分析该货主的数据，回答中不要提及其他货主。]\n"
                  f"{msg.text}")
    return ChannelTurn(prompt=prompt, history=history, owner_scope=owner)
