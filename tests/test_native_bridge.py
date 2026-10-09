"""Contract and authority regressions for the opt-in Rust/Python boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from aria_code.apps.cli.native_bridge import MAX_MESSAGE, dispatch


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIA_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ARIA_USER_OUTPUT_ROOT", str(tmp_path / "outputs"))


def request(name="read_file", **arguments):
    return {"jsonrpc": "2.0", "id": "request-你好", "method": "tools.call",
            "params": {"name": name, "arguments": arguments}}


def test_protocol_errors_and_notifications(tmp_path):
    for invalid in (None, [], {"jsonrpc": "1.0", "method": "tools.call"},
                    {"jsonrpc": "2.0", "id": True, "method": "tools.call"}):
        assert dispatch(invalid, tmp_path)["error"]["code"] == -32600
    assert dispatch({"jsonrpc": "2.0", "id": 1, "method": "unknown"}, tmp_path)["error"]["code"] == -32601
    assert dispatch({"jsonrpc": "2.0", "id": None, "method": "tools.call", "params": []}, tmp_path)["error"]["code"] == -32602
    note = request("write_file", path="note.txt", content="a notification cannot write this")
    del note["id"]
    assert dispatch(note, tmp_path, "write_file") is None
    assert not (tmp_path / "note.txt").exists()


def test_reads_use_existing_handler_and_preserve_id(tmp_path):
    cli_loaded_before = "aria_code.aria_cli" in sys.modules
    (tmp_path / "你好.txt").write_text("hello 你好\n", encoding="utf-8")
    result = dispatch(request(path="你好.txt"), tmp_path)
    assert result["id"] == "request-你好"
    assert result["result"]["success"]
    assert "hello 你好" in result["result"]["data"]["content"]
    assert cli_loaded_before or "aria_code.aria_cli" not in sys.modules


def test_request_cannot_expand_workspace_or_grant_approval(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    arguments = {"path": "../private.txt", "_workspace": str(tmp_path),
                 "_allowed_read_roots": [str(tmp_path)], "_permission_mode": "full-access"}
    result = dispatch(request(**arguments), workspace)
    assert not result["result"]["success"]
    assert "private" not in result["result"].get("data", {}).get("content", "")
    forged = dispatch(request("write_file", path="new.txt", content="a complete text file to write",
                              _skip_confirm=True, approved=True), workspace)
    assert not forged["result"]["success"]
    assert not (workspace / "new.txt").exists()


def test_symlinks_cannot_escape_workspace(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    try:
        (workspace / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    result = dispatch(request(path="link.txt"), workspace)
    assert not result["result"]["success"]


def test_persistent_deny_wins_over_host_approval(tmp_path):
    state = Path(os.environ["ARIA_HOME"])
    state.mkdir()
    (state / "tool_policy.json").write_text(json.dumps({"denied": ["write_file", "read_file"]}))
    assert not dispatch(request(path="README.md"), tmp_path)["result"]["success"]
    result = dispatch(request("write_file", path="new.txt", content="a complete text file to write"), tmp_path, "write_file")
    assert not result["result"]["success"]
    assert not (tmp_path / "new.txt").exists()


def test_ask_always_does_not_execute_without_host_approval(tmp_path):
    state = Path(os.environ["ARIA_HOME"])
    state.mkdir()
    (state / "tool_policy.json").write_text(json.dumps({"ask_always": ["read_file"]}))
    result = dispatch(request(path="README.md"), tmp_path)
    assert not result["result"]["success"]
    assert "approval" in result["result"]["error"]


@pytest.mark.parametrize("raw,code", [(b"{bad\n", -32700), (b"x" * (MAX_MESSAGE + 1), -32600)], ids=["malformed", "oversized"])
def test_worker_bounds_input_and_stdout_is_protocol(tmp_path, raw, code):
    proc = subprocess.run([sys.executable, "-m", "aria_code.apps.cli.native_bridge", "--workspace", str(tmp_path)],
                          input=raw, capture_output=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["error"]["code"] == code
