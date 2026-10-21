"""The screen, the mouse and the keyboard are asked about, and never approved in bulk."""

import sys

import pytest

from aria_code.safety.risk import approval_requirement, assess_tool


@pytest.mark.parametrize("tool, params, level", [
    ("computer_screenshot", {}, 3),
    ("computer_action", {"action": "move", "x": 1, "y": 2}, 2),
    ("computer_action", {"action": "scroll", "dy": 3}, 2),
    ("computer_action", {"action": "click", "x": 1, "y": 2}, 3),
    ("computer_action", {"action": "right_click", "x": 1, "y": 2}, 3),
    ("computer_action", {"action": "drag", "x": 1, "y": 2, "ex": 5, "ey": 6}, 3),
    ("computer_action", {"action": "type", "text": "hello"}, 3),
    ("computer_action", {"action": "type", "text": "make deploy\n"}, 4),
    ("computer_action", {"action": "key", "key": "enter"}, 3),
    ("computer_action", {"action": "key", "key": "cmd+q"}, 4),
    ("computer_action", {"action": "key", "key": "Command+Option+Esc"}, 4),
    ("computer_action", {"action": "key", "key": "alt+f4"}, 4),
    ("computer_action", {}, 3),
    ("browser_navigate", {"url": "https://example.com"}, 2),
    ("browser_screenshot", {"url": "https://example.com"}, 2),
])
def test_levels(tool, params, level):
    assert assess_tool(tool, params).level == level


def test_driving_the_desktop_is_not_reversible_and_names_its_capability():
    click = assess_tool("computer_action", {"action": "click", "x": 1, "y": 1})
    assert "desktop.control" in click.capabilities and not click.reversible
    assert "screen.capture" in assess_tool("computer_screenshot", {}).capabilities


@pytest.mark.parametrize("params", [{"action": "click"}, {"action": "type", "text": "x"}])
def test_risk_mode_never_runs_desktop_input_on_its_own(params):
    assessment = assess_tool("computer_action", params)
    assert approval_requirement(assessment, mode="risk", auto_level=9) == "ask"


@pytest.fixture
def cli(monkeypatch):
    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli

    asked = []

    def fake_select(options, selected=0, title="", **kwargs):
        asked.append([label for label, _ in options])
        return len(options) - 1   # the last option is "No"

    monkeypatch.setattr(cli, "_arrow_select", fake_select)
    monkeypatch.setattr(cli, "HAS_RICH", False)
    monkeypatch.setattr(cli, "_ACTIVE_APPROVAL_MODE", ["manual"])
    monkeypatch.setattr(cli, "_ACTIVE_AUTO_APPROVE_LEVEL", [1])
    monkeypatch.setattr(cli, "_auto_approve_session", False)
    monkeypatch.setattr(cli, "_ARIA_BOT_MODE", False)
    monkeypatch.setattr(cli, "check_tool_policy", lambda name: "default")
    cli._session_always_allow.clear()
    cli._asked = asked
    yield cli
    cli._session_always_allow.clear()


CLICK = {"action": "click", "x": 10, "y": 20}


def test_a_click_asks_and_cannot_be_approved_in_bulk(cli):
    decision = cli._confirm_tool_execution_decision("computer_action", CLICK)
    assert not decision.approved and cli._asked
    assert not any("allow all tools" in label for label in cli._asked[0]), cli._asked[0]


def test_allow_all_tools_for_the_session_does_not_cover_the_desktop(cli, monkeypatch):
    monkeypatch.setattr(cli, "_auto_approve_session", True)
    assert not cli._confirm_tool_execution_decision("computer_action", CLICK).approved
    assert not cli._confirm_tool_execution_decision("computer_screenshot", {}).approved
    assert len(cli._asked) == 2


def test_a_standing_allow_policy_does_not_cover_the_desktop(cli, monkeypatch):
    monkeypatch.setattr(cli, "check_tool_policy", lambda name: "allow")
    assert not cli._confirm_tool_execution_decision("computer_action", CLICK).approved and cli._asked


def test_an_explicit_per_tool_choice_still_works_but_not_for_a_destructive_key(cli):
    cli._session_always_allow.add("computer_action")
    assert cli._confirm_tool_execution_decision("computer_action", CLICK).approved and cli._asked == []
    assert not cli._confirm_tool_execution_decision("computer_action", {"action": "key", "key": "cmd+q"}).approved
    assert cli._asked, "a session-wide yes must not cover an L4 key combination"


def test_a_chat_bot_cannot_drive_the_desktop(cli, monkeypatch):
    monkeypatch.setattr(cli, "_ARIA_BOT_MODE", True)
    cli._session_always_allow.add("computer_action")
    decision = cli._confirm_tool_execution_decision("computer_action", CLICK)
    assert not decision.approved and cli._asked == []


def test_the_prompt_shows_the_action_but_never_the_typed_text():
    from aria_code.apps.cli.runtime_consumer import approval_subject

    assert approval_subject("computer_action", CLICK) == "click at (10, 20)"
    assert approval_subject("computer_action", {"action": "key", "key": "cmd+q"}) == "press cmd+q"
    typed = approval_subject("computer_action", {"action": "type", "text": "hunter2-password"})
    assert typed == "type 16 characters" and "hunter2" not in typed
