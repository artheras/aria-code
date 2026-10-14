"""Cross-worker checkpoint compatibility and actual native file effects."""
import json
import os
import sqlite3
import subprocess

import pytest

from aria_code.change_store import ChangeStore
from aria_code.runtime.checkpoints import CheckpointStore
from test_native_go_bridge import bridge as bridge, request
from test_native_go_stream import stream as stream


def database(state):
    return state / "runtime" / "runs.sqlite3"


def test_python_can_read_and_restore_a_native_checkpoint(bridge):
    project, state, call = bridge
    path = project / "note.txt"
    path.write_text("Original complete body.\n", encoding="utf-8")
    result = call(request("write_file", path="note.txt", content="Updated complete body.\n"), "write_file")
    assert result["result"]["success"], result
    identifier = result["result"]["data"]["checkpoint_id"]
    old_store = CheckpointStore(database(state))
    record = old_store.get(identifier)
    assert record.status == "active"
    assert record.files[0].before_content == "Original complete body.\n"
    assert record.files[0].after_content == "Updated complete body.\n"
    assert record.source == "write_file"
    old_store.restore_checkpoint(identifier)
    assert path.read_text(encoding="utf-8") == "Original complete body.\n"
    assert not call(request("restore_checkpoint", checkpoint_id=identifier), "restore_checkpoint")["result"]["success"]


@pytest.mark.parametrize("created", [False, True])
def test_native_restore_matches_python_checkpoint_effects(bridge, created):
    project, state, call = bridge
    store = CheckpointStore(database(state))
    targets = [project / "native.txt", project / "oracle.txt"]
    records = []
    before = "" if created else "你好 complete original source.\n"
    after = "你好 complete modified source.\n"
    for target in targets:
        target.write_text(after, encoding="utf-8")
        records.append(store.record_change(path=target, before_content=before, after_content=after,
                                          existed_before=not created, source="edit_file", session_id="legacy-session",
                                          before_mode=0o751 if not created else None))
    result = call(request("restore_checkpoint", checkpoint_id=records[0].checkpoint_id), "restore_checkpoint")
    assert result["result"]["success"], result
    store.restore_checkpoint(records[1].checkpoint_id)
    assert targets[0].exists() == targets[1].exists()
    if not created:
        # Legacy Python checkpoints use write_text's platform newline rules.
        # Native checkpoints separately retain exact physical preimages.
        expected = before.replace("\n", os.linesep).encode("utf-8")
        assert targets[0].read_bytes() == targets[1].read_bytes() == expected
        if os.name != "nt":
            assert targets[0].stat().st_mode & 0o777 == targets[1].stat().st_mode & 0o777 == 0o751
    assert all(store.get(record.checkpoint_id).status == "restored" for record in records)


def test_native_restores_legacy_run_as_a_group_and_records_its_event(bridge):
    project, state, call = bridge
    store = CheckpointStore(database(state))
    run = store.run_store.create_run(prompt="Change two files.", workspace=str(project), session_id="legacy")
    path = project / "note.txt"
    path.write_text("one", encoding="utf-8")
    one = store.record_change(path=path, before_content="original", after_content="one", existed_before=True,
                              source="edit_file", run_id=run.run_id, session_id="legacy", before_mode=0o644)
    path.write_text("two", encoding="utf-8")
    two = store.record_change(path=path, before_content="one", after_content="two", existed_before=True,
                              source="edit_file", run_id=run.run_id, session_id="legacy", before_mode=0o644)
    created = project / "created.txt"
    created.write_text("actual new file", encoding="utf-8")
    three = store.record_change(path=created, before_content="", after_content="actual new file", existed_before=False,
                                source="write_file", run_id=run.run_id, session_id="legacy")
    result = call(request("restore_checkpoint", session_id="legacy"), "restore_checkpoint")
    assert result["result"]["success"], result
    assert result["result"]["data"]["checkpoint_ids"] == [three.checkpoint_id, two.checkpoint_id, one.checkpoint_id]
    assert path.read_text(encoding="utf-8") == "original"
    assert not created.exists()
    with sqlite3.connect(database(state)) as connection:
        event = connection.execute("SELECT event_type,data_json FROM run_events WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                                   (run.run_id,)).fetchone()
    assert event[0] == "checkpoint_restored"
    assert json.loads(event[1])["checkpoint_ids"] == result["result"]["data"]["checkpoint_ids"]


def test_legacy_group_conflict_keeps_every_file_and_checkpoint_active(bridge):
    project, state, call = bridge
    store = CheckpointStore(database(state))
    run = store.run_store.create_run(prompt="Change two files.", workspace=str(project), session_id="legacy")
    records = []
    for name in ("first.txt", "second.txt"):
        path = project / name
        path.write_text("after", encoding="utf-8")
        records.append(store.record_change(path=path, before_content="before", after_content="after", existed_before=True,
                                          source="edit_file", run_id=run.run_id, session_id="legacy"))
    (project / "first.txt").write_text("user edit", encoding="utf-8")
    result = call(request("restore_checkpoint", session_id="legacy"), "restore_checkpoint")
    assert not result["result"]["success"]
    assert (project / "first.txt").read_text(encoding="utf-8") == "user edit"
    assert (project / "second.txt").read_text(encoding="utf-8") == "after"
    assert all(store.get(record.checkpoint_id).status == "active" for record in records)


def test_native_stage_apply_reject_match_existing_change_store_effects(bridge):
    project, state, call = bridge
    native = project / "native.txt"
    oracle = project / "oracle.txt"
    for path in (native, oracle):
        path.write_text("before", encoding="utf-8")
    old_store = ChangeStore()
    old = old_store.stage(oracle, "after")
    result = call(request("write_file", path="native.txt", content="after", stage_only=True), "write_file")
    assert result["result"]["success"]
    identifier = result["result"]["data"]["change_id"]
    assert native.read_bytes() == oracle.read_bytes() == b"before"
    assert call(request("list_changes"))["result"]["data"]["count"] == 1
    result = call(request("apply_change", change_id=identifier), "apply_change")
    assert result["result"]["success"], result
    old_store.apply(old.change_id)
    assert native.read_bytes() == oracle.read_bytes() == b"after"
    rejected = call(request("write_file", path="native.txt", content="rejected", stage_only=True), "write_file")
    old = old_store.stage(oracle, "rejected")
    assert call(request("reject_change", change_id=rejected["result"]["data"]["change_id"]))["result"]["success"]
    old_store.reject(old.change_id)
    assert native.read_bytes() == oracle.read_bytes() == b"after"
    assert call(request("list_changes"))["result"]["data"]["count"] == len(old_store.list()) == 0


def test_actual_rust_entrypoints_stage_apply_and_restore_without_python(bridge):
    native = os.environ.get("ARIA_NATIVE_BINARY")
    if not native:
        pytest.skip("Build Rust to verify the complete frontend/runtime boundary")
    project, state, _ = bridge
    env = dict(os.environ, ARIA_RUNTIME=os.environ["ARIA_GO_RUNTIME_BINARY"], ARIA_PYTHON="missing-python-for-this-test")

    def tool(name, arguments, approved=False):
        args = [native, "-C", str(project), "tool"]
        if approved:
            args.append("--approve-write")
        result = subprocess.run(args + [name, json.dumps(arguments)], env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)["result"]

    staged = tool("write_file", dict(path="new.txt", content="Complete native boundary evidence.", stage_only=True), True)
    assert not (project / "new.txt").exists()
    applied = tool("apply_change", dict(change_id=staged["data"]["change_id"]), True)
    assert (project / "new.txt").read_text(encoding="utf-8") == "Complete native boundary evidence."
    result = subprocess.run([native, "-C", str(project), "exec", "/rewind code " + applied["data"]["checkpoint_id"] + " --yes"],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "Rewound 1 file" in result.stdout
    assert not (project / "new.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="Unix terminal; Windows retains protocol and renderer tests")
def test_actual_terminal_undo_refreshes_screen_state_and_preserves_draft(stream):
    import codecs
    import fcntl
    import pty
    import select
    import struct
    import termios
    import time
    pyte = pytest.importorskip("pyte")
    native = os.environ.get("ARIA_NATIVE_BINARY")
    if not native:
        pytest.skip("Build Rust to verify terminal history refresh")
    project, state, requests, call = stream
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 100, 0, 0))
    env = dict(call.env, ARIA_RUNTIME=os.environ["ARIA_GO_RUNTIME_BINARY"], ARIA_OFFLINE="1",
               ARIA_THEME="dark", TERM="xterm-256color")
    process = subprocess.Popen([native, "-C", str(project), "chat"], cwd=project, env=env,
                               stdin=slave, stdout=slave, stderr=slave)
    screen = pyte.Screen(100, 28)
    terminal = pyte.Stream(screen)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def display():
        from wcwidth import wcswidth
        rows = []
        for y in range(screen.lines):
            row, skip = [], False
            for x in range(screen.columns):
                if skip:
                    skip = False
                    continue
                text = screen.buffer[y][x].data or " "
                row.append(text)
                skip = wcswidth(text) == 2
            rows.append("".join(row))
        return "\n".join(rows)

    def wait(predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            text = display()
            if predicate(text):
                if select.select([master], [], [], 0.05)[0]:
                    terminal.feed(decoder.decode(os.read(master, 65536)))
                    continue
                return text
            if select.select([master], [], [], 0.1)[0]:
                terminal.feed(decoder.decode(os.read(master, 65536)))
            assert process.poll() is None, (process.returncode, text)
        pytest.fail("Terminal did not reach the expected state: " + display())

    try:
        wait(lambda text: "Ready" in text and "▄▄▄▄▄▄▄▄▄▄" in text)
        marker = "old-turn-marker-" + os.urandom(6).hex()
        os.write(master, (marker + "\r").encode())
        wait(lambda text: marker in text and "Completed" in text)
        assert len(requests) == 1
        os.write(master, b"/undo\r")
        os.write(master, b"retained-next-draft")
        text = wait(lambda text: "Undone" in text and marker not in text and "retained-next-draft" in text)
        assert marker not in text
        assert len(requests) == 1, "Undo called the model"
        session = json.loads(next((state / "sessions").glob("*.json")).read_text(encoding="utf-8"))
        assert session["messages"] == [] and session.get("provider_contents", []) == []
        os.write(master, b"\x03\x04")
        process.wait(timeout=5)
        assert process.returncode == 0
        assert termios.tcgetattr(slave) == before
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)
        os.close(slave)
