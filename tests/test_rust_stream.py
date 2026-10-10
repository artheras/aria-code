"""Real Rust executable: streaming, protocol failures and process cleanup."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("ARIA_NATIVE_BINARY"), reason="build the Rust prototype first")


def record(kind, **fields):
    return {"type": kind, **fields}


START = record("turn.started", prompt="hello", model="google/test")


def end(text="done", success=True, **fields):
    return record("turn.completed", response=text, success=success, **fields)


def lines(*events):
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events).encode()


@pytest.fixture
def native(tmp_path):
    env = dict(os.environ, ARIA_HOME=str(tmp_path / "state"), ARIA_USER_OUTPUT_ROOT=str(tmp_path / "outputs"))
    binary = os.environ["ARIA_NATIVE_BINARY"]

    def call(*args, **kwargs):
        return subprocess.run([binary, "--python", sys.executable, "-C", str(tmp_path), *args],
                              env=env, capture_output=True, timeout=10, **kwargs)
    return binary, env, call


def fake_runtime(tmp_path, env, body):
    root = tmp_path / "fake"
    package = root / "aria_code" / "apps" / "cli"
    package.mkdir(parents=True)
    for p in (root / "aria_code", root / "aria_code" / "apps", package):
        (p / "__init__.py").write_text("")
    code = "import json,os,sys,time,subprocess\nfrom pathlib import Path\n"
    code += "def emit(event):\n    print(json.dumps(event, ensure_ascii=False), flush=True)\n"
    code += "def main():\n" + "\n".join("    " + l for l in body.splitlines()) + "\n"
    (package / "main.py").write_text(code, encoding="utf-8")
    env["PYTHONPATH"] = str(root)


def test_replay_needs_no_python_and_preserves_code_and_unicode(native):
    binary, env, _ = native
    response = "你好\n```rust\nfn main() {}\n```\n"
    result = subprocess.run([binary, "--python", "missing-python", "render"],
                            input=lines(START, record("answer.delta", text=response), end(response)),
                            env=env, capture_output=True, timeout=3)
    assert result.returncode == 0 and result.stdout.decode() == response
    assert b"[Aria]" in result.stderr


def test_machine_replay_preserves_extra_fields_and_failed_turn_exit(native):
    _, _, call = native
    events = [START, record("tool.started", tool="edit_file", params={"path": "你好.py"}),
              record("tool.completed", tool="edit_file", success=False, error="permission_denied"),
              end("", False, error="checks_failed", acceptance={"verified": False})]
    result = call("render", "--jsonl", input=lines(*events))
    assert result.returncode == 1
    assert [json.loads(l) for l in result.stdout.splitlines()] == events
    assert not result.stderr


def test_exec_preserves_literal_args_workspace_and_relative_state(native, tmp_path):
    binary, env, _ = native
    literal = "你好; $(touch SHOULD_NOT_EXIST)"
    fake_runtime(tmp_path, env, f"""
prompt = next(a.split('=',1)[1] for a in sys.argv if a.startswith('--prompt='))
emit({{'type':'turn.started','prompt':prompt,'model':'fake','args':sys.argv[1:],'cwd':os.getcwd(),
      'home':os.environ['ARIA_HOME'],'full':os.environ['ARIA_EVENTS_FULL'],'stream':os.environ['ARIA_EVENTS_STREAM'],
      'stdin':sys.stdin.read()}})
emit({end('回答')!r})
""")
    env.update(ARIA_HOME="state", ARIA_EVENTS_FULL="1", PYTHONIOENCODING="cp1252")
    workspace = tmp_path / "project"
    workspace.mkdir()
    result = subprocess.run([binary, "--python", sys.executable, "-C", str(workspace),
                             "exec", "--jsonl", literal, "--", "--model", "google/test", "--allow-tools", "read_file"],
                            cwd=tmp_path, env=env, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr.decode()
    event = json.loads(result.stdout.splitlines()[0])
    assert event["prompt"] == literal
    assert event["args"] == ["--format=jsonl", "--quiet", "--no-banner", "--prompt="+literal,
                              "--model", "google/test", "--allow-tools", "read_file"]
    assert Path(event["cwd"]).resolve() == workspace.resolve()
    assert Path(event["home"]).resolve() == (tmp_path / "state").resolve()
    assert event["full"] == "0" and event["stream"] == "1" and event["stdin"] == ""
    assert not (workspace / "SHOULD_NOT_EXIST").exists()


def test_exec_flushes_answer_before_worker_finishes(native, tmp_path):
    binary, env, _ = native
    marker = tmp_path / "finished"
    delta = record("answer.delta", text="实时回答\n")
    completed = end("实时回答\n完整回答\n")
    fake_runtime(tmp_path, env, f"""
emit({START!r})
emit({delta!r})
time.sleep(1.5)
Path({str(marker)!r}).touch()
emit({completed!r})
""")
    process = subprocess.Popen([binary, "--python", sys.executable, "exec", "hello"],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert process.stdout.readline().decode() == "实时回答\n"
        assert not marker.exists()
        out, err = process.communicate(timeout=5)
        assert process.returncode == 0 and out.decode() == "完整回答\n", err.decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize("exit_code,success", [(0, False), (9, True), (1, False)])
def test_worker_and_turn_failures_do_not_become_success(native, tmp_path, exit_code, success):
    _, env, call = native
    fake_runtime(tmp_path, env, f"emit({START!r})\nemit({end('result', success, error='no_output' if not success else '')!r})\nsys.exit({exit_code})")
    result = call("exec", "hello")
    assert result.returncode == (exit_code or 1)
    assert b"no_output" in result.stderr if not success else b"exited" in result.stderr


@pytest.mark.parametrize("body", ["print('polluted stdout')", "emit({!r})".format(START),
                                  "sys.stdout.buffer.write(b'\\xff\\n')", "print('x'*1048577)", "sys.exit(9)"])
def test_malformed_oversized_or_truncated_live_stream_fails(native, tmp_path, body):
    _, env, call = native
    fake_runtime(tmp_path, env, body)
    result = call("exec", "hello")
    assert result.returncode == 2
    assert b"aria-native:" in result.stderr


@pytest.mark.parametrize("args", [("exec", ""), ("exec", "hello", "--format", "json"),
                                  ("exec", "hello", "--watch", "1"), ("exec", "hello", "--prompt=override"),
                                  ("exec", "hello", "--model"), ("exec", "hello", "--model", "--local"),
                                  ("render", "extra")])
def test_invalid_invocations_never_start_python(native, args):
    binary, env, _ = native
    result = subprocess.run([binary, "--python", "missing-python", *args], env=env, capture_output=True, timeout=3)
    assert result.returncode == 2 and b"Python worker" not in result.stderr


def delayed_child(marker):
    return "import time; from pathlib import Path; time.sleep(1.5); Path(" + repr(str(marker)) + ").touch()"


def test_timeout_kills_descendants_even_if_output_consumer_stops_reading(native, tmp_path):
    binary, env, _ = native
    marker = tmp_path / "survived"
    fake_runtime(tmp_path, env, f"""
subprocess.Popen([sys.executable,'-c',{delayed_child(marker)!r}])
print('child started',file=sys.stderr,flush=True)
emit({START!r})
for i in range(50): emit({record('answer.delta', text='x'*16384)!r})
time.sleep(60)
""")
    process = subprocess.Popen([binary, "--python", sys.executable, "--timeout-ms", "500", "exec", "hello"],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # Intentionally do not drain stdout while waiting for the deadline.
        process.wait(timeout=4)
        _, err = process.communicate(timeout=2)
        assert process.returncode == 2 and b"timed out" in err and b"child started" in err
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    time.sleep(1.6)
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="CI has no Windows console to deliver Ctrl+C")
def test_cancel_live_turn_kills_descendants(native, tmp_path):
    binary, env, _ = native
    marker = tmp_path / "survived"
    delta = record("answer.delta", text="ready\n")
    fake_runtime(tmp_path, env, f"subprocess.Popen([sys.executable,'-c',{delayed_child(marker)!r}])\nemit({START!r})\nemit({delta!r})\ntime.sleep(60)")
    process = subprocess.Popen([binary, "--python", sys.executable, "exec", "hello"],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert process.stdout.readline() == b"ready\n"
        process.send_signal(signal.SIGINT)
        _, err = process.communicate(timeout=4)
        assert process.returncode == 130 and b"Cancelled" in err
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    time.sleep(1.6)
    assert not marker.exists()


def test_real_python_runtime_slash_command_uses_native_output(native):
    _, _, call = native
    result = call("exec", "--jsonl", "/help")
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    events = [json.loads(l) for l in result.stdout.splitlines()]
    assert [e["type"] for e in events] == ["turn.started", "turn.completed"]
    assert events[-1]["success"] is True


@pytest.mark.parametrize("approved", [False, True])
def test_real_agent_loop_writes_only_with_operator_approval(native, tmp_path, approved):
    """Only inference is stubbed; CLI, agent loop, policy and file tool are real."""
    _, env, call = native
    state = Path(env["ARIA_HOME"])
    state.mkdir()
    (state / "config.json").write_text(json.dumps({
        "model": "google/gemini-3.5-flash", "local_provider": "google",
        "check_for_update_on_startup": False,
    }), encoding="utf-8")
    hooks = tmp_path / "provider_fixture"
    hooks.mkdir()
    hook_source = r'''
import socket
from pathlib import Path
from aria_code.apps.cli.providers import runtime_bridge
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
def offline_connect(self, address):
    # Windows asyncio implements socketpair with a loopback connection.
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
        return _connect(self, address)
    raise OSError("This fixture must not contact an external model")
def offline_connect_ex(self, address):
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
        return _connect_ex(self, address)
    raise OSError("This fixture must not contact an external model")
socket.socket.connect = offline_connect
socket.socket.connect_ex = offline_connect_ex
def provider_factory(**settings):
    calls = 0
    async def provider(prompt, history, *, on_token=None, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"success": True, "response": "", "provider": "fixture",
                    "tool_calls_pending": [{"tool": "write_file", "params": {
                        "path": "native-note.txt", "content": "A complete local file: 你好世界\n"}}]}
        if not Path("native-note.txt").exists():
            return {"success": False, "error": "approval_denied", "provider": "fixture"}
        text = "已写入本地文件。\n"
        if on_token is not None: on_token(text)
        return {"success": True, "response": text, "provider": "fixture"}
    return provider
runtime_bridge.make_provider_fn = provider_factory
'''
    compile(hook_source, "sitecustomize.py", "exec")
    (hooks / "sitecustomize.py").write_text(hook_source, encoding="utf-8")
    env["PYTHONPATH"] = str(hooks) + os.pathsep + str(Path(__file__).resolve().parents[1] / "src")
    env["ARIA_OFFLINE"] = "1"
    args = ["exec", "--jsonl", "Create native-note.txt in this project"]
    if approved:
        args += ["--", "--allow-tools", "write_file"]
    result = call(*args)
    assert b"Error in sitecustomize" not in result.stderr
    events = [json.loads(l) for l in result.stdout.splitlines()]
    details = (result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace"))
    assert any(e["type"] == "tool.started" and e["tool"] == "write_file" for e in events), details
    tools = [e for e in events if e["type"] == "tool.completed"]
    if approved:
        assert tools and tools[0]["tool"] == "write_file" and tools[0]["success"] is True, details
    else:
        assert not tools
        assert any(e["type"] == "turn.status" and e["state"] == "approval_denied" for e in events), details
    assert events[-1]["type"] == "turn.completed" and events[-1]["success"] is approved, details
    assert result.returncode == (0 if approved else 1)
    assert (tmp_path / "native-note.txt").exists() is approved
    if approved:
        assert "你好世界" in (tmp_path / "native-note.txt").read_text(encoding="utf-8")
        assert any(e["type"] == "answer.delta" for e in events)
