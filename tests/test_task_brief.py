"""Durable task intent survives restarts without becoming approval or evidence."""
import json
import sqlite3

import pytest

from aria_code.runtime.run_store import RunNotFoundError, RunStore
from aria_code.runtime.run_state import RunStatus
from aria_code.runtime.task_brief import TaskBrief


def create(store, **kwargs):
    return store.create_run(session_id="session", prompt="Implement a report", workspace="/tmp/project", **kwargs)


def test_default_cli_store_path_records_goal_only(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    run = create(store, metadata={"verification_state": "verified"})
    view = RunStore(store.database_path).get_task_brief(run.run_id)
    assert view["brief"]["goal"] == run.prompt
    assert view["brief"]["acceptance"] == ()
    assert view["acceptance_defined"] is False
    assert view["verification_state"] == "unverified"
    assert store.events(run.run_id)[0].data["brief_sha256"] == view["sha256"]


def test_full_brief_survives_lifecycle_and_restart(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    brief = TaskBrief(goal="Add an offline navigation experiment",
                      boundaries=["Do not actuate hardware", "Do not publish results"],
                      rationale="Use deterministic simulation to reproduce failures",
                      acceptance=["Replay matches saved trajectory", "Blocked goal asks for input"])
    run = create(store, task_brief=brief)
    initial = store.get_task_brief(run.run_id)
    for state in (RunStatus.RUNNING, RunStatus.VERIFYING, RunStatus.SUCCEEDED):
        store.transition(run.run_id, state)
    restored = RunStore(store.database_path).get_task_brief(run.run_id)
    assert restored == initial
    assert restored["brief"] == brief.to_dict()
    assert restored["acceptance_defined"] is True
    assert restored["verification_state"] == "unverified"


def test_credential_redaction_precedes_digest_and_storage(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    run = create(store, task_brief=TaskBrief(goal="Use api_key=private-value",
                                           rationale="password=private-password",
                                           acceptance=["Never print token=private-token"]))
    view = store.get_task_brief(run.run_id)
    assert "[REDACTED]" in view["brief"]["goal"]
    assert "private-value" not in json.dumps(view)
    with store._connect() as conn:
        assert "private-password" not in conn.execute("SELECT brief_json FROM run_briefs").fetchone()[0]
    assert len(view["sha256"]) == 64


@pytest.mark.parametrize("value", [
    {"goal": ""}, {"goal": "x" * 65537}, {"goal": "g", "boundaries": "all"},
    {"goal": "g", "acceptance": [""]}, {"goal": "g", "acceptance": ["x"] * 33},
    {"goal": "g", "rationale": 1}, {"goal": "g", "boundaries": ["界" * 2000]},
])
def test_invalid_brief_rejected(value):
    with pytest.raises(ValueError):
        TaskBrief(**value)


def test_mapping_cannot_smuggle_authorization():
    value = TaskBrief(goal="g").to_dict()
    for key, item in (("approved", True), ("workspace", "/"), ("version", True)):
        with pytest.raises(ValueError):
            TaskBrief.from_dict({**value, key: item})


def test_inputs_are_immutable():
    checks = ["CLI returns valid JSON"]
    brief = TaskBrief(goal="g", acceptance=checks)
    checks.append("Publish")
    assert brief.acceptance == ("CLI returns valid JSON",)


def test_invalid_type_never_creates_partial_run(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    with pytest.raises(TypeError):
        create(store, task_brief={"goal": "g"})
    assert store.list_runs() == []


def test_creation_and_brief_are_atomic(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    with store._connect() as conn:
        conn.execute("""CREATE TRIGGER reject_brief BEFORE INSERT ON run_briefs
                        BEGIN SELECT RAISE(ABORT, 'storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        create(store)
    assert store.list_runs() == []


def test_legacy_missing_and_corrupt_are_distinct(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    run = create(store)
    with store._connect() as conn:
        conn.execute("UPDATE run_briefs SET brief_json=? WHERE run_id=?",
                     (json.dumps(TaskBrief(goal="changed").to_dict()), run.run_id))
    with pytest.raises(ValueError, match="Stored task brief is invalid"):
        store.get_task_brief(run.run_id)
    with store._connect() as conn:
        conn.execute("DELETE FROM run_briefs WHERE run_id=?", (run.run_id,))
    assert RunStore(store.database_path).get_task_brief(run.run_id) is None
    with pytest.raises(RunNotFoundError):
        store.get_task_brief("missing")


def test_duplicate_run_cannot_replace_original_brief(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    run = create(store, run_id="fixed")
    original = store.get_task_brief(run.run_id)
    with pytest.raises(sqlite3.IntegrityError):
        create(store, run_id="fixed", task_brief=TaskBrief(goal="new criteria"))
    assert store.get_task_brief(run.run_id) == original


def test_legacy_schema_reopens_without_backfill(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    run = create(store)
    with store._connect() as conn:
        conn.execute("DROP TABLE run_briefs")
    restored = RunStore(store.database_path)
    assert restored.get_run(run.run_id) == run
    assert restored.get_task_brief(run.run_id) is None
    assert restored.get_task_brief(create(restored).run_id) is not None


def test_acceptance_text_never_executes(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    marker = tmp_path / "must-not-exist"
    run = create(store, task_brief=TaskBrief(goal="g", acceptance=[f"touch {marker}"]))
    assert store.get_task_brief(run.run_id)["verification_state"] == "unverified"
    assert not marker.exists()
