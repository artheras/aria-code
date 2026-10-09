"""Rewinding a turn puts back files, conversation, approvals and tasks together."""

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import aria_code.apps.cli.todo_tracker as todo_tracker
from aria_code.runtime.checkpoints import CheckpointConflictError, CheckpointStore
from aria_code.runtime.transactions import (
    FAILED,
    PASSED,
    UNVERIFIED,
    TransactionStore,
    new_point,
    verdict_from_acceptance,
)

# The approval state transactions read lives in the terminal's own module;
# for the fake terminal below, that is this one.
_auto_approve_session = False
_session_always_allow: set = set()
_session_command_prefixes: set = set()


class FakeTerminal:
    def __init__(self, workspace):
        self.session_id = "sess-1"
        self.conversation = []
        self.config = {"_session_workspace_root": str(workspace)}
        self._code_verdict = ""


@pytest.fixture
def tx(tmp_path, monkeypatch):
    import apps.cli.transactions as transactions

    transactions.reset_for_tests(TransactionStore(tmp_path / "transactions"))
    monkeypatch.setenv("ARIA_TASK_ISOLATION", "off")
    _session_always_allow.clear()
    _session_command_prefixes.clear()
    globals()["_auto_approve_session"] = False
    todo_tracker.clear_todos()
    yield transactions
    transactions.reset_for_tests(None)


def edit(path, content, session="sess-1"):
    before = path.read_text() if path.exists() else ""
    existed = path.exists()
    path.write_text(content)
    CheckpointStore().record_change(path=path, before_content=before, after_content=content,
                                    existed_before=existed, source="test", session_id=session)


def test_store_finds_points_by_count_id_and_verdict(tmp_path):
    store = TransactionStore(tmp_path)
    first = store.record(new_point(session_id="s", turn=1, prompt="one", verdict=""))
    second = store.record(new_point(session_id="s", turn=2, prompt="two", verdict=PASSED))
    third = store.record(new_point(session_id="s", turn=3, prompt="three", verdict=FAILED))

    assert [p.turn for p in store.list("s")] == [3, 2, 1]
    assert store.find("s", "1").point_id == third.point_id
    assert store.find("s", second.point_id[:6]).turn == 2
    assert store.latest_green("s").point_id == second.point_id
    store.drop_from(second)
    assert [p.point_id for p in store.list("s")] == [first.point_id]


def test_oversized_conversations_keep_their_newest_messages(tmp_path):
    store = TransactionStore(tmp_path, max_snapshot_bytes=64 * 1024)
    big = [{"role": "user", "content": "x" * 30_000} for _ in range(4)] + [{"role": "user", "content": "last"}]
    point = store.record(new_point(session_id="s", turn=1, prompt="p", messages=big))
    assert point.truncated and point.messages[-1]["content"] == "last" and len(point.messages) < 5


def test_the_verdict_follows_turns_that_changed_files():
    assert verdict_from_acceptance({"paths": ["a.py"], "verified": True}, "") == PASSED
    assert verdict_from_acceptance({"paths": ["a.py"], "verified": False}, PASSED) == FAILED
    assert verdict_from_acceptance({"paths": ["a.py"], "verified": None}, PASSED) == UNVERIFIED
    assert verdict_from_acceptance({"paths": [], "verified": None}, PASSED) == PASSED
    assert verdict_from_acceptance(None, FAILED) == FAILED


def test_rewinding_a_turn_puts_everything_back(tx, tmp_path):
    app = tmp_path / "app.py"
    edit(app, "v1")
    terminal = FakeTerminal(tmp_path)
    terminal.conversation += [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    todo_tracker.update_todos({"todos": [{"content": "plan", "status": "in_progress"}]})
    _session_always_allow.add("read_file")
    terminal._code_verdict = PASSED

    point = tx.capture(terminal, "make it faster")

    # The turn: edits, a new grant, more conversation, a new plan, red checks.
    terminal.conversation += [{"role": "user", "content": "make it faster"},
                              {"role": "assistant", "content": "done"}]
    edit(app, "v2")
    edit(tmp_path / "cache.py", "new file")
    _session_always_allow.add("write_file")
    _session_command_prefixes.add(("rm",))
    globals()["_auto_approve_session"] = True
    todo_tracker.update_todos({"todos": [{"content": "other", "status": "pending"}]})
    terminal._code_verdict = FAILED

    outcome = tx.rewind(terminal, tx.store().find("sess-1", "1"))

    assert outcome.point.point_id == point.point_id
    assert app.read_text() == "v1" and not (tmp_path / "cache.py").exists()
    assert terminal.conversation == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert outcome.messages_removed == 2
    assert _session_always_allow == {"read_file"} and _session_command_prefixes == set()
    assert globals()["_auto_approve_session"] is False
    assert outcome.approvals_revoked == ["allow-all", "write_file", "rm"]
    assert todo_tracker.get_active_todos() == [{"content": "plan", "status": "in_progress"}]
    assert terminal._code_verdict == PASSED
    assert tx.store().list("sess-1") == []
    assert any("2 restored" in line for line in outcome.lines())


def test_a_hand_edit_since_the_point_stops_the_rewind_before_anything_changes(tx, tmp_path):
    app = tmp_path / "app.py"
    terminal = FakeTerminal(tmp_path)
    point = tx.capture(terminal, "edit")
    terminal.conversation.append({"role": "user", "content": "edit"})
    edit(app, "by the model")
    app.write_text("by hand")

    with pytest.raises(CheckpointConflictError):
        tx.rewind(terminal, point)
    assert terminal.conversation == [{"role": "user", "content": "edit"}]
    assert tx.store().list("sess-1")  # the point is still there to retry


def test_background_tasks_started_after_the_point_are_cancelled(tx, tmp_path, monkeypatch):
    import runtime.subagent as subagent

    terminal = FakeTerminal(tmp_path)
    earlier = subagent.SubagentTask(task_id="earlier", prompt="p", status="running", session_id="sess-1",
                                    created_at=time.time() - 60)
    monkeypatch.setattr(subagent, "_LEDGER", subagent.TaskLedger(tmp_path / "ledger.json"))
    monkeypatch.setitem(subagent._TASKS, "earlier", earlier)
    point = tx.capture(terminal, "spawn")
    later = subagent.SubagentTask(task_id="later", prompt="p", status="running", session_id="sess-1",
                                  created_at=time.time() + 1)
    monkeypatch.setitem(subagent._TASKS, "later", later)

    outcome = tx.rewind(terminal, point)

    assert outcome.tasks_cancelled == ["later"]
    assert later.status == "cancelled" and earlier.status == "running"


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo,
                   check=True, capture_output=True)


def test_an_unapplied_task_whose_worktree_is_gone_is_reopened(tx, tmp_path, monkeypatch):
    import apps.cli.task_isolation as task_isolation
    from aria_code.runtime.task_worktree import TaskWorktrees

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "app.py").write_text("a - b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    monkeypatch.delenv("ARIA_TASK_ISOLATION")
    tasks = TaskWorktrees(tmp_path / "worktrees")
    task_isolation.reset_for_tests(tasks)
    terminal = FakeTerminal(repo)

    task = tasks.ensure(repo)
    (Path(task.path) / "app.py").write_text("a + b\n")
    point = tx.capture(terminal, "next")
    tasks.discard(task)

    outcome = tx.rewind(terminal, point)

    reopened = tasks.active(repo)
    assert "reopened" in outcome.task_note
    assert (Path(reopened.path) / "app.py").read_text() == "a + b\n"
    assert (repo / "app.py").read_text() == "a - b\n"
    task_isolation.reset_for_tests(None)


def test_points_store_only_the_messages_added_since_the_previous_one(tmp_path):
    store = TransactionStore(tmp_path)
    conversation = []
    for turn in range(1, 6):
        conversation += [{"role": "user", "content": f"q{turn} " + "x" * 5000},
                         {"role": "assistant", "content": f"a{turn}"}]
        store.record(new_point(session_id="s", turn=turn, prompt=f"q{turn}", messages=list(conversation)))

    size = (tmp_path / "s.jsonl").stat().st_size
    assert size < 2 * len(str(conversation))  # not five copies of a growing conversation
    points = store.list("s")
    assert [len(p.messages) for p in points] == [10, 8, 6, 4, 2]
    assert points[0].messages == conversation


def test_a_compacted_conversation_starts_a_new_full_snapshot(tmp_path):
    store = TransactionStore(tmp_path)
    store.record(new_point(session_id="s", turn=1, prompt="a", messages=[{"role": "user", "content": "long"}]))
    store.record(new_point(session_id="s", turn=2, prompt="b", messages=[{"role": "system", "content": "summary"}]))
    assert store.list("s")[0].messages == [{"role": "system", "content": "summary"}]


def test_old_points_are_dropped_and_the_rest_stay_readable(tmp_path):
    store = TransactionStore(tmp_path, keep_per_session=3)
    conversation = []
    for turn in range(1, 9):
        conversation.append({"role": "user", "content": f"q{turn}"})
        store.record(new_point(session_id="s", turn=turn, prompt=f"q{turn}", messages=list(conversation)))
    points = store.list("s")
    assert [p.turn for p in points] == [8, 7, 6]
    assert points[-1].messages == conversation[:6]


def _isolated_repo(tmp_path, monkeypatch):
    import apps.cli.task_isolation as task_isolation
    from aria_code.runtime.task_worktree import TaskWorktrees

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "app.py").write_text("a - b\n")
    (repo / ".gitignore").write_text("node_modules/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    (repo / "node_modules" / "dep").mkdir(parents=True)
    monkeypatch.delenv("ARIA_TASK_ISOLATION")
    tasks = TaskWorktrees(tmp_path / "worktrees")
    task_isolation.reset_for_tests(tasks)
    return repo, tasks, task_isolation


def test_an_open_worktree_is_reset_to_the_point_including_shell_edits(tx, tmp_path, monkeypatch):
    repo, tasks, task_isolation = _isolated_repo(tmp_path, monkeypatch)
    terminal = FakeTerminal(repo)
    task = tasks.ensure(repo)
    work = Path(task.path)
    (work / "app.py").write_text("a + b\n")
    point = tx.capture(terminal, "next turn")

    # The next turn changes things no checkpoint records: shell edits.
    (work / "app.py").write_text("sed rewrote this\n")
    (work / "generated.py").write_text("made by a formatter\n")

    outcome = tx.rewind(terminal, point)

    assert (work / "app.py").read_text() == "a + b\n"
    assert not (work / "generated.py").exists()
    assert (work / "node_modules" / "dep").is_dir()
    assert "reset" in outcome.task_note
    assert (repo / "app.py").read_text() == "a - b\n"
    task_isolation.reset_for_tests(None)


def test_a_task_started_after_the_point_is_discarded(tx, tmp_path, monkeypatch):
    repo, tasks, task_isolation = _isolated_repo(tmp_path, monkeypatch)
    terminal = FakeTerminal(repo)
    point = tx.capture(terminal, "first turn")
    time.sleep(0.01)
    task = tasks.ensure(repo)
    (Path(task.path) / "app.py").write_text("later work\n")

    outcome = tx.rewind(terminal, point)

    assert tasks.active(repo) is None and not Path(task.path).exists()
    assert "discarded" in outcome.task_note
    task_isolation.reset_for_tests(None)
