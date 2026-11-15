"""Tests for runtime.tool_policy per-tool allowlist/denylist."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch


@pytest.fixture()
def tmp_policy_file(tmp_path, monkeypatch):
    """Patch _policy_file() to use a temp path so tests don't touch ~/.arthera."""
    policy_path = tmp_path / "tool_policy.json"
    import runtime.tool_policy as _tp
    monkeypatch.setattr(_tp, "_policy_file", lambda: policy_path)
    return policy_path


def test_check_returns_default_for_unknown(tmp_policy_file):
    from runtime.tool_policy import check_tool_policy
    assert check_tool_policy("read_file") == "default"


def test_add_to_allowlist(tmp_policy_file):
    from runtime.tool_policy import add_to_policy, check_tool_policy
    add_to_policy("read_file", "allow")
    assert check_tool_policy("read_file") == "allow"


def test_add_to_denylist(tmp_policy_file):
    from runtime.tool_policy import add_to_policy, check_tool_policy
    add_to_policy("run_command", "deny")
    assert check_tool_policy("run_command") == "deny"


def test_add_to_ask_always(tmp_policy_file):
    from runtime.tool_policy import add_to_policy, check_tool_policy
    add_to_policy("write_file", "ask")
    assert check_tool_policy("write_file") == "ask"


def test_moving_between_lists(tmp_policy_file):
    from runtime.tool_policy import add_to_policy, check_tool_policy
    add_to_policy("edit_file", "allow")
    assert check_tool_policy("edit_file") == "allow"
    # Move from allow to deny
    add_to_policy("edit_file", "deny")
    assert check_tool_policy("edit_file") == "deny"
    # It should no longer be in allowed
    from runtime.tool_policy import load_tool_policy
    policy = load_tool_policy()
    assert "edit_file" not in policy["allowed"]


def test_remove_from_policy(tmp_policy_file):
    from runtime.tool_policy import add_to_policy, remove_from_policy, check_tool_policy
    add_to_policy("glob", "deny")
    assert check_tool_policy("glob") == "deny"
    removed = remove_from_policy("glob")
    assert removed is True
    assert check_tool_policy("glob") == "default"


def test_remove_nonexistent_returns_false(tmp_policy_file):
    from runtime.tool_policy import remove_from_policy
    assert remove_from_policy("nonexistent_tool") is False


def test_policy_persists_to_disk(tmp_policy_file):
    from runtime.tool_policy import add_to_policy, load_tool_policy
    add_to_policy("web_fetch", "allow")
    raw = json.loads(tmp_policy_file.read_text())
    assert "web_fetch" in raw["allowed"]


def test_corrupt_file_returns_defaults(tmp_policy_file):
    tmp_policy_file.write_text("not json{{{{")
    from runtime.tool_policy import load_tool_policy
    policy = load_tool_policy()
    assert "allowed" in policy
    assert "denied" in policy
    assert "ask_always" in policy


def test_parameterized_command_rule_allow_and_deny(tmp_policy_file):
    from runtime.tool_policy import add_to_policy, check_tool_policy

    add_to_policy("run_command(pytest *)", "allow")
    add_to_policy("run_command(rm -rf *)", "deny")

    assert check_tool_policy("run_command", {"command": "pytest tests/test_tool_policy.py"}) == "allow"
    assert check_tool_policy("run_command", {"command": "rm -rf /tmp/foo"}) == "deny"
    assert check_tool_policy("run_command", {"command": "ls -la"}) == "default"
    assert check_tool_policy("run_command") == "default"


def test_parameterized_rule_precedence_over_tool_rule(tmp_policy_file):
    from runtime.tool_policy import add_to_policy, check_tool_policy

    add_to_policy("write_file", "ask")
    add_to_policy("write_file(tests/*.py)", "allow")
    add_to_policy("write_file(*.env)", "deny")

    assert check_tool_policy("write_file", {"path": "tests/test_a.py"}) == "allow"
    assert check_tool_policy("write_file", {"path": ".env"}) == "deny"
    assert check_tool_policy("write_file", {"path": "src/main.py"}) == "ask"
