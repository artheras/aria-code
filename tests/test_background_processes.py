"""A long-running command can be started, read, written to and stopped.

run_command waited for every command to finish, so a dev server blocked the
turn until the timeout and there was no way to start one and test against it.
"""

from __future__ import annotations

import shlex
import sys
import time
import os
import subprocess

import pytest

from aria_code.runtime import processes

PY = subprocess.list2cmdline([sys.executable]) if os.name == "nt" else shlex.quote(sys.executable)


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    yield
    processes.stop_all()
    processes._processes.clear()


def start(command, cwd, **extra):
    from aria_code.apps.cli.tools.system_tools import tool_run_command

    return tool_run_command({"command": command, "background": True, "policy": "balanced",
                             "cwd": str(cwd), "permission_mode": "workspace-write",
                             "network_enabled": False, **extra}, has_rich=False)


SERVER = (f"{PY} -u -c \"import time; print('booting'); time.sleep(0.5); print('Listening on 8000');"
          " [print('tick', i) or time.sleep(0.2) for i in range(200)]\"")


def test_start_returns_at_once_and_waits_for_the_ready_line(tmp_path):
    # Time process startup, not the interpreter's first module import.
    from aria_code.apps.cli.tools.system_tools import tool_run_command  # noqa: F401
    began = time.monotonic()
    result = start(SERVER, tmp_path, until="Listening on", wait_seconds=10)
    data = result["data"]
    assert result["success"] and data["background"] and data["running"]
    assert data["matched"] and "Listening on 8000" in data["output"]
    assert time.monotonic() - began < 5


def test_output_is_what_came_since_the_last_read(tmp_path):
    pid = start(SERVER, tmp_path, until="Listening on", wait_seconds=10)["data"]["process_id"]
    first = processes.tool_process({"action": "output", "process_id": pid, "wait_seconds": 2})["data"]
    second = processes.tool_process({"action": "output", "process_id": pid, "wait_seconds": 2})["data"]
    assert "tick" in first["output"] and "tick" in second["output"]
    assert "Listening on" not in first["output"] + second["output"]


def test_input_reaches_the_program(tmp_path):
    echo = f"{PY} -u -c \"import sys; [print('got', line.strip()) for line in sys.stdin]\""
    pid = start(echo, tmp_path, wait_seconds=0.5)["data"]["process_id"]
    reply = processes.tool_process({"action": "input", "process_id": pid, "text": "hello\n",
                                    "wait_seconds": 3})["data"]
    assert "got hello" in reply["output"]


def test_stop_ends_the_process_and_its_children(tmp_path):
    parent = f"{PY} -u -c \"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); print('up'); time.sleep(60)\""
    pid = start(parent, tmp_path, until="up", wait_seconds=10)["data"]["process_id"]
    proc = processes._processes[pid]
    stopped = processes.tool_process({"action": "stop", "process_id": pid})["data"]
    assert stopped["stopped"] and not stopped["running"]
    assert not alive(proc.popen.pid)


def alive(pid):
    """A zombie cannot execute or hold a port, even before PID 1 reaps it."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            status = wintypes.DWORD()
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(status)) and status.value == 259)
        finally:
            api.CloseHandle(handle)
    status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=3).stdout.strip()
    return bool(status and not status.startswith(("Z", "X")))


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal/process-group regression")
@pytest.mark.parametrize("leader_exits", [False, True])
def test_term_ignoring_child_is_stopped_even_if_its_leader_exited(tmp_path, leader_exits):
    child = ("import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
             "print('CHILD_READY',os.getpid(),flush=True); time.sleep(60)")
    parent = (f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-u','-c',{child!r}]); "
              + ("sys.exit(0)" if leader_exits else "time.sleep(60)"))
    data = processes.start([sys.executable, "-u", "-c", parent], shell=False,
                           cwd=str(tmp_path), label="child cleanup regression", wait=5,
                           until="CHILD_READY [0-9]+\\n")["data"]
    child_pid = int(data["output"].split("CHILD_READY ")[1].splitlines()[0])
    proc = processes._processes[data["process_id"]]
    if leader_exits:
        proc.popen.wait(timeout=3)
    try:
        assert alive(child_pid)
        assert processes.listing()["data"]["processes"][0]["running"]
        began = time.monotonic()
        stopped = processes.stop(proc.id, grace=0.1)
        assert time.monotonic() - began < 4
        assert stopped["success"] and stopped["data"]["stopped"]
        assert not alive(child_pid)
        assert processes.stop(proc.id, grace=0.1)["data"]["stopped"]
    finally:
        if alive(child_pid):
            os.kill(child_pid, __import__("signal").SIGKILL)


def test_exit_cleanup_includes_children_of_exited_leaders(tmp_path):
    child = "import os,time; print('CHILD_READY',os.getpid(),flush=True); time.sleep(60)"
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-u','-c',{child!r}])"
    data = processes.start([sys.executable, "-u", "-c", parent], shell=False,
                           cwd=str(tmp_path), label="exit cleanup", wait=5,
                           until="CHILD_READY [0-9]+\\n")["data"]
    child_pid = int(data["output"].split("CHILD_READY ")[1].splitlines()[0])
    processes._processes[data["process_id"]].popen.wait(timeout=3)
    processes.stop_all()
    assert not alive(child_pid)


def test_a_command_that_ends_reports_its_exit_code(tmp_path):
    data = start(f"{PY} -c \"print('done')\"", tmp_path, wait_seconds=5)["data"]
    assert data["running"] is False and data["exit_code"] == 0 and "done" in data["output"]


def test_list_and_unknown_ids(tmp_path):
    pid = start(SERVER, tmp_path, wait_seconds=0.2)["data"]["process_id"]
    rows = processes.tool_process({"action": "list"})["data"]["processes"]
    assert [r["process_id"] for r in rows] == [pid]
    missing = processes.tool_process({"action": "output", "process_id": "p999"})
    assert not missing["success"] and pid in missing["error"]


def test_too_many_running_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(processes, "MAX_RUNNING", 1)
    start(SERVER, tmp_path, wait_seconds=0.1)
    second = start(SERVER, tmp_path, wait_seconds=0.1)
    assert not second["success"] and "already running" in second["error"]


def test_the_policy_still_applies(tmp_path):
    blocked = start("curl https://example.com", tmp_path)
    assert not blocked["success"]


@pytest.mark.skipif(not __import__("aria_code.safety.sandbox", fromlist=["available"]).available(),
                    reason="needs macOS sandbox-exec")
def test_background_commands_run_in_the_sandbox(tmp_path):
    from pathlib import Path

    target = Path.home() / ".aria-bg-sandbox-test.txt"
    data = start(f"echo hi > {target}", tmp_path, wait_seconds=5)["data"]
    assert not target.exists()
    assert data["exit_code"] != 0 and "sandbox" in data.get("hint", "")


def test_the_model_is_offered_the_tool_and_the_flag():
    from aria_code import aria_cli

    names = {s["function"]["name"]: s["function"] for s in aria_cli.LOCAL_TOOL_SCHEMAS if "function" in s}
    assert "process" in names and "process" in aria_cli.LOCAL_TOOLS
    assert "background" in names["run_command"]["parameters"]["properties"]
