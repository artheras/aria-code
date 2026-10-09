"""Approval by risk: what an action touches decides how it is approved."""

import sys

import pytest

from aria_code.safety.risk import approval_requirement, assess_command, assess_tool


@pytest.mark.parametrize("command, mode, level, expected", [
    ("pytest -q", "manual", 1, "ask"),
    ("python3 script.py", "risk", 1, "auto"),          # L1
    ("npm install jose", "risk", 1, "ask"),            # L2 above the level
    ("npm install jose", "risk", 2, "auto"),
    ("git push", "risk", 2, "ask"),                    # L3 is never automatic
    ("git push", "risk", 9, "ask"),                    # the level is capped
    ("rm -rf build", "risk", 2, "always"),             # L4 always asks
    ("rm -rf build", "manual", 1, "always"),
])
def test_requirement(command, mode, level, expected):
    assert approval_requirement(assess_command(command), mode=mode, auto_level=level) == expected


def test_a_bad_level_falls_back_to_one():
    assert approval_requirement(assess_command("python3 x.py"), mode="risk", auto_level="x") == "auto"
    assert approval_requirement(assess_command("npm i x"), mode="risk", auto_level=None) == "ask"


def test_the_card_names_the_blast_radius():
    from aria_code.ui.render.output import format_risk_card

    lines = [line for _, line in format_risk_card(assess_command("npm install jose"))]
    assert lines[0].startswith("Risk     L2 medium · ") and lines[0].endswith("reversible")
    assert "Network  registry.npmjs.org" in lines
    assert "Files    package.json, package-lock.json" in lines
    styles = dict((line, style) for style, line in format_risk_card(assess_command("git push --force")))
    assert any(style == "bold red" for style in styles.values())


@pytest.fixture
def cli(monkeypatch):
    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli

    asked = []

    def fake_select(options, selected=0, title="", **kwargs):
        asked.append([label for label, _ in options])
        return len(options) - 1   # "No"

    monkeypatch.setattr(cli, "_arrow_select", fake_select)
    monkeypatch.setattr(cli, "HAS_RICH", False)
    monkeypatch.setattr(cli, "_ACTIVE_APPROVAL_MODE", ["manual"])
    monkeypatch.setattr(cli, "_ACTIVE_AUTO_APPROVE_LEVEL", [1])
    monkeypatch.setattr(cli, "_auto_approve_session", False)
    monkeypatch.setattr(cli, "check_tool_policy", lambda name: "default")
    cli._session_always_allow.clear()
    cli._session_command_prefixes.clear()
    cli._asked = asked
    yield cli
    cli._session_always_allow.clear()
    cli._session_command_prefixes.clear()


def test_risk_mode_runs_a_workspace_command_without_asking(cli, capsys):
    cli._ACTIVE_APPROVAL_MODE[0] = "risk"
    decision = cli._confirm_tool_execution_decision("run_command", {"command": "python3 build.py"},
                                                    config_policy="safe")
    assert decision.approved and decision.user_approved and decision.policy == "balanced"
    assert cli._asked == []
    assert "auto-approved · L1 low" in capsys.readouterr().out


def test_risk_mode_still_asks_above_the_level(cli):
    cli._ACTIVE_APPROVAL_MODE[0] = "risk"
    decision = cli._confirm_tool_execution_decision("run_command", {"command": "npm install jose"},
                                                    config_policy="balanced")
    assert not decision.approved and cli._asked


def test_manual_mode_is_unchanged(cli):
    cli._confirm_tool_execution_decision("run_command", {"command": "python3 build.py"}, config_policy="safe")
    assert cli._asked


def test_an_always_allow_does_not_cover_a_critical_write(cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli._session_always_allow.add("write_file")
    ordinary = cli._confirm_tool_execution_decision("write_file", {"path": "src/a.py", "content": "x"})
    assert ordinary.approved and cli._asked == []
    secret = cli._confirm_tool_execution_decision("write_file", {"path": ".env", "content": "KEY=1"})
    assert not secret.approved and cli._asked, "a session-wide yes must not cover an L4 write"
    assert assess_tool("write_file", {"path": ".env"}, root=tmp_path).level == 4


def test_the_card_is_shown_before_the_prompt(cli, capsys):
    cli._confirm_tool_execution_decision("run_command", {"command": "npm install jose"}, config_policy="balanced")
    out = capsys.readouterr().out
    assert "Risk     L2 medium" in out and "registry.npmjs.org" in out
