"""Two REPL turns, then /rewind: files, conversation and verdicts line up."""

import io
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_rewinding_turns_through_the_repl(tmp_path, monkeypatch):
    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli
    import apps.cli.providers.runtime_bridge as bridge
    import apps.cli.transactions as transactions
    from rich.console import Console
    from aria_code.runtime.approval import ApprovalDecision
    from aria_code.runtime.transactions import TransactionStore

    monkeypatch.chdir(tmp_path)
    transactions.reset_for_tests(TransactionStore(tmp_path / "tx"))
    app = tmp_path / "app.py"
    version = "def version():\n    return {}\n".format
    app.write_text(version(0))
    plan = iter([(version(1), True), (version(2), False)])

    async def fake_turn(**kwargs):
        content, verified = next(plan)
        executor = bridge.build_tool_executor(cli.LOCAL_TOOLS, kwargs["config"], kwargs["execution_context"])
        written = executor.execute_local("write_file", {"path": "app.py", "content": content},
                                         approval=ApprovalDecision.allow(policy="balanced", user_approved=True))
        assert written.get("success"), written
        final = SimpleNamespace(success=True, cancelled=False, delivery=None, provider="test",
                                acceptance={"paths": [str(app)], "verified": verified},
                                stop_reason="completed", error="", tools=["write_file"], sources=[],
                                metadata=SimpleNamespace(prompt_tokens=1, completion_tokens=1, thinking_tokens=0))
        return SimpleNamespace(text="wrote it", final=final, error=None, cancelled=False, ok=True)

    out = io.StringIO()
    monkeypatch.setattr(bridge, "run_chat_via_runtime", fake_turn)
    monkeypatch.setattr(cli, "console", Console(file=out, width=120))
    terminal = cli.ArtheraTerminal(dict(cli.DEFAULT_CONFIG, model="claude-sonnet-4-5",
                                        permission_mode="workspace-write"))
    terminal.config["_session_workspace_root"] = str(tmp_path)
    monkeypatch.setattr(terminal.commands.context, "console", Console(file=out, width=120))

    await terminal.send_message("first change to app.py")
    await terminal.send_message("second change to app.py")
    assert app.read_text() == version(2)
    assert len(terminal.conversation) == 4

    terminal.commands.cmd_rewind("turns")
    listing = out.getvalue()
    assert "before turn 2" in listing and "before turn 1" in listing

    terminal.commands.cmd_rewind("turn --yes")
    assert app.read_text() == version(1)
    assert [m["role"] for m in terminal.conversation] == ["user", "assistant"]
    assert terminal._code_verdict == "passed"

    # The turn that was undone is gone from history; turn 1's point remains.
    assert [p.turn for p in transactions.store().list(terminal.session_id)] == [1]
    terminal.commands.cmd_rewind("turn 1 --yes")
    assert app.read_text() == version(0) and terminal.conversation == []
    transactions.reset_for_tests(None)


def test_green_finds_the_last_point_whose_checks_passed(tmp_path):
    from aria_code.runtime.transactions import TransactionStore, new_point

    store = TransactionStore(tmp_path)
    store.record(new_point(session_id="s", turn=1, prompt="a", verdict=""))
    green = store.record(new_point(session_id="s", turn=2, prompt="b", verdict="passed"))
    store.record(new_point(session_id="s", turn=3, prompt="c", verdict="failed"))
    assert store.latest_green("s").point_id == green.point_id
