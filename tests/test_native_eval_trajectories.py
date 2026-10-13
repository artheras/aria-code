"""The training loop on the native runtime: eval → trajectory → SFT record.

Aria's tuned models are served by the native runtime, so they are trained on
its trajectories: the eval runner drives the Rust frontend and Go runtime over
a task, and the events it writes must be what training/sft_dataset.py (and the
native `training dataset`) turn into Gemini functionCall / functionResponse
records — whole parameters and results, secrets masked.
"""
import json
import os
import socketserver
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from aria_code.evals.harness import PASS, load_suite, run_suite
from aria_code.evals.runner import Trajectories, build_agent_solver
from aria_code.training import sft_dataset

WRITTEN = "fixed\n# deploy token=abc123secret stays out of training data\n"


class LocalServer(ThreadingHTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


@pytest.fixture
def model_server():
    """A Gemini API stand-in: write the fix, then say so."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            last = body["contents"][-1]["parts"][0]
            if "functionResponse" in last:
                output = [{"text": "Fixed note.txt."}]
            else:
                output = [{"functionCall": {"name": "write_file", "args": {"path": "note.txt", "content": WRITTEN}}}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            record = {"candidates": [{"content": {"role": "model", "parts": output}, "finishReason": "STOP"}]}
            self.wfile.write(("data: " + json.dumps(record) + "\n\n").encode())

    server = LocalServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_native_eval_produces_trainable_trajectories(tmp_path, monkeypatch, model_server):
    native, runtime = os.environ.get("ARIA_NATIVE_BINARY"), os.environ.get("ARIA_GO_RUNTIME_BINARY")
    if not native or not runtime:
        pytest.skip("The native-runtime CI job builds both executables")
    state = tmp_path / "state"
    state.mkdir()
    (state / "config.json").write_text(json.dumps({"local_provider": "google", "model": "gemini-test",
                                                   "use_vertexai": False, "api_key": "fixture-only-key"}), encoding="utf-8")
    for key, value in {"ARIA_HOME": str(state), "GOOGLE_GEMINI_BASE_URL": model_server, "GOOGLE_GENAI_USE_VERTEXAI": "false",
                       "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost", "ARIA_PYTHON": "missing-python"}.items():
        monkeypatch.setenv(key, value)

    fixtures = tmp_path / "evals" / "fixtures" / "fix_note"
    fixtures.mkdir(parents=True)
    (fixtures / "note.txt").write_text("broken\n", encoding="utf-8")
    suites = tmp_path / "evals" / "suites"
    suites.mkdir()
    check = "import sys; sys.exit(0 if open('note.txt', encoding='utf-8').read().startswith('fixed') else 1)"
    suite_doc = {"suite": "smoke", "tasks": [{"id": "fix-note", "prompt": "note.txt 写错了，改成 fixed", "fixture": "fix_note",
                                              "verify": '{python} -c "' + check + '"', "tags": ["software"]}]}
    (suites / "smoke.yaml").write_text(yaml.safe_dump(suite_doc, allow_unicode=True), encoding="utf-8")

    name, tasks = load_suite(suites / "smoke.yaml")
    trajectories = Trajectories(tmp_path / "traj", name, "gemini-test")
    solver = build_agent_solver(model="", timeout=60, trajectories=trajectories, runtime="native",
                                native=native, go_runtime=runtime)
    suite = run_suite(tasks, solver=solver, fixtures_root=tmp_path / "evals" / "fixtures", name=name)
    trajectories.close(suite.results)
    [result] = suite.results
    assert result.outcome == PASS, (result.detail, result.log)

    [path] = sorted((tmp_path / "traj" / "smoke").glob("*.jsonl"))
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    kinds = [event["type"] for event in events]
    assert kinds[0] == "eval.task" and kinds[-1] == "eval.outcome" and "turn.completed" in kinds
    started = next(e for e in events if e["type"] == "tool.started")
    completed = next(e for e in events if e["type"] == "tool.completed")
    assert started["params"] == {"path": "note.txt", "content": WRITTEN.replace("token=abc123secret", "token=***")}
    assert completed["result"]["success"] is True and completed["result"]["data"]["changed"] is True
    final = next(e for e in events if e["type"] == "turn.completed")
    assert final["response"] == "Fixed note.txt."

    out = tmp_path / "sft"
    manifest = sft_dataset.build(tmp_path / "traj", suites, out, validation_share=0.0)
    records = [json.loads(line) for line in (out / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1, manifest
    contents = records[0]["contents"]
    assert contents[0] == {"role": "user", "parts": [{"text": "note.txt 写错了，改成 fixed"}]}
    assert contents[1]["parts"][0]["functionCall"] == {"name": "write_file", "args": started["params"]}
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "write_file"
    assert contents[-1] == {"role": "model", "parts": [{"text": "Fixed note.txt."}]}
    assert "abc123secret" not in json.dumps(records, ensure_ascii=False)

    # The native dataset builder reads the native trajectory the same way.
    native_out = tmp_path / "sft-native"
    built = subprocess.run([runtime, "training", "dataset", str(tmp_path / "traj"), "--suites", str(suites),
                            "--out", str(native_out), "--validation-share", "0"],
                           capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert built.returncode == 0, built.stderr
    def examples(folder):
        return [json.loads(line) for line in (folder / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    assert examples(native_out) == examples(out)
