"""A coding task works on a copy of the user's tree and lands only when applied."""

import subprocess
from pathlib import Path

import pytest

from aria_code.runtime.task_worktree import TaskWorktreeError, TaskWorktrees, repository_root, snapshot


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo,
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / ".gitignore").write_text("node_modules/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def tasks(tmp_path):
    return TaskWorktrees(tmp_path / "worktrees")


def test_the_worktree_starts_from_uncommitted_work_without_touching_the_index(repo, tasks):
    (repo / "app.py").write_text("def add(a, b):\n    return a - b  # wip\n")
    (repo / "notes.txt").write_text("untracked\n")
    git(repo, "add", "app.py")
    status_before = git(repo, "status", "--porcelain")
    index_before = (repo / ".git" / "index").read_bytes()

    task = tasks.ensure(repo)

    copy = Path(task.path)
    assert (copy / "app.py").read_text().endswith("# wip\n")
    assert (copy / "notes.txt").read_text() == "untracked\n"
    assert git(repo, "status", "--porcelain") == status_before
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert tasks.changes(task) == []


def test_edits_stay_in_the_worktree_until_applied(repo, tasks):
    task = tasks.ensure(repo)
    (Path(task.path) / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (Path(task.path) / "test_app.py").write_text("from app import add\n")

    assert "a - b" in (repo / "app.py").read_text()
    assert {(c.status, c.path) for c in tasks.changes(task)} == {("M", "app.py"), ("A", "test_app.py")}

    applied = tasks.apply(task)

    assert {c.path for c in applied} == {"app.py", "test_app.py"}
    assert "a + b" in (repo / "app.py").read_text()
    assert (repo / "test_app.py").exists()
    assert not Path(task.path).exists()
    assert tasks.active(repo) is None


def test_a_task_is_reused_across_turns_and_rebuilt_when_the_workspace_moves(repo, tasks):
    first = tasks.ensure(repo)
    assert tasks.ensure(repo).task_id == first.task_id

    (repo / "app.py").write_text("changed by the user\n")
    second = tasks.ensure(repo)
    assert second.task_id != first.task_id
    assert not Path(first.path).exists()
    assert (Path(second.path) / "app.py").read_text() == "changed by the user\n"


def test_a_task_with_changes_is_kept_even_when_the_workspace_moves(repo, tasks):
    task = tasks.ensure(repo)
    (Path(task.path) / "app.py").write_text("task edit\n")
    (repo / "other.txt").write_text("user edit\n")

    assert tasks.ensure(repo).task_id == task.task_id
    assert TaskWorktrees(tasks.root).active(repo).task_id == task.task_id  # survives a restart


def test_applying_over_the_users_own_edit_to_the_same_lines_is_refused(repo, tasks):
    task = tasks.ensure(repo)
    (Path(task.path) / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "app.py").write_text("def add(a, b):\n    return b - a\n")

    with pytest.raises(TaskWorktreeError, match="overwrite"):
        tasks.apply(task)
    assert "b - a" in (repo / "app.py").read_text()
    assert Path(task.path).exists()


def test_ignored_dependency_directories_are_linked_but_never_a_change(repo, tasks):
    (repo / "node_modules" / "left-pad").mkdir(parents=True)
    task = tasks.ensure(repo)

    link = Path(task.path) / "node_modules"
    assert link.is_symlink() and (link / "left-pad").is_dir()
    assert tasks.changes(task) == []
    tasks.discard(task)
    assert (repo / "node_modules" / "left-pad").is_dir()


def test_discard_leaves_the_workspace_as_it_was(repo, tasks):
    task = tasks.ensure(repo)
    (Path(task.path) / "app.py").write_text("thrown away\n")
    tasks.discard(task)
    assert "a - b" in (repo / "app.py").read_text()
    assert not Path(task.path).exists()


def test_outside_a_repository_there_is_no_task(tmp_path, tasks):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert repository_root(plain) is None
    assert tasks.ensure(plain) is None


def test_a_repository_without_commits_can_still_be_snapshotted(tmp_path):
    repo = tmp_path / "fresh"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n")
    commit, tree = snapshot(repo)
    assert commit and tree


# ── the executor sends the repository's paths into the worktree ─────────────

def _executor(seen, origin, worktree):
    from aria_code.runtime import ToolExecutor

    def record(name):
        return (lambda params: seen.setdefault(name, params) and {"success": True}, "")

    tools = {name: record(name) for name in ("write_file", "read_file", "run_command")}
    return ToolExecutor(tools, config={"permission_mode": "full-access"},
                        execution_context=lambda: {"_workspace": str(worktree),
                                                   "_workspace_origin": str(origin)})


def test_paths_into_the_repository_are_taken_to_mean_the_worktree(tmp_path):
    origin, worktree = tmp_path / "project", tmp_path / "wt"
    origin.mkdir(), worktree.mkdir()
    seen = {}
    executor = _executor(seen, origin, worktree)

    executor.execute_local("write_file", {"path": str(origin / "src" / "app.py"), "content": "x"})
    executor.execute_local("read_file", {"path": "README.md"})
    executor.execute_local("run_command", {"command": f"cd {origin} && pytest {origin}/tests -q",
                                           "cwd": str(origin / "src")})

    assert seen["write_file"]["path"] == str(worktree.resolve() / "src" / "app.py")
    assert seen["read_file"]["path"] == str(worktree.resolve() / "README.md")
    assert seen["run_command"]["cwd"] == str(worktree.resolve() / "src")
    assert seen["run_command"]["command"] == f"cd {worktree.resolve()} && pytest {worktree.resolve()}/tests -q"


def test_paths_outside_the_repository_are_left_alone(tmp_path):
    origin, worktree = tmp_path / "project", tmp_path / "wt"
    origin.mkdir(), worktree.mkdir()
    sibling = tmp_path / "project-docs" / "spec.md"
    seen = {}
    executor = _executor(seen, origin, worktree)

    executor.execute_local("read_file", {"path": str(sibling)})
    executor.execute_local("run_command", {"command": f"cat {sibling}"})

    assert seen["read_file"]["path"] == str(sibling.resolve())
    assert seen["run_command"]["command"] == f"cat {sibling}"


# ── the REPL asks before a task reaches the user's files ────────────────────

@pytest.fixture
def isolation(tasks, monkeypatch):
    from aria_code.apps.cli import task_isolation
    monkeypatch.delenv("ARIA_TASK_ISOLATION", raising=False)
    task_isolation.reset_for_tests(tasks)
    yield task_isolation
    task_isolation.reset_for_tests(None)


def _turn_edits(isolation, repo, content="def add(a, b):\n    return a + b\n"):
    config = {"_session_workspace_root": str(repo)}
    task = isolation.prepare(config, notify=lambda _text: None)
    context = isolation.execution_context(task, config)
    Path(context["_workspace"], "app.py").write_text(content)
    return task


def test_a_turn_runs_in_the_worktree_and_applying_lands_it(isolation, repo):
    task = _turn_edits(isolation, repo)
    said = []
    outcome = isolation.offer(task, choose=lambda options, title: isolation.APPLY, say=said.append)
    assert outcome == isolation.APPLY
    assert "a + b" in (repo / "app.py").read_text()
    assert any("Applied task" in line for line in said)


def test_cancelling_the_question_keeps_the_task_and_the_files(isolation, repo):
    task = _turn_edits(isolation, repo)
    outcome = isolation.offer(task, choose=lambda options, title: -1, say=lambda _text: None)
    assert outcome == isolation.KEEP
    assert "a - b" in (repo / "app.py").read_text()
    assert "app.py" in isolation.command("", {"_session_workspace_root": str(repo)})
    assert "Discarded" in isolation.command("discard", {"_session_workspace_root": str(repo)})
    assert "a - b" in (repo / "app.py").read_text()


def test_a_turn_that_changed_nothing_asks_nothing(isolation, repo):
    task = isolation.prepare({"_session_workspace_root": str(repo)}, notify=lambda _text: None)
    asked = []
    assert isolation.offer(task, choose=lambda *a: asked.append(a) or 0, say=lambda _t: None) is None
    assert asked == []


def test_read_only_modes_and_task_isolation_off_edit_in_place(isolation, repo):
    for config in ({"permission_mode": "plan"}, {"permission_mode": "read-only"}, {"task_isolation": "off"}):
        assert isolation.prepare({**config, "_session_workspace_root": str(repo)}) is None


def test_a_session_started_in_a_subdirectory_works_in_the_same_subdirectory(isolation, repo):
    (repo / "pkg").mkdir()
    config = {"_session_workspace_root": str(repo / "pkg")}
    task = isolation.prepare(config, notify=lambda _text: None)
    context = isolation.execution_context(task, config)
    assert context["_workspace"] == str(Path(task.path) / "pkg")
    assert context["_workspace_origin"] == str(repo.resolve())


def test_the_environment_can_turn_isolation_off(isolation, repo, monkeypatch):
    monkeypatch.setenv("ARIA_TASK_ISOLATION", "off")
    assert isolation.prepare({"_session_workspace_root": str(repo)}) is None


def test_bytecode_never_enters_a_snapshot(repo, tasks):
    # A copied .pyc whose source is rewritten in the same second at the same
    # size still validates: the baseline once ran the changed code this way.
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"stale")
    (repo / "legacy.pyc").write_bytes(b"stale")
    task = tasks.ensure(repo)
    assert not (Path(task.path) / "__pycache__").exists()
    assert not (Path(task.path) / "legacy.pyc").exists()


# ── commands in a worktree import the worktree's code ───────────────────────

def test_source_roots_come_from_the_project_layout(tmp_path):
    from aria_code.runtime.task_worktree import python_source_roots

    root = tmp_path / "p"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("")
    (root / "lib").mkdir()
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["lib"]\n')
    assert python_source_roots(root) == [(root / "lib").resolve(), (root / "src").resolve(), root.resolve()]

    other = tmp_path / "q"
    (other / "code").mkdir(parents=True)
    (other / "setup.cfg").write_text("[options]\npackage_dir =\n    =code\n")
    assert python_source_roots(other)[0] == (other / "code").resolve()


def test_an_editable_install_does_not_shadow_the_worktree(tmp_path, tasks, monkeypatch):
    import sys as _sys
    import aria_code.aria_cli as cli
    from aria_code.apps.cli.providers.runtime_bridge import build_tool_executor
    from aria_code.runtime.approval import ApprovalDecision

    repo = tmp_path / "proj"
    (repo / "src" / "mypkg").mkdir(parents=True)
    (repo / "src" / "mypkg" / "__init__.py").write_text("VALUE = 'user'\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    # What `pip install -e .` amounts to: the user's src answers `import mypkg`.
    monkeypatch.setenv("PYTHONPATH", str(repo / "src"))

    task = tasks.ensure(repo)
    (Path(task.path) / "src" / "mypkg" / "__init__.py").write_text("VALUE = 'task'\n")
    executor = build_tool_executor(cli.LOCAL_TOOLS, {"permission_mode": "workspace-write"},
                                   lambda: {"_workspace": task.path, "_workspace_origin": str(repo)})
    result = executor.execute_local(
        "run_command", {"command": f'{_sys.executable} -c "import mypkg; print(mypkg.VALUE)"'},
        approval=ApprovalDecision.allow(policy="balanced", user_approved=True))
    assert result["success"], result
    assert result["data"]["stdout"].strip() == "task"
