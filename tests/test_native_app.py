"""Persistent app protocol with the REAL interactive agent and file permissions.

Only inference is replaced in a subprocess sitecustomize. The external network
is blocked so these tests cannot consume credentials or contact a real model.
"""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest


PROVIDER = r'''
import asyncio, socket, faulthandler
from pathlib import Path
from aria_code.apps.cli.providers import runtime_bridge
faulthandler.dump_traceback_later(8)
_connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex
def guard(self, address):
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
        return _connect(self, address)
    raise OSError("Native app tests must not contact an external model")
def guard_ex(self, address):
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
        return _connect_ex(self, address)
    raise OSError("Native app tests must not contact an external model")
socket.socket.connect, socket.socket.connect_ex = guard, guard_ex
def factory(**settings):
    calls = 0
    async def provider(prompt, history, *, on_token=None, on_thinking=None, **kw):
        nonlocal calls
        calls += 1
        if "wait forever" in prompt:
            if on_token: on_token("Started waiting.\n")
            await asyncio.sleep(60)
        if "Create native-note" in prompt and calls == 1:
            return {"success": True, "response": "", "provider": "fixture",
                    "tool_calls_pending": [{"tool": "write_file", "params": {
                        "path": "native-note.txt", "content": "Aria wrote this complete local document.\n你好，世界。\n"}}]}
        if "Create native-note" in prompt:
            text = "已写入文件。\n" if Path("native-note.txt").exists() else "I will follow your feedback instead.\n"
        else:
            text = "History: " + " | ".join(str(m.get("content", "")) for m in history if m.get("role") == "user") + "\n你好。\n"
        if on_thinking: on_thinking("HIDDEN_REASONING_MUST_NOT_LEAK")
        if on_token: on_token(text)
        return {"success": True, "response": text, "provider": "fixture"}
    return provider
runtime_bridge.make_provider_fn = factory
'''


class Client:
    def __init__(self, command, env, cwd):
        self.p = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.events = queue.Queue()
        self.seen = []
        self.errors = []
        def read():
            try:
                for line in self.p.stdout:
                    self.events.put(json.loads(line))
            except Exception as exc:
                self.events.put(exc)
            self.events.put(None)
        threading.Thread(target=read, daemon=True).start()
        def errors():
            for line in self.p.stderr:
                self.errors.append(line.decode('utf-8', errors='replace'))
                self.errors[:] = self.errors[-100:]
        threading.Thread(target=errors, daemon=True).start()

    def send(self, kind, **fields):
        self.p.stdin.write((json.dumps({"type": kind, "protocol": 1, **fields}) + "\n").encode())
        self.p.stdin.flush()

    def until(self, kind, timeout=12):
        while True:
            try:
                event = self.events.get(timeout=timeout)
            except queue.Empty:
                pytest.fail(f'No {kind}: worker={self.p.poll()}, events={self.seen[-20:]}, stderr={self.errors}')
            assert isinstance(event, dict), (event, self.p.poll(), self.seen[-8:])
            self.seen.append(event)
            if event["type"] == kind:
                return event
            assert event["type"] != "session.failed", event

    def close(self):
        if self.p.poll() is None:
            self.send("shutdown")
            self.until("session.closed")
            self.p.wait(timeout=5)
        assert self.p.returncode == 0, ''.join(self.errors)
        assert "HIDDEN_REASONING_MUST_NOT_LEAK" not in json.dumps(self.seen)


@pytest.fixture
def application(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "model": "gemini-3.5-flash", "local_provider": "google", "local_mode": False,
        "ui_lang": "en", "auto_save_sessions": True, "task_isolation": "off",
        "check_for_update_on_startup": False, "auto_compact_context": False,
    }), encoding="utf-8")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    compile(PROVIDER, "sitecustomize.py", "exec")
    (hooks / "sitecustomize.py").write_text(PROVIDER, encoding="utf-8")
    env = dict(os.environ, ARIA_HOME=str(home), ARIA_OFFLINE="1", HOME=str(tmp_path / "home"),
               ARIA_USER_OUTPUT_ROOT=str(tmp_path / "outputs"),
               PYTHONPATH=str(hooks) + os.pathsep + str(Path(__file__).resolve().parents[1] / "src"))
    clients = []
    def start(*args, native=False):
        if native:
            if not os.environ.get("ARIA_NATIVE_BINARY"):
                pytest.skip("build the Rust prototype first")
            command = [os.environ["ARIA_NATIVE_BINARY"], "--python", sys.executable, "chat", "--jsonl", *args]
        else:
            command = [sys.executable, "-u", "-m", "aria_code.apps.cli.app_server", *args]
        c = Client(command, env, tmp_path)
        clients.append(c)
        c.ready = c.until("session.ready")
        return c
    yield start, tmp_path, home, env
    for c in clients:
        if c.p.poll() is None:
            c.p.kill()
            c.p.wait(timeout=5)


@pytest.mark.parametrize("native", [False, True])
def test_multi_turn_history_and_saved_session_resume(application, native):
    start, _, home, _ = application
    c = start(native=native)
    c.send("turn.submit", turn_id="one", text="Remember apricot")
    c.until("turn.completed")
    c.send("turn.submit", turn_id="two", text="What did I say?")
    c.until("turn.completed")
    assert any(e["type"] == "answer.delta" and "Remember apricot" in e["text"] for e in c.seen)
    ident = c.ready["session_id"]
    c.close()
    saved = json.loads((home / "sessions" / (ident + ".json")).read_text())
    assert len(saved["messages"]) == 4
    resumed = start("--resume", ident, native=native)
    assert len(resumed.ready["messages"]) == 4
    resumed.send("turn.submit", turn_id="three", text="Recall apricot")
    resumed.until("turn.completed")
    assert any(e["type"] == "answer.delta" and "Remember apricot" in e["text"] for e in resumed.seen)
    resumed.close()


@pytest.mark.parametrize("decision", ["yes", "no", "escape"])
def test_real_file_write_is_frontend_approved_or_declined(application, decision):
    start, project, _, _ = application
    c = start()
    c.send("turn.submit", turn_id="write", text="Create native-note.txt in this project")
    menu = c.until("approval.requested")
    assert menu["choices"][0][0] == "Yes"
    choice = 0 if decision == "yes" else len(menu["choices"]) - 1 if decision == "no" else -1
    c.send("dialog.respond", turn_id="write", request_id=menu["request_id"], choice=choice)
    if decision == "no":
        question = c.until("input.requested")
        c.send("dialog.respond", turn_id="write", request_id=question["request_id"], text="Do not create files; explain your plan.")
    c.until("turn.completed")
    assert (project / "native-note.txt").exists() is (decision == "yes")
    if decision == "yes":
        assert "你好" in (project / "native-note.txt").read_text(encoding="utf-8")
        assert any(e["type"] == "tool.completed" and e["success"] for e in c.seen)
    c.close()


def test_cancel_pending_model_then_continue_same_worker(application):
    start, _, _, _ = application
    c = start()
    c.send("turn.submit", turn_id="wait", text="wait forever")
    c.until("answer.delta")
    c.send("turn.cancel", turn_id="wait")
    assert c.until("turn.completed")["status"] == "cancelled"
    c.send("turn.submit", turn_id="next", text="Continue normally")
    assert c.until("turn.completed")["status"] == "ok"
    c.close()


def test_stale_approval_and_invalid_requests_do_not_grant_access(application):
    start, project, _, _ = application
    c = start()
    c.send("turn.submit", turn_id="write", text="Create native-note.txt in this project")
    menu = c.until("approval.requested")
    c.send("dialog.respond", turn_id="old", request_id=menu["request_id"], choice=0)
    assert "Stale" in c.until("protocol.error")["error"]
    assert not (project / "native-note.txt").exists()
    c.send("turn.cancel", turn_id="write")
    c.until("turn.completed")
    c.send("session.resume", session_id="../escape")
    assert "Invalid session" in c.until("protocol.error")["error"]
    c.send("turn.submit", turn_id="", text="hello")
    assert "Invalid turn" in c.until("protocol.error")["error"]
    c.close()


def test_slash_commands_are_captured_and_do_not_call_model(application):
    start, _, _, _ = application
    c = start()
    c.send("turn.submit", turn_id="help", text="/help")
    c.until("turn.completed")
    assert any(e["type"] == "output.delta" and "/" in e["text"] for e in c.seen)
    assert not any(e["type"] == "answer.delta" for e in c.seen)
    c.close()


def test_automated_git_leaves_application_stdin_unconsumed(tmp_path):
    # A real Git command that reads stdin must see EOF unless given explicit
    # blob/patch data. The JSONL request belongs to the application alone.
    code = '''
import sys
from aria_code.runtime.task_worktree import _git
print(_git("hash-object", "--stdin", cwd=".").decode().strip())
print(_git("hash-object", "--stdin", cwd=".", input_data=b"blob").decode().strip())
print(sys.stdin.readline().strip())
'''
    request = '{"type":"shutdown","protocol":1}'
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                            input=request + "\n", capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    import hashlib
    blob = hashlib.sha1(b"blob 4\x00blob").hexdigest()
    assert result.stdout.splitlines() == ["e69de29bb2d1d6434b8b29ae775ad8c2e48c5391", blob, request]


@pytest.mark.skipif(sys.platform == "win32", reason="Unix PTY; Windows uses TestBackend and protocol tests")
def test_real_terminal_paste_approval_resize_cancel_and_restore(application):
    import codecs
    import fcntl
    import pty
    import select
    import struct
    import termios
    pyte = pytest.importorskip("pyte")
    if not os.environ.get("ARIA_NATIVE_BINARY"):
        pytest.skip("build the Rust prototype first")
    _, project, _, env = application
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 100, 0, 0))
    p = subprocess.Popen([os.environ["ARIA_NATIVE_BINARY"], "--python", sys.executable, "chat"],
                         cwd=project, env=dict(env, TERM="xterm-256color"), stdin=slave, stdout=slave, stderr=slave)
    screen = pyte.Screen(100, 32)
    stream = pyte.Stream(screen)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    raw = bytearray()
    def display():
        # pyte leaves empty cells when a diff overwrites half of a CJK glyph.
        # Real terminals treat those trailing cells as blanks.
        from wcwidth import wcswidth
        rows = []
        for y in range(screen.lines):
            row, skip = [], False
            for x in range(screen.columns):
                if skip:
                    skip = False
                    continue
                data = screen.buffer[y][x].data or " "
                row.append(data)
                skip = wcswidth(data) == 2
            rows.append("".join(row))
        return "\n".join(rows)
    def wait(text):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if text in display():
                return
            if select.select([master], [], [], 0.1)[0]:
                chunk = os.read(master, 65_536)
                raw.extend(chunk)
                stream.feed(decoder.decode(chunk))
            assert p.poll() is None, raw[-3000:].decode(errors="replace")
        raise AssertionError((text, display()))
    try:
        wait("Ready")
        os.write(master, b"\x1b[200~Remember apricot\n\x1b[201~")
        wait("Remember apricot")
        os.write(master, b"\r")
        wait("你好")
        wait("ok")
        os.write(master, b"Create native-note.txt in this project\r")
        wait("Your response required")
        wait("Always allow")
        os.write(master, b"n")
        wait("Tell Aria what to do instead")
        os.write(master, b"Please explain instead.\r")
        wait("write_file  declined")
        wait("ok · F1")
        assert not (project / "native-note.txt").exists()
        os.write(master, b"wait forever\r")
        wait("Started waiting")
        os.write(master, b"\x1b")
        wait("cancelled")
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 40, 0, 0))
        screen.resize(12, 40)
        p.send_signal(__import__("signal").SIGWINCH)
        wait("Ask Aria")
        os.write(master, b"\x04")
        p.wait(timeout=5)
        assert p.returncode == 0
        assert termios.tcgetattr(slave) == before
        assert b"HIDDEN_REASONING_MUST_NOT_LEAK" not in raw
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)
        os.close(master)
        os.close(slave)


def test_session_switch_validates_before_creation_and_resets_history(application):
    start, _, home, _ = application
    c = start()
    old_id = c.ready['session_id']
    c.send('turn.submit', turn_id='one', text='Remember apricot')
    c.until('turn.completed')
    c.send('session.resume', session_id='does-not-exist')
    assert c.until('protocol.error')['error'] == 'Session not found'
    assert not (home / 'sessions' / 'does-not-exist.jsonl').exists()
    c.send('session.new')
    ready = c.until('session.ready')
    assert ready['session_id'] != old_id and ready['messages'] == []
    c.send('turn.submit', turn_id='two', text='Fresh conversation')
    c.until('turn.completed')
    new_turn = c.seen[c.seen.index(ready):]
    assert all('apricot' not in e.get('text', '') for e in new_turn)
    c.send('session.resume', session_id=old_id)
    assert len(c.until('session.ready')['messages']) == 2
    c.close()


def test_atomic_snapshot_failure_keeps_previous_session(tmp_path, monkeypatch):
    from aria_code.apps.cli import session_store
    store = session_store.SessionManager(tmp_path)
    prior = [{'role': 'user', 'content': 'Earlier 你好'}]
    store.save_session('session', prior)
    def failed_replace(*args):
        raise OSError('Interrupted replacement')
    monkeypatch.setattr(session_store.os, 'replace', failed_replace)
    with pytest.raises(OSError, match='Interrupted'):
        store.save_session('session', [{'role': 'user', 'content': 'New turn'}])
    assert store.load_session('session')['messages'] == prior
    assert not list(tmp_path.glob('.session-*.tmp'))


@pytest.mark.parametrize('behavior', ['malformed', 'exit-status', 'hang'])
def test_native_protocol_failure_timeout_and_descendant_cleanup(tmp_path, behavior):
    binary = os.environ.get('ARIA_NATIVE_BINARY')
    if not binary:
        pytest.skip('build the Rust prototype first')
    package = tmp_path / 'fake' / 'aria_code' / 'apps' / 'cli'
    package.mkdir(parents=True)
    for p in (package, package.parent, package.parent.parent):
        (p / '__init__.py').write_text('')
    code = '''
import json, os, subprocess, sys, time
from pathlib import Path
def emit(kind, **fields):
    print(json.dumps(dict(type=kind, protocol=1, **fields)), flush=True)
def main():
    behavior = os.environ['TEST_BEHAVIOR']
    if behavior == 'malformed':
        print('not JSON', flush=True)
        time.sleep(60)
    if behavior == 'exit-status':
        emit('session.failed', error='Fixture startup failed')
        emit('session.closed')
        sys.exit(7)
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                             start_new_session=(os.name != 'nt'))
    Path('child.pid').write_text(str(child.pid))
    emit('session.ready')
    sys.stdin.readline()
    emit('turn.started', turn_id='hung')
    time.sleep(60)
'''
    (package / 'app_server.py').write_text(code, encoding='utf-8')
    env = dict(os.environ, PYTHONPATH=str(tmp_path / 'fake'), ARIA_HOME=str(tmp_path / 'state'),
               TEST_BEHAVIOR=behavior)
    done = subprocess.run([binary, '--python', sys.executable, '-C', str(tmp_path),
                           '--timeout-ms', '1000', 'chat', '--jsonl'],
                          input=b'{"protocol":1,"type":"turn.submit","turn_id":"hung","text":"hello"}\n',
                          capture_output=True, env=env, timeout=8)
    if behavior == 'exit-status':
        assert done.returncode == 7, done.stderr
    else:
        assert done.returncode != 0, done.stdout
        expected = b'Runtime JSON' if behavior == 'malformed' else b'timed out'
        assert expected in done.stderr, done.stderr
    if behavior == 'hang':
        pid = int((tmp_path / 'child.pid').read_text())
        from test_background_processes import alive as pid_alive
        deadline = time.monotonic() + 3
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not pid_alive(pid)


def test_new_session_drops_temporary_file_write_grant(application):
    start, _, _, _ = application
    c = start()
    c.send('turn.submit', turn_id='grant', text='Create native-note.txt in this project')
    menu = c.until('approval.requested')
    assert 'Always allow' in menu['choices'][1][0]
    c.send('dialog.respond', turn_id='grant', request_id=menu['request_id'], choice=1)
    c.until('turn.completed')
    c.send('turn.submit', turn_id='same', text='Create native-note.txt in this project again')
    before = len(c.seen)
    c.until('turn.completed')
    assert not any(e['type'] == 'approval.requested' for e in c.seen[before:])
    c.send('session.new')
    c.until('session.ready')
    c.send('turn.submit', turn_id='fresh', text='Create native-note.txt in this project again')
    menu = c.until('approval.requested')
    c.send('dialog.respond', turn_id='fresh', request_id=menu['request_id'], choice=-1)
    c.until('turn.completed')
    c.close()
