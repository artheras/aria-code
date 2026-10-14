"""Headless Go binary with the actual Google SDK and a local protocol fixture."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socketserver
import subprocess
import threading

import pytest


class LocalServer(ThreadingHTTPServer):
    """A loopback server that skips HTTPServer's reverse lookup of its own
    address, which can stall past the test timeout on macOS runners."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


@pytest.fixture
def stream(tmp_path):
    binary = os.environ.get("ARIA_GO_RUNTIME_BINARY")
    if not binary:
        pytest.skip("Build the native Go application before checking its public stream")
    project = tmp_path / "project"
    state = tmp_path / "state"
    project.mkdir()
    state.mkdir()
    (state / "config.json").write_text(json.dumps(dict(local_provider="google", model="gemini-test",
                                                       use_vertexai=False, api_key="fixture-only-key")), encoding="utf-8")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            parts = body["contents"][-1]["parts"]
            response = parts[0].get("functionResponse")
            if response:
                result = response["response"]
                if result["success"]:
                    text = result.get("data", {}).get("content", "Actual write completed.")
                else:
                    text = "Operation declined: " + result["error"]
                output = [{"text": text}]
            else:
                text = parts[0]["text"]
                if text == "read note.txt":
                    output = [dict(functionCall=dict(name="read_file", args=dict(path="note.txt")))]
                elif text == "write note.txt":
                    output = [dict(functionCall=dict(name="write_file", args=dict(path="note.txt", content="Actual SDK native write.\n")))]
                else:
                    output = [{"text": "你好！"}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            record = dict(candidates=[dict(content=dict(role="model", parts=output), finishReason="STOP")])
            self.wfile.write(("data: " + json.dumps(record) + "\n\n").encode())
            self.wfile.flush()

    server = LocalServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(os.environ, ARIA_HOME=str(state), ARIA_PYTHON="missing-python-for-this-test",
               GOOGLE_GEMINI_BASE_URL=f"http://127.0.0.1:{server.server_port}", GOOGLE_GENAI_USE_VERTEXAI="false",
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")

    def call(prompt, *args, rust=False):
        if rust:
            native = os.environ.get("ARIA_NATIVE_BINARY")
            if not native:
                pytest.skip("Build the Rust frontend for the complete execution boundary")
            command = [native, "-C", str(project), "exec", "--jsonl", prompt, "--", *args]
            call_env = dict(env, ARIA_RUNTIME=binary)
            data = None
        else:
            command = [binary, "stream", "--format=jsonl", "--quiet", "--no-banner", "--prompt="+prompt, *args]
            call_env = env
            data = "\n"
        process = subprocess.run(command, cwd=project, env=call_env, input=data,
                                 capture_output=True, text=True, encoding="utf-8", timeout=10)
        events = [json.loads(line) for line in process.stdout.splitlines()]
        assert events[0]["type"] == "turn.started"
        assert events[-1]["type"] == "turn.completed"
        assert sum(e["type"] == "turn.completed" for e in events) == 1
        return process, events
    call.env = env
    yield project, state, requests, call
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_native_stream_reads_actual_file_and_saves_session(stream):
    project, state, requests, call = stream
    evidence = "unpredictable-file-marker-" + os.urandom(8).hex()
    (project / "note.txt").write_text(evidence, encoding="utf-8")
    process, events = call("read note.txt")
    assert process.returncode == 0, process.stderr
    assert evidence in events[-1]["response"]
    assert len(requests) == 2
    assert [e["tool"] for e in events if e["type"] == "tool.started"] == ["read_file"]
    session_id = events[-1]["session_id"]
    saved = json.loads((state / "sessions" / (session_id + ".json")).read_text(encoding="utf-8"))
    assert evidence in saved["messages"][-1]["content"]


def test_native_stream_refuses_unapproved_write_then_accepts_explicit_grant(stream):
    project, _, _, call = stream
    process, events = call("write note.txt")
    assert process.returncode == 0, process.stderr
    assert "approval required" in events[-1]["response"]
    assert not (project / "note.txt").exists()
    assert not any(e["type"] == "approval.requested" for e in events)
    process, events = call("write note.txt", "--allow-tools", "write_file")
    assert process.returncode == 0, process.stderr
    assert (project / "note.txt").read_text(encoding="utf-8") == "Actual SDK native write.\n"
    assert events[-1]["success"] is True
    # The headless result ends with the delivery report, written from what ran.
    delivery = events[-1]["delivery"]
    assert delivery["status"] == "done"
    [changed] = delivery["changed"]
    assert os.path.samefile(changed.pop("path"), project / "note.txt")
    assert changed == dict(added=1, removed=0, created=True, symbols=[], tested_by=[])
    assert delivery["text"].startswith("DONE\n\nChanged\n  A note.txt  +1\n")
    assert "Review the changes — no check ran" in delivery["text"]


def test_native_stream_greeting_has_no_tools_and_prompt_is_literal(stream):
    project, _, requests, call = stream
    process, events = call("你好")
    assert process.returncode == 0, process.stderr
    assert events[-1]["response"] == "你好！"
    assert not requests[0].get("tools")
    assert not any(e["type"] == "tool.started" for e in events)
    process, _ = call("literal ; $(touch SHOULD_NOT_EXIST)")
    assert process.returncode == 0, process.stderr
    assert not (project / "SHOULD_NOT_EXIST").exists()


def test_native_stream_nonzero_shell_exit_remains_failure(stream):
    _, _, requests, call = stream
    process, events = call("!echo actual-command-output & exit 7" if os.name == "nt" else "!printf actual-command-output; exit 7",
                           "--allow-tools", "run_command")
    assert process.returncode == 1
    result = next(e for e in events if e["type"] == "tool.completed")
    assert result["exit_code"] == 7 and result["success"] is False
    assert "actual-command-output" in result["output"]
    assert not requests


def test_rust_exec_and_go_sdk_keep_existing_jsonl_contract(stream):
    _, _, _, call = stream
    process, events = call("你好", rust=True)
    assert process.returncode == 0, process.stderr
    assert events[-1]["response"] == "你好！" and events[-1]["success"] is True
