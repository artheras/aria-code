"""The Python daemon leaves price alerts and schedules to `aria-runtime daemon` while it runs."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest


@pytest.fixture()
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIA_DAEMON_DIR", str(tmp_path))
    import aria_daemon

    monkeypatch.setattr(aria_daemon, "_ARIA_DIR", tmp_path)
    return aria_daemon


def test_go_daemon_detection(daemon, tmp_path):
    assert daemon._go_daemon_running() is False
    pid_file = tmp_path / "daemon-go.pid"
    pid_file.write_text(str(os.getpid()))
    assert daemon._go_daemon_running() is False, "its own pid is not another daemon"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pid_file.write_text(str(child.pid))
        assert daemon._go_daemon_running() is True
    finally:
        child.kill()
        child.wait()
    assert daemon._go_daemon_running() is False, "a stale pid file means no Go daemon"
    pid_file.write_text("not a pid")
    assert daemon._go_daemon_running() is False


def test_scheduled_jobs_are_skipped_while_go_runs(daemon, monkeypatch):
    calls = []

    async def brief():
        calls.append("brief")
        return "brief"

    monkeypatch.setattr(daemon, "_run_morning_brief", brief)
    monkeypatch.setattr(daemon, "_go_daemon_running", lambda: True)
    asyncio.run(daemon._run_scheduled_job({"id": "s1", "command": "morning-brief", "channels": "[]"}))
    asyncio.run(daemon._run_morning_brief_and_push())
    assert calls == [], "nothing runs twice"
