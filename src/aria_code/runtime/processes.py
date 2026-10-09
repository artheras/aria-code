"""Long-running commands: start one, read its output later, send it input, stop it.

run_command waited for a command to finish, so a dev server, a test watcher
or anything interactive either blocked the turn until the timeout or could
not be used at all: there was no way to start a server and then test against
it. ``run_command`` with ``background: true`` starts the process here, under
the same approval and sandbox as any command, and the ``process`` tool reads
what it has printed since the last look, writes to its stdin, or stops it.

Output goes through pipes, not a terminal, so a program that insists on a TTY
behaves as it does when piped. Every process gets its own process group, so
stopping it stops what it started; all of them are stopped when Aria exits.
"""

from __future__ import annotations

import atexit
import itertools
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

MAX_RUNNING = 8
MAX_BUFFER = 1_000_000         # characters kept per process; older output is dropped
MAX_READ = 8_000               # characters returned by one read
MAX_WAIT = 30.0

_ids = itertools.count(1)
_lock = threading.Lock()


@dataclass
class _Process:
    id: str
    command: str
    cwd: str
    popen: subprocess.Popen
    started: float = field(default_factory=time.time)
    text: str = ""             # the kept tail of everything printed
    dropped: int = 0           # characters dropped from the front of `text`
    read_upto: int = 0         # absolute position the caller has read to
    done: bool = False         # exited, and everything it printed has been read in
    changed: threading.Condition = field(default_factory=threading.Condition)
    stop_lock: threading.Lock = field(default_factory=threading.Lock)
    group_cleaned: bool = False
    windows_job: object | None = None

    @property
    def running(self) -> bool:
        return _group_running(self)

    def end(self) -> int:
        return self.dropped + len(self.text)


_processes: dict[str, _Process] = {}


def _group_running(proc: _Process) -> bool:
    """The group may outlive its leader. Zombies have already stopped.

    Linux CI's PID 1 may not reap orphan zombies promptly; killpg(..., 0)
    alone therefore cannot tell whether a command is still executing.
    """
    if proc.group_cleaned:
        return False
    proc.popen.poll()  # reap our own child even while its children hold stdout
    if os.name == "nt":
        if proc.windows_job is not None:
            active = proc.windows_job.running()
            if not active:
                proc.windows_job.close()
                proc.group_cleaned = True
            return active
        return proc.popen.returncode is None
    try:
        os.killpg(proc.popen.pid, 0)
    except ProcessLookupError:
        proc.group_cleaned = True
        return False
    except PermissionError:
        return True
    if Path("/proc/self/stat").exists():
        seen = False
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) != proc.popen.pid:
                    continue
                seen = True
                if fields[0] not in ("Z", "X"):
                    return True
            except (OSError, ValueError, IndexError):
                continue
        if seen:
            proc.group_cleaned = True
            return False
    return True


def _wait_group(proc: _Process, seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while _group_running(proc):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True


def _pump(proc: _Process) -> None:
    stream = proc.popen.stdout
    try:
        while True:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            with proc.changed:
                proc.text += text
                if len(proc.text) > MAX_BUFFER:
                    cut = len(proc.text) - MAX_BUFFER
                    proc.text = proc.text[cut:]
                    proc.dropped += cut
                proc.changed.notify_all()
    except (OSError, ValueError):
        pass
    finally:
        proc.popen.wait()
        stream.close()
        with proc.changed:
            proc.done = True
            proc.changed.notify_all()


def start(argv: str | Sequence[str], *, shell: bool, cwd: str | None, label: str,
          wait: float = 3.0, until: str | None = None) -> dict:
    with _lock:
        running = [p for p in _processes.values() if p.running]
        if len(running) >= MAX_RUNNING:
            ids = ", ".join(p.id for p in running)
            return {"success": False, "error": f"{MAX_RUNNING} background processes are already running "
                                               f"({ids}). Stop one with the process tool first."}
        popen = subprocess.Popen(
            argv, shell=shell, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
            # The command must not spawn children before the Job owns it.
            creationflags=0x4 if os.name == "nt" else 0,  # CREATE_SUSPENDED
        )
        proc = _Process(id=f"p{next(_ids)}", command=label, cwd=cwd or os.getcwd(), popen=popen)
        if os.name == "nt":
            from .windows_job import WindowsJob
            try:
                proc.windows_job = WindowsJob(popen)
                proc.windows_job.resume(popen.pid)
            except OSError as exc:
                if proc.windows_job is not None:
                    proc.windows_job.close()
                popen.kill()
                popen.communicate(timeout=5)
                return {"success": False, "error": f"Cannot manage the background process tree: {exc}"}
        _processes[proc.id] = proc
    threading.Thread(target=_pump, args=(proc,), daemon=True, name=f"aria-{proc.id}").start()
    return {"success": True, "data": read(proc.id, wait=wait, until=until)["data"] | {"background": True}}


def _get(process_id: str) -> _Process | None:
    return _processes.get(str(process_id or "").strip())


def _unknown(process_id: str) -> dict:
    known = ", ".join(_processes) or "none"
    return {"success": False, "error": f"No background process '{process_id}' (known: {known})."}


def read(process_id: str, *, wait: float = 0.0, until: str | None = None) -> dict:
    """Output printed since the last read.

    Waits up to ``wait`` seconds, returning early when the process exits or,
    with ``until``, when that regular expression appears in the new output (a
    server's "Listening on"). Returning at the first byte gave half a line
    and a process that had not yet exited.
    """
    proc = _get(process_id)
    if proc is None:
        return _unknown(process_id)
    pattern = None
    if until:
        try:
            pattern = re.compile(until)
        except re.error as exc:
            return {"success": False, "error": f"Invalid 'until' pattern: {exc}"}
    deadline = time.monotonic() + max(0.0, min(float(wait or 0), MAX_WAIT))

    def ready() -> bool:
        if proc.done:
            return True
        return bool(pattern and pattern.search(proc.text[max(0, proc.read_upto - proc.dropped):]))

    with proc.changed:
        while not ready():
            left = deadline - time.monotonic()
            if left <= 0:
                break
            proc.changed.wait(left)
        start_at = max(proc.read_upto, proc.dropped)
        missed = start_at - proc.read_upto
        new = proc.text[start_at - proc.dropped:]
        proc.read_upto = proc.end()
    clipped = len(new) > MAX_READ
    data = {
        "process_id": proc.id,
        "command": proc.command,
        "running": proc.running,
        "exit_code": proc.popen.returncode,
        "output": new[-MAX_READ:],
    }
    if clipped or missed:
        data["output_dropped_chars"] = missed + (len(new) - MAX_READ if clipped else 0)
    if pattern is not None:
        data["matched"] = bool(pattern.search(new))
    return {"success": True, "data": data}


def write(process_id: str, text: str, *, wait: float = 1.0) -> dict:
    proc = _get(process_id)
    if proc is None:
        return _unknown(process_id)
    if not proc.running:
        return {"success": False, "error": f"{proc.id} has exited (code {proc.popen.returncode})."}
    try:
        proc.popen.stdin.write(str(text).encode("utf-8"))
        proc.popen.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        return {"success": False, "error": f"Could not write to {proc.id}: {exc}"}
    return read(proc.id, wait=wait)


def stop(process_id: str, *, grace: float = 3.0) -> dict:
    proc = _get(process_id)
    if proc is None:
        return _unknown(process_id)
    error = ""
    with proc.stop_lock:
        if os.name == "nt" and proc.running:
            try:
                proc.windows_job.terminate()
                _wait_group(proc, 2.0)
            except OSError as exc:
                error = str(exc)
        elif os.name != "nt":
            for sig, pause in ((signal.SIGTERM, min(max(float(grace), 0), 30)),
                               (signal.SIGKILL, 2.0)):
                if not proc.running:
                    break
                try:
                    os.killpg(proc.popen.pid, sig)
                except ProcessLookupError:
                    proc.group_cleaned = True
                    break
                except PermissionError as exc:
                    error = str(exc)
                    break
                if _wait_group(proc, pause):
                    break
        if proc.popen.poll() is not None and proc.popen.stdin:
            proc.popen.stdin.close()
    result = read(proc.id, wait=0.5)
    result["data"]["stopped"] = not proc.running
    if error or proc.running:
        result.update(success=False, error=error or "The process group did not stop within the deadline.")
    return result


def listing() -> dict:
    rows = [{
        "process_id": p.id, "command": p.command, "cwd": p.cwd, "running": p.running,
        "exit_code": p.popen.returncode, "seconds": round(time.time() - p.started, 1),
        "unread_chars": max(0, p.end() - max(p.read_upto, p.dropped)),
    } for p in _processes.values()]
    return {"success": True, "data": {"processes": rows}}


def stop_all() -> None:
    for proc in list(_processes.values()):
        if proc.running:
            stop(proc.id, grace=1.0)


atexit.register(stop_all)


def tool_process(params: dict) -> dict:
    """The ``process`` tool: output | input | stop | list."""
    action = str(params.get("action") or "output").strip().lower()
    pid = params.get("process_id", "")
    if action == "list":
        return listing()
    if action == "output":
        return read(pid, wait=params.get("wait_seconds", 2), until=params.get("until"))
    if action == "input":
        if "text" not in params:
            return {"success": False, "error": "input needs 'text' (include \\n to press Enter)."}
        return write(pid, params["text"], wait=params.get("wait_seconds", 1))
    if action == "stop":
        return stop(pid)
    return {"success": False, "error": f"Unknown action '{action}': use output, input, stop or list."}


PROCESS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "process",
        "description": (
            "Manage a command started with run_command background=true (a dev server, a watcher, an "
            "interactive program). action=output returns what it printed since the last read, "
            "waiting up to wait_seconds, or until a regex `until` appears; action=input writes `text` "
            "to its stdin (include \\n for Enter); action=stop ends it and everything it started; "
            "action=list shows all background processes. Stop servers when you are done with them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["output", "input", "stop", "list"]},
                "process_id": {"type": "string", "description": "The id run_command returned, e.g. p1"},
                "text": {"type": "string", "description": "For input: the text to send"},
                "wait_seconds": {"type": "number", "description": "How long to wait for output (max 30)"},
                "until": {"type": "string", "description": "For output: a regex to wait for, e.g. 'Listening on'"},
            },
            "required": ["action"],
        },
    },
}

__all__ = ["start", "read", "write", "stop", "listing", "stop_all", "tool_process", "PROCESS_SCHEMA"]
