"""The REPL's side of task worktrees: start one per task, offer to apply it.

``runtime/task_worktree.py`` does the git work; this decides when a turn runs
in a worktree, tells the executor where it is, and asks after a turn that
changed files whether those changes should reach the user's own copy.

Only the interactive REPL isolates. Headless ``-p`` runs keep editing in
place: a script that asked for a change expects to find it on disk when the
command exits, and there is nobody to ask.

``task_isolation: off`` in the config, or ``ARIA_TASK_ISOLATION=off`` in the
environment, turns it off.
"""

from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional

from aria_code.runtime.task_worktree import (
    TaskWorktree,
    TaskWorktreeError,
    TaskWorktrees,
    default_task_worktrees,
)

logger = logging.getLogger(__name__)

_READ_ONLY_MODES = frozenset({"read-only", "readonly", "read_only", "plan"})

_MANAGER: list[Optional[TaskWorktrees]] = [None]
_CLEANUP_REGISTERED = [False]


def manager() -> TaskWorktrees:
    if _MANAGER[0] is None:
        _MANAGER[0] = default_task_worktrees()
    return _MANAGER[0]


_OFF = {"off", "false", "none", "0"}


def isolation_enabled(config: dict) -> bool:
    setting = os.environ.get("ARIA_TASK_ISOLATION") or config.get("task_isolation", "worktree")
    if str(setting or "").strip().lower() in _OFF:
        return False
    return str(config.get("permission_mode", "workspace-write") or "") not in _READ_ONLY_MODES


def _workspace(config: dict) -> Path:
    return Path(config.get("_session_workspace_root") or os.getcwd()).expanduser().resolve()


def prepare(config: dict, *, notify: Callable[[str], None] = print) -> Optional[TaskWorktree]:
    """The worktree this turn runs in, or None to edit in place.

    Never raises: a repository git cannot snapshot falls back to editing in
    place with a one-line note, as before this existed.
    """
    if not isolation_enabled(config):
        return None
    tasks = manager()
    workspace = _workspace(config)
    try:
        before = tasks.active(workspace)
        task = tasks.ensure(workspace)
    except TaskWorktreeError as exc:
        logger.debug("task worktree unavailable: %s", exc)
        notify(f"Task isolation unavailable, editing your files directly: {exc}")
        return None
    if task is None:
        return None
    if before is None or before.task_id != task.task_id:
        _register_cleanup()
    return task


def execution_context(task: Optional[TaskWorktree], config: dict) -> dict:
    """What the executor needs to work in ``task`` instead of the workspace."""
    if task is None:
        return {}
    workspace = _workspace(config)
    origin = Path(task.repository)
    try:
        inside = workspace.relative_to(origin)
    except ValueError:
        inside = Path()
    target = Path(task.path) / inside
    # git does not carry empty or ignored directories; the session's own
    # directory has to exist for commands to run in it.
    target.mkdir(parents=True, exist_ok=True)
    return {"_workspace": str(target), "_workspace_origin": str(origin), "_task_base": task.base}


def summary_lines(task: TaskWorktree, changes) -> list[str]:
    lines = [f"Task {task.task_id} changed {len(changes)} file{'s' if len(changes) != 1 else ''} "
             "in its worktree; your files are unchanged."]
    for change in changes[:12]:
        lines.append(f"  {change.status} {change.path}")
    if len(changes) > 12:
        lines.append(f"  … and {len(changes) - 12} more")
    return lines


def apply(task: TaskWorktree, *, session_id: str = "") -> str:
    applied = manager().apply(task, session_id=session_id)
    return (f"Applied task {task.task_id}: {len(applied)} file{'s' if len(applied) != 1 else ''} "
            "changed in your workspace; /rewind list shows how to undo it.")


def discard(task: TaskWorktree) -> str:
    manager().discard(task)
    return f"Discarded task {task.task_id}; your files were not changed."


APPLY, KEEP, DISCARD = 0, 1, 2


def offer(task: Optional[TaskWorktree], *, choose: Callable[[list, str], int],
          say: Callable[[str], None], session_id: str = "") -> Optional[int]:
    """After a turn: if the task changed files, ask what to do with them.

    ``choose(options, title)`` returns an index, or -1 when cancelled, which
    keeps the task — nothing is applied or thrown away without an answer.
    """
    if task is None:
        return None
    try:
        changes = manager().changes(task)
    except TaskWorktreeError as exc:
        logger.debug("task changes unavailable: %s", exc)
        return None
    if not changes:
        return None
    for line in summary_lines(task, changes):
        say(line)
    options = [
        ("Apply to my files", "copy the changes into your workspace"),
        ("Keep working", "the next message continues in this task"),
        ("Discard", "drop the task's changes"),
    ]
    choice = choose(options, "Apply this task?")
    if choice == APPLY:
        try:
            say(apply(task, session_id=session_id))
        except TaskWorktreeError as exc:
            say(str(exc))
            return KEEP
        return APPLY
    if choice == DISCARD:
        say(discard(task))
        return DISCARD
    say(f"Kept task {task.task_id}. /task apply or /task discard when you are ready.")
    return KEEP


def command(args: str, config: dict, *, session_id: str = "") -> str:
    """``/task [status|diff|apply|discard]`` for the workspace's open task."""
    sub = (args.strip().split() or ["status"])[0].lower()
    tasks = manager()
    try:
        task = tasks.active(_workspace(config))
    except TaskWorktreeError as exc:
        return str(exc)
    if task is None:
        state = "on" if isolation_enabled(config) else "off (task_isolation: off)"
        return f"No open task. Task isolation is {state}."
    try:
        if sub == "apply":
            return apply(task, session_id=session_id)
        if sub == "discard":
            return discard(task)
        if sub == "diff":
            return tasks.diff(task) or "The task has no changes yet."
        changes = tasks.changes(task)
    except TaskWorktreeError as exc:
        return str(exc)
    if not changes:
        return f"Task {task.task_id} has no changes yet.\n  worktree {task.path}"
    lines = summary_lines(task, changes)
    lines += [f"  worktree {task.path}", "/task diff · /task apply · /task discard"]
    return "\n".join(lines)


def _register_cleanup() -> None:
    if _CLEANUP_REGISTERED[0]:
        return
    _CLEANUP_REGISTERED[0] = True
    atexit.register(_discard_unchanged)


def _discard_unchanged() -> None:
    """At exit, drop open tasks with nothing in them; keep any with changes."""
    tasks = _MANAGER[0]
    if tasks is None:
        return
    for record in list(tasks._load().values()):
        try:
            task = TaskWorktree.from_dict(record)
            if not Path(task.path).is_dir() or not tasks.changes(task):
                tasks.discard(task)
        except Exception:
            continue


def reset_for_tests(replacement: Any = None) -> None:
    _MANAGER[0] = replacement
