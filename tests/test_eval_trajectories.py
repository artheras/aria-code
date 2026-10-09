"""Each eval attempt is kept as a trajectory: the prompt, every tool call and result, the outcome.

That sequence is what an agent model learns from. `-p --format jsonl` emits
it; in full mode (ARIA_EVENTS_FULL=1, set by the eval runner) parameters and
tool results are kept whole, with secret-shaped values masked and indentation
intact.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

from aria_code.apps.cli.exec_events import ExecEvents
from aria_code.evals.runner import Trajectories


def test_full_mode_keeps_code_and_results(monkeypatch):
    monkeypatch.setenv("ARIA_EVENTS_FULL", "1")
    out = io.StringIO()
    events = ExecEvents(out)
    events.tool_started("write_file", {"path": "a.py", "content": "def f():\n    return 1\n" * 300, "_run_id": "r"})
    events.tool_completed("run_command", {"success": True, "data": {"stdout": "token=abc123secret\n5\n",
                                                                   "exit_code": 0}})
    started, completed = [json.loads(line) for line in out.getvalue().splitlines()]
    assert started["params"]["content"].startswith("def f():\n    return 1\n")
    assert len(started["params"]["content"]) == len("def f():\n    return 1\n") * 300
    assert "_run_id" not in started["params"]
    assert completed["result"]["data"]["stdout"] == "token=***\n5\n"


def test_the_default_view_is_unchanged(monkeypatch):
    monkeypatch.delenv("ARIA_EVENTS_FULL", raising=False)
    out = io.StringIO()
    events = ExecEvents(out)
    events.tool_started("write_file", {"path": "a.py", "content": "x" * 5000})
    events.tool_completed("run_command", {"success": True, "data": {"stdout": "5"}})
    started, completed = [json.loads(line) for line in out.getvalue().splitlines()]
    assert started["params"]["content"] == "<5000 chars>" and "result" not in completed


def test_trajectories_are_one_file_per_attempt_closed_with_the_outcome(tmp_path):
    store = Trajectories(tmp_path, "operations", "google/gemini-3.5-flash")
    events = '{"type": "turn.started"}\nnoise from the screen\n{"type": "turn.completed", "success": true}\n'
    first = store.record("inventory-reorder", "write reorder.json", events)
    second = store.record("inventory-reorder", "write reorder.json", events)
    store.close([
        SimpleNamespace(task_id="inventory-reorder", outcome="fail", detail="exited 1", changed=(), seconds=17.0),
        SimpleNamespace(task_id="inventory-reorder", outcome="pass", detail="", changed=("reorder.json",),
                        seconds=39.0),
    ])
    assert first.name == "inventory-reorder-1.jsonl" and second.name == "inventory-reorder-2.jsonl"
    lines = [json.loads(line) for line in Path(second).read_text().splitlines()]
    assert lines[0] == {"type": "eval.task", "task_id": "inventory-reorder", "attempt": 2,
                        "model": "google/gemini-3.5-flash", "prompt": "write reorder.json"}
    assert [l["type"] for l in lines] == ["eval.task", "turn.started", "turn.completed", "eval.outcome"]
    assert lines[-1]["outcome"] == "pass" and lines[-1]["changed"] == ["reorder.json"]
    assert json.loads(Path(first).read_text().splitlines()[-1])["outcome"] == "fail"


def test_the_runner_asks_for_events_and_full_mode(monkeypatch, tmp_path):
    import subprocess

    from aria_code.evals import runner

    seen = {}

    def fake_run(command, **kwargs):
        seen["command"], seen["env"] = command, kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout='{"type": "turn.completed"}\n', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    store = Trajectories(tmp_path, "core", "m")
    workspace = tmp_path / "off-by-one"
    workspace.mkdir()
    runner.build_agent_solver(model="m", trajectories=store)("fix it", workspace)
    assert seen["command"][seen["command"].index("--format") + 1] == "jsonl"
    assert seen["env"]["ARIA_EVENTS_FULL"] == "1"
    assert (tmp_path / "core" / "off-by-one-1.jsonl").exists()


def test_timeout_keeps_partial_events_and_stderr(monkeypatch, tmp_path):
    import subprocess
    from aria_code.evals import runner

    def stalled(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1, output=b'{"type":"tool.started","tool":"read_file"}\n',
                                        stderr=b"waiting for provider")
    monkeypatch.setattr(subprocess, "run", stalled)
    store = Trajectories(tmp_path, "projects", "m")
    workspace = tmp_path / "feature"
    workspace.mkdir()
    result = runner.build_agent_solver(timeout=1, trajectories=store)("implement", workspace)
    assert result.returncode == 124
    assert "waiting for provider" in result.stderr and "1s budget" in result.stderr
    assert json.loads((tmp_path / "projects/feature-1.jsonl").read_text().splitlines()[1])["type"] == "tool.started"
