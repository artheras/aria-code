"""Rewinding to a point undoes every file change recorded after it."""

import pytest

from aria_code.runtime.checkpoints import CheckpointConflictError, CheckpointStore


@pytest.fixture
def store(tmp_path):
    return CheckpointStore(tmp_path / "runs.sqlite3")


def write(store, path, content, *, session="s1"):
    before = path.read_text() if path.exists() else ""
    existed = path.exists()
    path.write_text(content)
    store.record_change(path=path, before_content=before, after_content=content,
                        existed_before=existed, source="test", session_id=session)


def test_everything_after_the_point_is_undone_newest_first(store, tmp_path):
    app, new = tmp_path / "app.py", tmp_path / "new.py"
    write(store, app, "v1")
    point = store.max_sequence()
    write(store, app, "v2")
    write(store, new, "created")
    write(store, app, "v3")

    result = store.restore_since(point, session_id="s1")

    assert app.read_text() == "v1"
    assert not new.exists()
    assert len(result.checkpoint_ids) == 3
    assert store.since(point, session_id="s1") == []


def test_other_sessions_are_left_alone(store, tmp_path):
    mine, theirs = tmp_path / "mine.py", tmp_path / "theirs.py"
    point = store.max_sequence()
    write(store, mine, "mine")
    write(store, theirs, "theirs", session="s2")

    store.restore_since(point, session_id="s1")

    assert not mine.exists()
    assert theirs.read_text() == "theirs"


def test_a_later_hand_edit_stops_the_whole_rewind(store, tmp_path):
    app = tmp_path / "app.py"
    write(store, app, "v1")
    point = store.max_sequence()
    write(store, app, "v2")
    app.write_text("edited by hand")

    with pytest.raises(CheckpointConflictError):
        store.restore_since(point, session_id="s1")
    assert app.read_text() == "edited by hand"


def test_files_with_nowhere_to_go_back_to_are_skipped(store, tmp_path):
    gone = tmp_path / "worktree" / "app.py"
    gone.parent.mkdir()
    point = store.max_sequence()
    write(store, gone, "in the task")
    (tmp_path / "real.py").write_text("")
    write(store, tmp_path / "real.py", "applied")
    gone.unlink()
    gone.parent.rmdir()

    result = store.restore_since(point, session_id="s1",
                                 skip=lambda path: path.startswith(str(tmp_path / "worktree")))

    assert (tmp_path / "real.py").read_text() == ""
    assert result.restored_paths == (str(tmp_path / "real.py"),)


def test_a_point_with_nothing_after_it_rewinds_to_itself(store, tmp_path):
    write(store, tmp_path / "app.py", "v1")
    result = store.restore_since(store.max_sequence(), session_id="s1")
    assert result.restored_paths == ()
