"""/diff /commit /pr /checkpoint /worktree /graph /agents /test /build /fix."""
import asyncio
import io
import json
import sys
from types import SimpleNamespace

import pytest

from aria_code.apps.cli.commands.engineering_cmds import EngineeringCommandsMixin, detect_commands, fix_prompt

NEW = ["/sim", "/diff", "/commit", "/pr", "/checkpoint", "/worktree", "/graph", "/agents", "/test", "/build", "/fix"]


def test_every_new_command_is_registered_and_described():
    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli

    table = cli.ArtheraTerminal(dict(cli.DEFAULT_CONFIG)).commands.commands
    for name in NEW:
        assert name in table, name
        assert table[name][1].strip(), f"{name} has no description"
    # /history already existed (the conversation); this must not replace it.
    assert "conversation" in table["/history"][1].lower()


def test_the_new_commands_are_findable_from_the_popup_catalog():
    from aria_code.apps.cli.commands.catalog import COMMAND_DESCRIPTIONS_ZH, VISIBLE_SLASH_COMMANDS

    for name in NEW:
        assert name in VISIBLE_SLASH_COMMANDS and name in COMMAND_DESCRIPTIONS_ZH


# ── detect_commands ──────────────────────────────────────────────────────────

def test_a_node_project_uses_its_scripts_and_package_manager(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest", "build": "vite build", "lint": "eslint ."}}))
    assert detect_commands(tmp_path) == {"test": "npm test", "build": "npm run build", "lint": "npm run lint"}
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert detect_commands(tmp_path)["test"] == "pnpm test"


def test_a_script_the_project_does_not_declare_is_not_invented(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"start": "node ."}}))
    assert detect_commands(tmp_path) == {}


def test_rust_go_and_python_projects(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    assert detect_commands(tmp_path)["test"] == "cargo test"
    (tmp_path / "Cargo.toml").unlink()
    (tmp_path / "go.mod").write_text("module x\n")
    assert detect_commands(tmp_path)["build"] == "go build ./..."
    (tmp_path / "go.mod").unlink()
    (tmp_path / "pyproject.toml").write_text("[build-system]\nrequires=[]\n[tool.pytest.ini_options]\n[tool.ruff]\n")
    assert detect_commands(tmp_path) == {"test": "python3 -m pytest -q", "build": "python3 -m build", "lint": "ruff check ."}


def test_make_targets_fill_what_nothing_else_declared(tmp_path):
    (tmp_path / "Makefile").write_text("build:\n\tcc x.c\n\ntest:\n\t./run\n")
    assert detect_commands(tmp_path) == {"test": "make test", "build": "make build"}


def test_the_fix_prompt_names_the_problem_and_the_checks():
    prompt = fix_prompt("expired sessions are not refreshed", {"test": "npm test", "lint": "npm run lint"})
    assert "expired sessions are not refreshed" in prompt and "`npm test`" in prompt and "`npm run lint`" in prompt
    assert "smallest change" in prompt
    assert "project's checks" in fix_prompt("x", {})


# ── the commands themselves ──────────────────────────────────────────────────

class _Fake(EngineeringCommandsMixin):
    def __init__(self, root):
        self.calls = []
        self.said = io.StringIO()
        self.context = SimpleNamespace(has_rich=False, console=None)
        self.terminal = SimpleNamespace(config={"_session_workspace_root": str(root)}, send_message=self._send)

    async def _send(self, text):
        self.calls.append(("send", text))

    def cmd_git(self, args): self.calls.append(("git", args))
    def cmd_task(self, args): self.calls.append(("task", args))
    def cmd_impact(self, args): self.calls.append(("impact", args))
    def cmd_rewind(self, args): self.calls.append(("rewind", args))
    def cmd_tasks(self, args): self.calls.append(("tasks", args))


def run(coro):
    return asyncio.run(coro)


def test_diff_commit_worktree_graph_and_checkpoint_delegate(tmp_path):
    f = _Fake(tmp_path)
    run(f.cmd_diff("")); run(f.cmd_diff("--patch")); run(f.cmd_diff("src/a.py"))
    run(f.cmd_commit("fix: refresh")); run(f.cmd_commit(""))
    run(f.cmd_worktree("apply")); run(f.cmd_graph("src/a.py Session.refresh"))
    run(f.cmd_checkpoint("")); run(f.cmd_checkpoint("restore 2")); run(f.cmd_checkpoint("restore"))
    assert f.calls == [
        ("git", "status"), ("git", "diff"), ("git", "patch"), ("git", "patch src/a.py"),
        ("git", "commit fix: refresh"), ("git", "commit"),
        ("task", "apply"), ("impact", "src/a.py Session.refresh"),
        ("rewind", "list"), ("rewind", "turns"), ("rewind", "turn 2"), ("rewind", "turn"),
    ]


def test_agents_lists_project_agents_and_background_tasks(tmp_path, capsys):
    (tmp_path / ".aria" / "agents").mkdir(parents=True)
    (tmp_path / ".aria" / "agents" / "security.md").write_text("x")
    (tmp_path / ".aria" / "agents" / "frontend.md").write_text("x")
    f = _Fake(tmp_path)
    run(f.cmd_agents(""))
    assert "frontend, security" in capsys.readouterr().out and f.calls == [("tasks", "")]


def test_fix_needs_a_description_and_sends_one_prompt(tmp_path, capsys):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    f = _Fake(tmp_path)
    run(f.cmd_fix("  "))
    assert f.calls == [] and "Usage: /fix" in capsys.readouterr().out
    run(f.cmd_fix("login loops forever"))
    (kind, prompt), = f.calls
    assert kind == "send" and "login loops forever" in prompt and "`npm test`" in prompt


def test_test_runs_the_projects_command_and_reports(tmp_path, capsys, monkeypatch):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    f = _Fake(tmp_path)
    seen = []
    monkeypatch.setattr(f, "_eng_run", lambda command, timeout=600: seen.append(command) or {"success": True, "data": {"stdout": "5 passed"}})
    run(f.cmd_test("src/a.test.js"))
    out = capsys.readouterr().out
    assert seen == ["npm test src/a.test.js"] and "5 passed" in out and "✓ test passed" in out
    monkeypatch.setattr(f, "_eng_run", lambda command, timeout=600: {"success": False, "error": "exit 1", "data": {"stderr": "boom"}})
    run(f.cmd_test(""))
    out = capsys.readouterr().out
    assert "✗ test failed" in out and "boom" in out


def test_test_and_build_say_so_when_the_project_declares_nothing(tmp_path, capsys):
    f = _Fake(tmp_path)
    run(f.cmd_test("")); run(f.cmd_build(""))
    out = capsys.readouterr().out
    assert out.count("No test command found") == 1 and out.count("No build command found") == 1


def test_pr_needs_gh_and_a_known_subcommand(tmp_path, capsys, monkeypatch):
    f = _Fake(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: None)
    run(f.cmd_pr(""))
    assert "GitHub CLI" in capsys.readouterr().out
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh")
    ran = []
    monkeypatch.setattr(f, "_eng_run", lambda command, timeout=600: ran.append(command) or {"success": True, "data": {}})
    run(f.cmd_pr("create Fix refresh")); run(f.cmd_pr("")); run(f.cmd_pr("nonsense"))
    assert ran == ["gh pr create --fill --title 'Fix refresh'", "gh pr status"]
    assert "Usage: /pr" in capsys.readouterr().out


def test_a_project_workflow_with_the_same_name_is_pointed_out_not_silently_shadowed(tmp_path, capsys):
    f = _Fake(tmp_path)
    f._workflow_named = lambda cmd: cmd == "/test"
    run(f.cmd_test(""))
    assert "workflow named 'test'" in capsys.readouterr().out
    run(f.cmd_build(""))
    assert "workflow named" not in capsys.readouterr().out


# ── robotics: ROS 2, declared commands, /sim ─────────────────────────────────

def test_a_colcon_workspace_builds_with_colcon_and_reads_its_results(tmp_path):
    (tmp_path / "src" / "my_robot").mkdir(parents=True)
    (tmp_path / "src" / "my_robot" / "package.xml").write_text("<package/>")
    found = detect_commands(tmp_path)
    assert found["build"] == "colcon build" and found["test"] == "colcon test"
    assert found["test_report"] == "colcon test-result --verbose"
    assert "sim" not in found, "a simulation is never guessed"


def test_a_single_ros2_package_is_recognised(tmp_path):
    (tmp_path / "package.xml").write_text("<package/>")
    assert detect_commands(tmp_path)["test"] == "colcon test"


def test_project_yaml_declares_commands_that_win_over_detection(tmp_path):
    (tmp_path / "src" / "p").mkdir(parents=True)
    (tmp_path / "src" / "p" / "package.xml").write_text("<package/>")
    (tmp_path / ".aria").mkdir()
    (tmp_path / ".aria" / "project.yaml").write_text(
        "commands:\n  test: ./scripts/test.sh\n  sim: ros2 launch my_robot sim.launch.py\n  nonsense: x\n  lint: 5\n")
    found = detect_commands(tmp_path)
    assert found["test"] == "./scripts/test.sh" and found["sim"] == "ros2 launch my_robot sim.launch.py"
    assert "test_report" not in found, "the project's own test command reports for itself"
    assert found["build"] == "colcon build" and "lint" not in found and "nonsense" not in found


def test_a_broken_project_yaml_is_ignored(tmp_path):
    (tmp_path / ".aria").mkdir()
    (tmp_path / ".aria" / "project.yaml").write_text("commands: [not, a, mapping")
    assert detect_commands(tmp_path) == {}


def test_colcon_test_is_followed_by_its_results(tmp_path, capsys, monkeypatch):
    (tmp_path / "package.xml").write_text("<package/>")
    f = _Fake(tmp_path)
    seen = []
    monkeypatch.setattr(f, "_eng_run", lambda command, timeout=600: seen.append(command) or {"success": True, "data": {"stdout": "ok"}})
    run(f.cmd_test(""))
    assert seen == ["colcon test", "colcon test-result --verbose"]
    assert "test results passed" in capsys.readouterr().out


def test_sim_runs_only_what_the_project_declares(tmp_path, capsys, monkeypatch):
    f = _Fake(tmp_path)
    run(f.cmd_sim(""))
    assert ".aria/project.yaml" in capsys.readouterr().out
    (tmp_path / ".aria").mkdir()
    (tmp_path / ".aria" / "project.yaml").write_text("commands:\n  sim: ros2 launch my_robot sim.launch.py\n")
    seen = []
    monkeypatch.setattr(f, "_eng_run", lambda command, timeout=600: seen.append(command) or {"success": True, "data": {}})
    run(f.cmd_sim("scenario:=docking"))
    assert seen == ["ros2 launch my_robot sim.launch.py scenario:=docking"]
