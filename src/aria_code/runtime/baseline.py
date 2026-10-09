"""Test baseline: was this check already failing before the change?

A red check sends the model back to repair. When the test was failing before
the task touched anything, that sends it to "fix" code unrelated to the
request — or to give up on a task that was actually done. The baseline runs
the failed command once more on the code as it was before the change and
compares:

* pytest: by test id (``FAILED tests/x.py::test_y``). Failures that also
  fail on the baseline are pre-existing; the rest are the change's.
* anything else: by exit status. A command that fails on the baseline too
  is pre-existing as a whole.

"Before the change" is the task's starting snapshot when the turn runs in a
task worktree. Otherwise it is the working tree now with this turn's edits
undone from their checkpoints — edits a shell command made are not
recorded, so they are in the baseline too.

It runs only after a failure, in a scratch worktree removed straight after,
and each command at most once per turn.
"""

from __future__ import annotations

import inspect
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

from .task_worktree import TaskWorktreeError, checkout, remove_checkout, repository_root, snapshot

logger = logging.getLogger(__name__)

_PYTEST_FAILURE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-\s|$)", re.MULTILINE)

# ``(command, cwd) -> run result dict``, as the acceptance gate's runner returns.
BaselineRunner = Callable[[str, str], Union[dict, Awaitable[dict]]]


def failing_tests(output: str) -> frozenset[str]:
    """pytest's failing test ids, from its short summary lines."""
    return frozenset(match.group(1) for match in _PYTEST_FAILURE.finditer(output or ""))


@dataclass(frozen=True)
class Comparison:
    preexisting: bool                    # every failure was already there
    new_failures: tuple[str, ...] = ()   # test ids failing only after the change
    old_failures: tuple[str, ...] = ()   # test ids failing before it too
    note: str = ""


def compare(after_output: str, after_failed: bool, before: Optional[tuple[int, str]]) -> Comparison:
    """Classify a failed check against its baseline run ``(exit_code, output)``."""
    if before is None or not after_failed:
        return Comparison(preexisting=False)
    before_code, before_output = before
    if before_code == 0:
        return Comparison(preexisting=False, note="passed before this change")
    after_ids, before_ids = failing_tests(after_output), failing_tests(before_output)
    if after_ids:
        new = tuple(sorted(after_ids - before_ids))
        old = tuple(sorted(after_ids & before_ids))
        return Comparison(preexisting=not new, new_failures=new, old_failures=old,
                          note="failing before this change" if not new else "")
    return Comparison(preexisting=True, note="failing before this change")


class Baseline:
    def __init__(
        self,
        workspace: Path | str,
        *,
        run: BaselineRunner,
        base_commit: str = "",
        undo_since: Optional[int] = None,
        session_id: str = "",
        scratch_root: Path | str,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.run = run
        self.base_commit = base_commit
        self.undo_since = undo_since
        self.session_id = session_id
        self.scratch_root = Path(scratch_root).expanduser()
        self._results: dict[str, Optional[tuple[int, str]]] = {}

    async def result(self, command: str) -> Optional[tuple[int, str]]:
        """``(exit_code, output)`` of ``command`` before the change; None if unknown."""
        if command not in self._results:
            try:
                self._results[command] = await self._run(command)
            except Exception as exc:
                logger.debug("baseline run failed: %s", exc)
                self._results[command] = None
        return self._results[command]

    async def _run(self, command: str) -> Optional[tuple[int, str]]:
        repo = repository_root(self.workspace)
        if repo is None:
            return None
        commit = self.base_commit or snapshot(repo)[0]
        destination = (self.scratch_root / f"baseline-{uuid.uuid4().hex[:8]}").resolve()
        linked = checkout(repo, commit, destination)
        try:
            if not self.base_commit:
                self._undo_turn(repo, destination)
            inside = self.workspace.relative_to(repo) if self.workspace.is_relative_to(repo) else Path()
            cwd = destination / inside
            cwd.mkdir(parents=True, exist_ok=True)
            raw = self.run(command, str(cwd))
            if inspect.isawaitable(raw):
                raw = await raw
        finally:
            remove_checkout(repo, destination, linked)
        from .acceptance import _read_run_result

        code, output, error = _read_run_result(raw)
        return (code if not error else 1, output)

    def _undo_turn(self, repo: Path, destination: Path) -> None:
        """Put back what this turn's checkpointed edits replaced."""
        if self.undo_since is None:
            return
        from .checkpoints import CheckpointStore

        earliest: dict[str, Any] = {}
        for record in reversed(CheckpointStore().since(self.undo_since, session_id=self.session_id or None)):
            for file in record.files:
                earliest.setdefault(file.path, file)   # oldest change holds the true "before"
        for path, file in earliest.items():
            try:
                relative = Path(path).resolve().relative_to(repo)
            except ValueError:
                continue
            target = destination / relative
            if file.existed_before:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(file.before_content, encoding="utf-8")
            elif target.exists():
                target.unlink()


__all__ = ["Baseline", "Comparison", "compare", "failing_tests", "TaskWorktreeError"]
