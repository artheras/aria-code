"""Transaction points: the whole session state before each turn.

``/rewind`` used to restore files, and ``/rewind both`` then dropped the last
exchange, so rewinding three turns of edits left the conversation one turn
back and every approval granted since still in force. A transaction point
records, before each turn, everything that turn could change:

* the conversation, in full;
* the task list the model was keeping;
* the approvals in force (allow-all, per-tool, per-command-prefix);
* the newest file checkpoint, so every edit after it can be undone;
* whether the code at that point had passed its checks;
* the open task worktree and its unapplied diff.

Rewinding to a point undoes every file change recorded after it, then puts
the rest back as it was. Points live in ``<root>/<session_id>.jsonl``, the
newest ``keep_per_session`` kept, each storing only the messages its turn's
predecessor did not have.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

TRANSACTION_SCHEMA = "aria.transaction_point.v1"
DEFAULT_KEEP_PER_SESSION = 50
DEFAULT_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024

# What the code was known to be at a point: checks ran green, ran red, files
# changed with nothing to check them, or nothing has changed yet.
PASSED, FAILED, UNVERIFIED, UNCHANGED = "passed", "failed", "unverified", ""
# Red, but only where it was red before the change (runtime/baseline.py).
PREEXISTING = "preexisting"


@dataclass
class TransactionPoint:
    point_id: str
    session_id: str
    turn: int
    prompt: str                      # the message the turn after this point answered
    created_at: float
    messages: list = field(default_factory=list)
    todos: list = field(default_factory=list)
    approvals: dict = field(default_factory=dict)
    checkpoint_sequence: int = 0
    verdict: str = UNCHANGED
    task: Optional[dict] = None      # {task_id, repository, path, patch}
    truncated: bool = False

    def to_dict(self) -> dict:
        return {"schema": TRANSACTION_SCHEMA, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict) -> "TransactionPoint":
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in data.items() if key in fields})


def verdict_from_acceptance(acceptance: Any, previous: str) -> str:
    """The code's state after a turn, from the acceptance summary it carried.

    A turn that changed nothing leaves the previous verdict standing.
    """
    if not isinstance(acceptance, dict) or not acceptance.get("paths"):
        return previous
    verified = acceptance.get("verified")
    if verified is True:
        return PASSED
    if verified is False:
        return PREEXISTING if acceptance.get("regressions") is False else FAILED
    return UNVERIFIED


class TransactionStore:
    """Points per session, appended one line per turn.

    Consecutive points usually share the conversation up to the newer one's
    last few messages, so a point whose conversation extends the previous
    one's stores only the messages added since (``delta``). A compaction or a
    rewind breaks the chain and the next point is stored in full. The file is
    rewritten only to drop old points, once it holds twice as many as kept.
    """

    def __init__(self, root: Path | str, *, keep_per_session: int = DEFAULT_KEEP_PER_SESSION,
                 max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES) -> None:
        self.root = Path(root).expanduser()
        self.keep_per_session = max(1, int(keep_per_session))
        self.max_snapshot_bytes = max(64 * 1024, int(max_snapshot_bytes))

    def _path(self, session_id: str) -> Path:
        safe = "".join(ch for ch in str(session_id) if ch.isalnum() or ch in "-_") or "default"
        return self.root / f"{safe}.jsonl"

    def _lines(self, session_id: str) -> list[dict]:
        try:
            raw = self._path(session_id).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records = []
        for line in raw:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("schema") == TRANSACTION_SCHEMA:
                records.append(data)
        return records

    def _read(self, session_id: str) -> list[TransactionPoint]:
        """Oldest first, each with its whole conversation."""
        points: list[TransactionPoint] = []
        for data in self._lines(session_id):
            delta = data.pop("delta", None)
            try:
                point = TransactionPoint.from_dict(data)
            except TypeError:
                continue
            if delta is not None:
                if not points or points[-1].point_id != delta.get("base"):
                    continue  # its base was dropped: unrecoverable, skip it
                prefix = points[-1].messages[: int(delta.get("start") or 0)]
                point.messages = prefix + list(delta.get("messages") or [])
                point.truncated = point.truncated or points[-1].truncated
            points.append(point)
        return points

    @staticmethod
    def _encode(point: TransactionPoint, previous: Optional[TransactionPoint]) -> dict:
        data = point.to_dict()
        if previous is not None:
            start = len(previous.messages)
            if start and point.messages[:start] == previous.messages:
                data.pop("messages")
                data["delta"] = {"base": previous.point_id, "start": start,
                                 "messages": point.messages[start:]}
        return data

    def _rewrite(self, session_id: str, points: list[TransactionPoint]) -> None:
        path = self._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        previous = None
        with temp.open("w", encoding="utf-8") as handle:
            for point in points[-self.keep_per_session:]:
                handle.write(json.dumps(self._encode(point, previous), ensure_ascii=False, default=str) + "\n")
                previous = point
        os.replace(temp, path)

    def _fit(self, point: TransactionPoint) -> TransactionPoint:
        """Drop the oldest messages until the snapshot is under the byte cap."""
        messages = list(point.messages)
        while messages and len(json.dumps(messages, ensure_ascii=False, default=str)) > self.max_snapshot_bytes:
            messages.pop(0)
            point.truncated = True
        point.messages = messages
        return point

    def record(self, point: TransactionPoint) -> TransactionPoint:
        # Round-trip through JSON so the stored and the compared forms agree.
        point.messages = json.loads(json.dumps(point.messages, ensure_ascii=False, default=str))
        point = self._fit(point)
        points = self._read(point.session_id)
        if len(points) + 1 > 2 * self.keep_per_session:
            self._rewrite(point.session_id, points + [point])
            return point
        path = self._path(point.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(self._encode(point, points[-1] if points else None), ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return point

    def list(self, session_id: str) -> list[TransactionPoint]:
        """Newest first, at most ``keep_per_session``."""
        return list(reversed(self._read(session_id)[-self.keep_per_session:]))

    def find(self, session_id: str, ref: str) -> Optional[TransactionPoint]:
        """By id (or its prefix), or by count: ``"1"`` is the point before the last turn."""
        points = self.list(session_id)
        ref = str(ref or "1").strip()
        if ref.isdigit() and len(ref) <= 3:
            index = int(ref) - 1
            return points[index] if 0 <= index < len(points) else None
        matches = [point for point in points if point.point_id.startswith(ref)]
        return matches[0] if len(matches) == 1 else None

    def latest_green(self, session_id: str) -> Optional[TransactionPoint]:
        return next((point for point in self.list(session_id) if point.verdict == PASSED), None)

    def drop_from(self, point: TransactionPoint) -> None:
        """Forget ``point`` and every later one: after a rewind they are not history."""
        kept = []
        for candidate in self._read(point.session_id):
            if candidate.point_id == point.point_id:
                break
            kept.append(candidate)
        self._rewrite(point.session_id, kept)


def new_point(*, session_id: str, turn: int, prompt: str, **state: Any) -> TransactionPoint:
    return TransactionPoint(point_id=uuid.uuid4().hex[:10], session_id=session_id, turn=turn,
                            prompt=" ".join(str(prompt or "").split())[:120],
                            created_at=time.time(), **state)


def default_transaction_store() -> TransactionStore:
    from ..packages.aria_core.paths import aria_home

    return TransactionStore(aria_home() / "transactions")
