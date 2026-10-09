"""The REPL's transaction points: take one before each turn, rewind to any.

``runtime/transactions.py`` stores points; this reads and writes the state a
point covers, which lives in the terminal and in CLI module globals:

* the conversation — ``terminal.conversation``;
* approvals — ``_auto_approve_session``, ``_session_always_allow`` and
  ``_session_command_prefixes`` in the module the terminal was built in
  (aria_cli rebinds the approval functions onto its own globals, so that copy
  is the one in force, not tool_executor's);
* the task list — ``aria_code.apps.cli.todo_tracker``, the copy the
  ``update_todos`` tool writes;
* background tasks — ``runtime.subagent``, the copy the CLI registers;
* the task worktree — ``apps.cli.task_isolation``.

Files are undone first: a conflict there stops the rewind before anything
else has changed.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from aria_code.runtime.transactions import (
    TransactionPoint,
    TransactionStore,
    default_transaction_store,
    new_point,
)

logger = logging.getLogger(__name__)

_STORE: list[Optional[TransactionStore]] = [None]


def store() -> TransactionStore:
    if _STORE[0] is None:
        _STORE[0] = default_transaction_store()
    return _STORE[0]


def reset_for_tests(replacement: Any = None) -> None:
    _STORE[0] = replacement


def _namespace(terminal: Any) -> dict:
    return vars(sys.modules[type(terminal).__module__])


def _todos():
    from aria_code.apps.cli import todo_tracker
    return todo_tracker


def _checkpoints():
    # The aria_code.* copy, as the write tools record with: a conflict raised
    # from it is the class callers catch.
    from aria_code.runtime.checkpoints import CheckpointStore
    return CheckpointStore()


def _approvals(namespace: dict) -> dict:
    return {
        "allow_all": bool(namespace.get("_auto_approve_session", False)),
        "tools": sorted(namespace.get("_session_always_allow") or ()),
        "prefixes": sorted(list(prefix) for prefix in namespace.get("_session_command_prefixes") or ()),
    }


def _open_task(terminal: Any) -> Optional[dict]:
    try:
        from apps.cli import task_isolation

        tasks = task_isolation.manager()
        task = tasks.active(task_isolation._workspace(terminal.config))
        if task is None:
            return None
        return {"task_id": task.task_id, "repository": task.repository, "path": task.path,
                "patch": tasks.patch(task)}
    except Exception as exc:
        logger.debug("task state not captured: %s", exc)
        return None


def capture(terminal: Any, prompt: str) -> Optional[TransactionPoint]:
    """Record the state before the turn ``prompt`` starts. Never raises."""
    try:
        session_id = str(getattr(terminal, "session_id", "") or "default")
        turn = sum(1 for message in terminal.conversation if message.get("role") == "user") + 1
        point = new_point(
            session_id=session_id, turn=turn, prompt=prompt,
            messages=list(terminal.conversation),
            todos=list(_todos().get_active_todos()),
            approvals=_approvals(_namespace(terminal)),
            checkpoint_sequence=_checkpoints().max_sequence(),
            verdict=str(getattr(terminal, "_code_verdict", "") or ""),
            task=_open_task(terminal),
        )
        return store().record(point)
    except Exception as exc:
        logger.debug("transaction point not recorded: %s", exc)
        return None


@dataclass
class RewindOutcome:
    point: TransactionPoint
    restored_paths: tuple = ()
    messages_removed: int = 0
    approvals_revoked: list = field(default_factory=list)
    tasks_cancelled: list = field(default_factory=list)
    task_note: str = ""

    def lines(self) -> list[str]:
        count = len(self.restored_paths)
        out = [f"Rewound to before turn {self.point.turn}: “{self.point.prompt}”"]
        out.append(f"  files         {count} restored" if count else "  files         no changes to undo")
        out.append(f"  conversation  {self.messages_removed} message{'s' if self.messages_removed != 1 else ''} removed")
        if self.approvals_revoked:
            out.append(f"  approvals     revoked {', '.join(self.approvals_revoked)}")
        if self.tasks_cancelled:
            out.append(f"  background    cancelled {', '.join(self.tasks_cancelled)}")
        if self.task_note:
            out.append(f"  task          {self.task_note}")
        if self.point.verdict:
            out.append(f"  checks        {self.point.verdict} at this point")
        if self.point.truncated:
            out.append("  note          the oldest messages were too large to keep and are not restored")
        return out


def _worktree_skip(terminal: Any):
    """Files in a task worktree that is gone have nowhere to be restored to."""
    try:
        from apps.cli import task_isolation
        root = task_isolation.manager().root.resolve()
    except Exception:
        return lambda _path: False

    def skip(path: str) -> bool:
        try:
            relative = Path(path).resolve().relative_to(root)
        except (ValueError, OSError):
            return False
        parts = relative.parts
        return len(parts) >= 2 and not (root / parts[0] / parts[1]).is_dir()

    return skip


def _restore_task(terminal: Any, point: TransactionPoint) -> str:
    saved = point.task
    if not saved or not str(saved.get("patch") or "").strip():
        return ""
    if Path(saved["path"]).is_dir():
        return ""  # still open: its files were restored with the rest
    from apps.cli import task_isolation

    tasks = task_isolation.manager()
    try:
        task = tasks.ensure(task_isolation._workspace(terminal.config))
        if task is None:
            raise RuntimeError("not a git repository")
        tasks.restore_patch(task, saved["patch"])
        return f"reopened as {task.task_id} with its unapplied changes"
    except Exception as exc:
        keep = Path(tasks.root) / f"rewound-{saved['task_id']}.patch"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text(saved["patch"], encoding="utf-8")
        return f"could not be reopened ({exc}); its changes are saved in {keep}"


def _cancel_later_tasks(terminal: Any, point: TransactionPoint) -> list[str]:
    try:
        from runtime.subagent import _TASKS, tool_task_cancel
    except Exception:
        return []
    session_id = str(getattr(terminal, "session_id", "") or "")
    cancelled = []
    for task in list(_TASKS.values()):
        if task.created_at <= point.created_at or task.status not in ("pending", "running"):
            continue
        if task.session_id and session_id and task.session_id != session_id:
            continue
        if tool_task_cancel({"task_id": task.task_id}).get("success"):
            cancelled.append(task.task_id)
    return cancelled


def rewind(terminal: Any, point: TransactionPoint) -> RewindOutcome:
    """Put the session back to ``point``. Raises if files cannot be restored."""
    session_id = str(getattr(terminal, "session_id", "") or "default")
    files = _checkpoints().restore_since(point.checkpoint_sequence, session_id=session_id,
                                         skip=_worktree_skip(terminal))
    outcome = RewindOutcome(point=point, restored_paths=files.restored_paths)
    outcome.task_note = _restore_task(terminal, point)

    outcome.messages_removed = max(0, len(terminal.conversation) - len(point.messages))
    terminal.conversation[:] = list(point.messages)

    todos = _todos()
    todos._ACTIVE_TODOS[:] = list(point.todos)

    namespace = _namespace(terminal)
    before = _approvals(namespace)
    saved = point.approvals or {}
    namespace["_auto_approve_session"] = bool(saved.get("allow_all", False))
    for key, values in (("_session_always_allow", saved.get("tools") or ()),
                        ("_session_command_prefixes", [tuple(p) for p in saved.get("prefixes") or ()])):
        current = namespace.get(key)
        if isinstance(current, set):
            current.clear()
            current.update(values)
    revoked = [tool for tool in before["tools"] if tool not in (saved.get("tools") or ())]
    revoked += [" ".join(prefix) for prefix in before["prefixes"] if prefix not in (saved.get("prefixes") or [])]
    if before["allow_all"] and not saved.get("allow_all"):
        revoked.insert(0, "allow-all")
    outcome.approvals_revoked = revoked

    outcome.tasks_cancelled = _cancel_later_tasks(terminal, point)
    terminal._code_verdict = point.verdict
    store().drop_from(point)
    return outcome


def describe(points: list[TransactionPoint], *, now: Optional[float] = None) -> list[str]:
    """``/rewind turns``: newest first, numbered as ``/rewind turn N`` takes them."""
    now = now or time.time()
    marks = {"passed": "✓", "failed": "✗", "unverified": "·", "": " "}
    lines = []
    for index, point in enumerate(points, 1):
        age = now - point.created_at
        when = f"{int(age)}s" if age < 60 else f"{int(age // 60)}m" if age < 3600 else f"{age / 3600:.1f}h"
        lines.append(f"  {index:>2}  {point.point_id[:8]}  {marks.get(point.verdict, ' ')}  "
                     f"{when:>5} ago  before turn {point.turn}: {point.prompt[:60]}")
    return lines
