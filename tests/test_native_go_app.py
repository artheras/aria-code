"""Exercise the actual Go app executable and its Rust containment barrier."""
import json
import os
import queue
import subprocess
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def test_model_picker_and_file_references_through_native_binary(tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.path == "/v1/models"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data":[{"id":"fixture-local-model"}]}')

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"Read the provided evidence."},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

    service = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=service.serve_forever, daemon=True).start()
    state, project = tmp_path / "state", tmp_path / "project"
    state.mkdir()
    project.mkdir()
    (state / "config.json").write_text(json.dumps({"local_provider": "google"}), encoding="utf-8")
    (state / "providers.json").write_text('{"model_providers":{}}', encoding="utf-8")
    (project / "design notes.txt").write_text("UNIQUE_REFERENCE_EVIDENCE", encoding="utf-8")
    with zipfile.ZipFile(project / "report.docx", "w", zipfile.ZIP_DEFLATED) as document:
        document.writestr("word/document.xml", '<document><p><t>OFFICE_DOCUMENT_EVIDENCE 中文</t></p></document>')
    process = subprocess.Popen(
        [binary(), "app"], cwd=project,
        env=dict(os.environ, ARIA_HOME=str(state), OLLAMA_HOST=f"http://127.0.0.1:{service.server_port}"),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    events = queue.Queue()

    def reader():
        for line in process.stdout:
            events.put(json.loads(line))

    threading.Thread(target=reader, daemon=True).start()

    def send(**fields):
        process.stdin.write(json.dumps(dict(protocol=1, **fields)) + "\n")
        process.stdin.flush()

    def until(kind):
        while True:
            event = events.get(timeout=10)
            assert event["type"] not in ("protocol.error", "session.failed"), event
            if event["type"] == kind:
                return event

    try:
        process.stdin.write("\n")
        process.stdin.flush()
        ready = until("session.ready")
        sid = ready["session_id"]
        send(type="completion.request", session_id=sid, text="read @des")
        assert until("completion.result")["candidates"] == ['read @file:"design notes.txt"']
        send(type="turn.submit", turn_id="choose", text="/model")
        dialog = until("input.requested")
        send(type="dialog.respond", turn_id="choose", request_id=dialog["request_id"], choice=1)
        dialog = until("input.requested")
        assert dialog["choices"][0][0] == "fixture-local-model"
        send(type="dialog.respond", turn_id="choose", request_id=dialog["request_id"], choice=0)
        assert until("turn.completed")["status"] == "ok"
        assert until("session.state")["provider"] == "ollama"
        send(type="turn.submit", turn_id="read", text='Explain @file:"design notes.txt" and @report.docx')
        assert until("tool.completed")["success"] is True
        assert until("turn.completed")["status"] == "ok"
        until("session.state")
        assert len(calls) == 1 and calls[0]["model"] == "fixture-local-model"
        assert "UNIQUE_REFERENCE_EVIDENCE" in json.dumps(calls[0]["messages"])
        assert "OFFICE_DOCUMENT_EVIDENCE" in json.dumps(calls[0]["messages"])
        saved = json.loads((state / "sessions" / f"{sid}.json").read_text(encoding="utf-8"))
        assert "UNIQUE_REFERENCE_EVIDENCE" in json.dumps(saved["provider_contents"])
        assert all(m["content"] != "/model" for m in saved["messages"])
        send(type="shutdown")
        until("session.closed")
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        service.shutdown()
        service.server_close()


def binary():
    value = os.environ.get("ARIA_GO_RUNTIME_BINARY")
    if not value:
        pytest.skip("The native-runtime CI job builds this executable")
    return value


def test_native_app_does_not_touch_state_before_the_host_barrier(tmp_path):
    program = binary()
    state = tmp_path / "state"
    project = tmp_path / "project"
    state.mkdir()
    project.mkdir()
    cfg = state / "config.json"
    original = json.dumps(dict(local_provider="google", model="gemini-3.5-flash"))
    cfg.write_text(original, encoding="utf-8")
    env = dict(os.environ, ARIA_HOME=str(state))
    process = subprocess.Popen([program, "app"], cwd=project, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    events = queue.Queue()
    def read():
        for line in process.stdout:
            events.put(json.loads(line))
    threading.Thread(target=read, daemon=True).start()
    try:
        time.sleep(0.2)
        assert not (state / "sessions").exists()
        assert cfg.read_text(encoding="utf-8") == original
        assert events.empty()
        process.stdin.write("\n")
        process.stdin.flush()
        assert events.get(timeout=5)["type"] == "session.ready"
        process.stdin.write(json.dumps(dict(protocol=1, type="shutdown")) + "\n")
        process.stdin.flush()
        assert events.get(timeout=5)["type"] == "session.closed"
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_unsupported_native_provider_preserves_existing_user_state(tmp_path):
    cfg = tmp_path / "config.json"
    original = json.dumps(dict(local_provider="undeclared-test-provider", last_session_id="previous"))
    cfg.write_text(original, encoding="utf-8")
    process = subprocess.run([binary(), "app"], input="\n", env=dict(os.environ, ARIA_HOME=str(tmp_path)), capture_output=True, text=True, encoding="utf-8", timeout=5)
    assert process.returncode != 0
    assert cfg.read_text(encoding="utf-8") == original
    assert not (tmp_path / "sessions").exists()
    events = [json.loads(line) for line in process.stdout.splitlines()]
    assert [event["type"] for event in events] == ["session.failed", "session.closed"]
