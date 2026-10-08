"""run_command is confined by the OS, not only by checks on the command text.

"workspace-write" and "network off" were enforced by pattern lists: a script
could write anywhere in the home directory and open any connection. On macOS
commands now run under sandbox-exec.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

from aria_code.safety import sandbox

needs_seatbelt = pytest.mark.skipif(not sandbox.available(), reason="needs macOS sandbox-exec")


def test_the_profile_names_the_roots_and_the_network_rule():
    rules = sandbox.profile(['/Users/me/my "repo"'], network=False)
    assert '(subpath "/Users/me/my \\"repo\\"")' in rules
    assert '(deny network-outbound (remote ip "*:*"))' in rules
    assert "network" not in sandbox.profile(["/w"], network=True)


def test_full_access_and_off_are_not_wrapped(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: True)
    assert sandbox.wrap("ls", use_shell=True, mode="full-access", network=True) is None
    assert sandbox.wrap("ls", use_shell=True, mode="workspace-write", network=True, setting="off") is None
    monkeypatch.setenv("ARIA_OS_SANDBOX", "off")
    assert sandbox.wrap("ls", use_shell=True, mode="workspace-write", network=True) is None
    monkeypatch.delenv("ARIA_OS_SANDBOX")
    argv = sandbox.wrap(["ls", "-l"], use_shell=False, mode="workspace-write", network=True)
    assert argv[0] == sandbox.SANDBOX_EXEC and argv[-2:] == ["ls", "-l"]


def test_read_only_does_not_open_the_workspace(tmp_path):
    work = os.path.realpath(tmp_path / "work")
    assert work not in sandbox.writable_roots("read-only", tmp_path / "work")
    roots = sandbox.writable_roots("workspace-write", tmp_path / "work")
    assert work in roots and "/dev" in roots


def test_the_repository_root_is_writable_from_a_subdirectory(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "pkg").mkdir()
    assert os.path.realpath(tmp_path) in sandbox.writable_roots("workspace-write", tmp_path / "pkg")


def run(command, cwd, mode="workspace-write", network=False):
    from aria_code.apps.cli.tools.system_tools import tool_run_command

    return tool_run_command({"command": command, "policy": "full", "cwd": str(cwd),
                             "permission_mode": mode, "network_enabled": network}, has_rich=False)


@pytest.fixture
def outside():
    # Not under a temporary directory: those stay writable.
    base = Path.home() / ".aria-sandbox-test"
    base.mkdir(exist_ok=True)
    yield base
    for p in base.iterdir():
        p.unlink()
    base.rmdir()


@needs_seatbelt
def test_workspace_write_writes_inside_and_not_outside(tmp_path, outside, monkeypatch):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    work = outside / "work"
    work.mkdir()
    try:
        result = run(f"echo in > inside.txt; echo out > {outside}/escaped.txt", work)
        assert (work / "inside.txt").read_text() == "in\n"
        assert not (outside / "escaped.txt").exists()
        assert "workspace-write sandbox" in result["data"]["hint"]
    finally:
        for p in work.iterdir():
            p.unlink()
        work.rmdir()


@needs_seatbelt
def test_network_off_blocks_connections(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    probe = (f"{shlex.quote(sys.executable)} -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=3); "
             "print('connected')\"")
    result = run(probe, tmp_path, network=False)
    assert "connected" not in result["data"]["stdout"]
    assert "Operation not permitted" in result["data"]["stderr"]
    assert "network access" in result["data"]["hint"]


@needs_seatbelt
def test_full_access_is_not_confined(tmp_path, outside, monkeypatch):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    run(f"echo out > {outside}/free.txt", tmp_path, mode="full-access")
    assert (outside / "free.txt").exists()


@needs_seatbelt
def test_command_cwd_cannot_grant_write_access_but_host_add_dir_can(tmp_path, outside, monkeypatch):
    from aria_code.apps.cli.tools.system_tools import tool_run_command
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    project, sibling = outside / "project", outside / "sibling"
    project.mkdir()
    sibling.mkdir()
    params = {"command": "echo modified > app.txt", "policy": "full", "cwd": str(sibling),
              "_workspace": str(project), "permission_mode": "workspace-write", "network_enabled": False}
    try:
        blocked = tool_run_command(params, has_rich=False)
        assert blocked["data"]["exit_code"] != 0
        assert not (sibling / "app.txt").exists()
        granted = tool_run_command({**params, "_allowed_write_roots": [str(sibling)]}, has_rich=False)
        assert granted["data"]["exit_code"] == 0
        assert (sibling / "app.txt").read_text() == "modified\n"
    finally:
        (sibling / "app.txt").unlink(missing_ok=True)
        sibling.rmdir()
        project.rmdir()
