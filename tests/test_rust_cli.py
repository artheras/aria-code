"""Integration tests against the real compiled executable on all three OSes."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("ARIA_NATIVE_BINARY"), reason="build the opt-in Rust CLI first")


@pytest.fixture
def native(tmp_path):
    binary = os.environ["ARIA_NATIVE_BINARY"]
    env = {**os.environ, "ARIA_HOME": str(tmp_path / "state"),
           "ARIA_USER_OUTPUT_ROOT": str(tmp_path / "outputs"), "PYTHONIOENCODING": "utf-8"}

    def call(*args, **kwargs):
        return subprocess.run([binary, "--python", sys.executable, "-C", str(tmp_path), *args],
                              capture_output=True, env=env, timeout=20, **kwargs)
    return binary, env, call


def test_fast_commands_do_not_need_python(tmp_path):
    binary = os.environ["ARIA_NATIVE_BINARY"]
    for argument in ("--version", "--help"):
        result = subprocess.run([binary, "--python", "nonexistent-python", argument], capture_output=True, timeout=3)
        assert result.returncode == 0
        assert b"experimental" in result.stdout


def test_relative_interpreter_is_resolved_before_changing_workspace(native, tmp_path):
    binary, env, _ = native
    env["PYTHONPATH"] = fake_package(tmp_path, "main", "def main():\n    print('relative interpreter works')\n")
    caller = Path(sys.executable).parent.parent
    relative = os.path.relpath(sys.executable, caller)
    result = subprocess.run([binary, "--python", relative, "-C", str(tmp_path), "run"],
                            cwd=caller, capture_output=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    assert b"relative interpreter works" in result.stdout


def test_reads_searches_and_approved_writes_through_existing_tools(native, tmp_path):
    _, _, call = native
    (tmp_path / "README.md").write_text("hello from Python tools\n", encoding="utf-8")
    result = call("tool", "read_file", json.dumps({"path": "README.md"}))
    assert result.returncode == 0, result.stderr.decode()
    assert "hello from Python tools" in json.loads(result.stdout)["result"]["data"]["content"]
    result = call("tool", "search_code", json.dumps({"pattern": "hello", "glob": "*.md"}))
    assert json.loads(result.stdout)["result"]["data"]["count"] == 1
    args = json.dumps({"path": "你好.txt", "content": "a complete file with Unicode 你好\n"})
    denied = call("tool", "write_file", args)
    assert denied.returncode == 1
    assert not (tmp_path / "你好.txt").exists()
    result = call("tool", "--approve-write", "write_file", args)
    assert result.returncode == 0, (result.stderr.decode(), result.stdout.decode())
    assert json.loads(result.stdout)["result"]["success"]  # no Rich text on stdout
    assert "你好" in (tmp_path / "你好.txt").read_text(encoding="utf-8")
    edit = json.dumps({"path": "你好.txt", "old_string": "complete", "new_string": "edited"})
    result = call("tool", "--approve-write", "edit_file", edit)
    assert result.returncode == 0, result.stderr.decode()
    assert "edited" in (tmp_path / "你好.txt").read_text(encoding="utf-8")


def test_approved_write_still_cannot_leave_workspace(native, tmp_path):
    _, _, call = native
    outside = tmp_path.parent / (tmp_path.name + "-outside.txt")
    result = call("tool", "--approve-write", "write_file", json.dumps({"path": str(outside),
                    "content": "a complete file should never reach outside", "_permission_mode": "full-access"}))
    assert result.returncode == 1
    assert not outside.exists()


def fake_package(tmp_path, module, code):
    root = tmp_path / "fake"
    package = root / "aria_code" / "apps" / "cli"
    package.mkdir(parents=True)
    for parent in (root / "aria_code", root / "aria_code" / "apps", package):
        (parent / "__init__.py").write_text("")
    (package / (module + ".py")).write_text(code, encoding="utf-8")
    return str(root)


def test_run_preserves_cwd_arguments_stdin_and_exit_status(native, tmp_path):
    binary, env, _ = native
    env["PYTHONPATH"] = fake_package(tmp_path, "main", """
import json, os, sys
def main():
    print(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd(), 'input': sys.stdin.read()}, ensure_ascii=True))
    sys.exit(17)
""")
    literal = "你好; $(touch SHOULD_NOT_EXIST)"
    result = subprocess.run([binary, "--python", sys.executable, "-C", str(tmp_path), "run", "--", literal, "--model", "google/gemini"],
                            input="你好\n".encode(), capture_output=True, env=env, timeout=10)
    assert result.returncode == 17
    response = json.loads(result.stdout)
    assert response["args"] == [literal, "--model", "google/gemini"]
    assert Path(response["cwd"]).resolve() == tmp_path.resolve()
    assert response["input"] == "你好\n"
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


@pytest.mark.parametrize("body", ["print('not json')", "print('{\"jsonrpc\":\"2.0\",\"id\":42,\"result\":{\"success\":true}}')",
                                 "print('x' * 1048577)", "import sys; sys.exit(9)"])
def test_malformed_crashed_or_oversized_worker_fails(native, tmp_path, body):
    binary, env, _ = native
    env["PYTHONPATH"] = fake_package(tmp_path, "native_bridge", body)
    result = subprocess.run([binary, "--python", sys.executable, "-C", str(tmp_path), "tool", "read_file", "{}"], capture_output=True, env=env, timeout=10)
    assert result.returncode == 2
    assert not result.stdout
    assert b"aria-native:" in result.stderr


def test_timeout_kills_worker_and_descendants(native, tmp_path):
    binary, env, _ = native
    marker = tmp_path / "descendant-survived"
    env["PYTHONPATH"] = fake_package(tmp_path, "native_bridge", f"""
import subprocess, sys, time
subprocess.Popen([sys.executable, '-c', {repr('import time; from pathlib import Path; time.sleep(1); Path(' + repr(str(marker)) + ').touch()')}])
print('ready', file=sys.stderr, flush=True)
time.sleep(60)
""")
    process = subprocess.Popen([binary, "--python", sys.executable, "-C", str(tmp_path), "--timeout-ms", "500", "tool", "read_file", "{}"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    output, errors = process.communicate(timeout=5)
    assert b"ready" in errors  # child was actually started before the timeout
    assert process.returncode == 2 and b"timed out" in errors
    assert not output
    time.sleep(1.1)
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal injection; Windows timeout exercises the Job Object")
def test_ctrl_c_cancels_instead_of_waiting_for_tool(native, tmp_path):
    binary, env, _ = native
    env["PYTHONPATH"] = fake_package(tmp_path, "native_bridge", "import sys,time; print('ready',file=sys.stderr,flush=True); time.sleep(60)")
    process = subprocess.Popen([binary, "--python", sys.executable, "-C", str(tmp_path), "tool", "read_file", "{}"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        assert process.stderr.readline().strip() == b"ready"
        process.send_signal(signal.SIGINT)
        output, errors = process.communicate(timeout=5)
        assert process.returncode == 130
        assert not output and b"Cancelled" in errors
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
