"""Restoring thousands of tasks must not do thousands of full-file writes."""

from unittest.mock import Mock

import pytest

import aria_code.runtime.subagent as subagent
from aria_code.runtime.task_ledger import MAX_RESULT_CHARS, TaskLedger


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    ledger = TaskLedger(tmp_path / "tasks.json")
    monkeypatch.setattr(subagent, "_LEDGER", ledger)
    monkeypatch.setattr(subagent, "_TASKS", {})
    return ledger


def track_io(ledger, monkeypatch):
    load = Mock(wraps=ledger.load)
    save = Mock(wraps=ledger.save)
    monkeypatch.setattr(ledger, "load", load)
    monkeypatch.setattr(ledger, "save", save)
    return load, save


def test_thousands_of_unchanged_tasks_are_read_once_without_writes(ledger, monkeypatch):
    statuses = ("done", "failed", "cancelled", "interrupted")
    records = {
        str(i): {**subagent.SubagentTask(task_id=str(i), prompt="inspect",
                                      status=statuses[i % len(statuses)]).snapshot(),
                 "updated_at": 123.0, "future_metadata": {"preserve": i}}
        for i in range(2500)
    }
    ledger.save(records)
    original = ledger.path.read_bytes()
    load, save = track_io(ledger, monkeypatch)

    assert subagent.restore_tasks() == 2500
    assert len(subagent._TASKS) == 2500
    assert load.call_count == 1
    save.assert_not_called()
    assert ledger.path.read_bytes() == original
    assert subagent.restore_tasks() == 0
    save.assert_not_called()


def test_running_tasks_are_interrupted_in_one_atomic_batch(ledger, monkeypatch):
    records = {
        str(i): subagent.SubagentTask(task_id=str(i), prompt="inspect", status="running",
                                    result="x" * (MAX_RESULT_CHARS + 1) if i == 0 else "partial",
                                    handoff={"verification": "unfinished"}).snapshot()
        for i in range(300)
    }
    records["done"] = {"task_id": "done", "status": "done", "prompt": "finished",
                       "updated_at": 123.0, "future_metadata": "keep"}
    ledger.save(records)
    load, save = track_io(ledger, monkeypatch)

    assert subagent.restore_tasks() == 301
    assert load.call_count == 2  # Initial restoration, then the batch merge.
    assert save.call_count == 1
    persisted = TaskLedger(ledger.path).load()
    for i in range(300):
        task = subagent._TASKS[str(i)]
        assert task.status == "interrupted"
        assert task.async_task is None
        assert "restarted" in task.error
        assert persisted[str(i)]["status"] == "interrupted"
        assert persisted[str(i)]["handoff"] == {"verification": "unfinished"}
    assert persisted["done"] == records["done"]
    assert persisted["0"]["result_truncated"] is True
    assert persisted["0"]["result"].startswith("x" * MAX_RESULT_CHARS)


def test_pending_tasks_left_by_an_earlier_process_are_interrupted(ledger):
    ledger.save({
        "queued": {"task_id": "queued", "status": "pending", "prompt": "never ran"},
        "done": {"task_id": "done", "status": "done", "prompt": "finished"},
    })

    assert subagent.restore_tasks() == 2
    assert subagent._TASKS["queued"].status == "interrupted"
    assert "never started" in subagent._TASKS["queued"].error
    assert TaskLedger(ledger.path).load()["queued"]["status"] == "interrupted"
    assert subagent._TASKS["done"].status == "done"


def test_clear_removes_finished_tasks_and_keeps_live_ones(ledger):
    for task_id, status in (("a", "done"), ("b", "interrupted"), ("c", "running"), ("d", "pending")):
        task = subagent.SubagentTask(task_id=task_id, prompt="p", status=status)
        subagent._TASKS[task_id] = task
        ledger.upsert(task.snapshot())

    assert subagent.clear_finished_tasks() == 2
    assert set(subagent._TASKS) == {"c", "d"}
    assert set(TaskLedger(ledger.path).load()) == {"c", "d"}


def test_live_tasks_are_not_overwritten_by_saved_running_state(ledger, monkeypatch):
    ledger.save({"live": {"task_id": "live", "status": "running", "prompt": "saved"}})
    live = subagent.SubagentTask(task_id="live", prompt="new", status="done", result="new result")
    subagent._TASKS["live"] = live
    _, save = track_io(ledger, monkeypatch)

    assert subagent.restore_tasks() == 0
    assert subagent._TASKS["live"] is live
    save.assert_not_called()


def test_failed_recovery_write_keeps_restored_state(ledger, monkeypatch):
    ledger.save({"running": {"task_id": "running", "status": "running", "prompt": "inspect"}})
    monkeypatch.setattr(ledger, "save", Mock(side_effect=OSError("disk full")))

    assert subagent.restore_tasks() == 1
    assert subagent._TASKS["running"].status == "interrupted"
    assert "restarted" in subagent._TASKS["running"].error


def test_invalid_batch_does_not_partially_write(ledger, monkeypatch):
    ledger.save({"existing": {"task_id": "existing", "status": "done"}})
    original = ledger.path.read_bytes()
    load, save = track_io(ledger, monkeypatch)

    with pytest.raises(ValueError, match="task_id"):
        ledger.upsert_many([{"task_id": "valid"}, {"task_id": ""}])
    ledger.upsert_many([])
    load.assert_not_called()
    save.assert_not_called()
    assert ledger.path.read_bytes() == original
