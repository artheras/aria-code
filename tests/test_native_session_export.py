"""Actual native application exports, compared with the retained Python oracle."""
import csv
import io
import json
import os
import queue
import subprocess
import threading

import pytest

from aria_code.apps.cli.session_export import build_session_export_payload


def test_native_exports_saved_history_without_model_call(tmp_path):
    program = os.environ.get("ARIA_GO_RUNTIME_BINARY")
    if not program:
        pytest.skip("native-runtime CI supplies the built executable")
    state, project = tmp_path / "state", tmp_path / "project"
    (state / "sessions").mkdir(parents=True)
    project.mkdir()
    (state / "config.json").write_text(json.dumps({"local_provider": "google", "model": "gemini-3.5-flash"}), encoding="utf-8")
    messages = [
        {"role": "user", "content": '请分析这家公司的库存、运输和仓储成本，给我具体建议。'},
        {"role": "assistant", "content": '建议先计算库存周转，再核对运输和仓储成本。\n"这些结论"需要根据实际数据逐项验证。'},
    ]
    session = state / "sessions" / "export-test.json"
    session.write_text(json.dumps({"id": "export-test", "messages": messages, "metadata": {}}), encoding="utf-8")
    process = subprocess.Popen(
        [program, "app", "--resume", "export-test", "--allow-tools", "write_file"],
        cwd=project, env=dict(os.environ, ARIA_HOME=str(state)),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    events = queue.Queue()
    def read():
        for line in process.stdout:
            events.put(json.loads(line))
    threading.Thread(target=read, daemon=True).start()
    def send(value):
        process.stdin.write(json.dumps(dict(protocol=1, **value)) + "\n")
        process.stdin.flush()
    def until(kind):
        while True:
            event = events.get(timeout=10)
            assert event["type"] not in ("session.failed", "protocol.error"), event
            if event["type"] == kind:
                return event
    try:
        process.stdin.write("\n")
        process.stdin.flush()
        assert "/export" in until("session.ready")["commands"]
        for fmt in ("json", "csv", "md", "sft", "ipynb"):
            filename = f"export {fmt}.txt"
            send(dict(type="turn.submit", turn_id=f"export-{fmt}", text=f"/export {fmt} {filename}"))
            completed = until("turn.completed")
            assert completed["status"] == "ok", completed
            actual = (project / filename).read_text(encoding="utf-8")
            expected, _, _ = build_session_export_payload(fmt, messages)
            if fmt in ("json", "sft", "ipynb"):
                assert json.loads(actual) == json.loads(expected)
            elif fmt == "csv":
                assert list(csv.reader(io.StringIO(actual))) == list(csv.reader(io.StringIO(expected)))
            else:
                assert actual == expected
        # Slash commands cannot enter or overwrite the saved model conversation.
        assert json.loads(session.read_text(encoding="utf-8"))["messages"] == messages
        send(dict(type="turn.submit", turn_id="full-export", text="/export session portable.json"))
        assert until("turn.completed")["status"] == "ok"
        until("session.state")
        original_files = set((state / "sessions").glob("*.json"))
        send(dict(type="turn.submit", turn_id="import", text="/import portable.json"))
        assert until("turn.completed")["status"] == "ok"
        assert until("session.state")["session_id"] == "export-test"
        imported_files = set((state / "sessions").glob("*.json")) - original_files
        assert len(imported_files) == 1
        imported = imported_files.pop()
        assert json.loads(imported.read_text(encoding="utf-8"))["messages"] == messages
        send(dict(type="session.resume", session_id=imported.stem))
        assert until("session.ready")["session_id"] == imported.stem
        send(dict(type="shutdown"))
        until("session.closed")
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
