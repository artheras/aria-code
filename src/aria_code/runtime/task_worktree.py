"""One git worktree per coding task, applied to the workspace on approval.

A coding turn used to edit the user's files directly; a bad edit was undone
file by file from checkpoints. With a task worktree the turn works on a copy:
the user's files do not change until they approve the result, and declining
it leaves nothing to undo.

The copy starts from what the user actually has, not from HEAD. Most people
ask for a change with uncommitted work in the tree, and a worktree of HEAD
would hand the model files the user has already moved past. ``snapshot``
records the working tree as a commit through a scratch index, so neither the
user's index nor their files are touched, and the worktree is checked out at
that commit.

A task lasts until it is applied or discarded, across as many turns as it
takes. While it has no changes it follows the workspace: each turn re-takes
the snapshot (about 0.15s for a thousand files) and rebuilds the worktree
only when the user's files moved. Applying is ``git apply`` of the task's
diff against that snapshot; when the user has since changed the same lines it
refuses and the worktree stays, so nothing is overwritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Sequence

# Ignored at the top of most repositories and needed to run anything: a
# worktree has none of them, so the task's tests would fail on imports. Linked
# into the worktree when the workspace has them and git ignores them.
DEPENDENCY_DIRS = ("node_modules", ".venv", "venv", "env", ".tox")

# Bytecode in a repository that does not ignore it would otherwise be copied
# into every snapshot. A copied .pyc whose source is then rewritten within the
# same second at the same size still validates, and Python runs the old code.
_NEVER_SNAPSHOT = (":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.py[co]")

_SNAPSHOT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Aria Code",
    "GIT_AUTHOR_EMAIL": "aria@localhost",
    "GIT_COMMITTER_NAME": "Aria Code",
    "GIT_COMMITTER_EMAIL": "aria@localhost",
}


class TaskWorktreeError(RuntimeError):
    """A task worktree could not be created, applied or removed."""


@dataclass
class TaskWorktree:
    task_id: str
    repository: str
    path: str
    base: str          # snapshot commit the worktree started from
    base_tree: str     # its tree, to tell whether the workspace has moved
    created_at: float
    linked: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["linked"] = list(self.linked)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "TaskWorktree":
        return cls(
            task_id=str(data["task_id"]), repository=str(data["repository"]),
            path=str(data["path"]), base=str(data["base"]),
            base_tree=str(data["base_tree"]), created_at=float(data.get("created_at") or 0.0),
            linked=tuple(data.get("linked") or ()),
        )


@dataclass(frozen=True)
class TaskChange:
    status: str        # A, M, D, R…, as git reports it
    path: str


def _git(*args: str, cwd: Path | str, env: Optional[dict] = None,
         input_data: Optional[bytes] = None, timeout: int = 60) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=str(cwd), input=input_data, capture_output=True,
            timeout=timeout, check=False, env={**os.environ, **(env or {})},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TaskWorktreeError(f"Unable to run git: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
        raise TaskWorktreeError(detail or f"git {' '.join(args)} failed")
    return completed.stdout


def _text(*args: str, cwd: Path | str, **kwargs) -> str:
    return _git(*args, cwd=cwd, **kwargs).decode("utf-8", errors="replace").strip()


def repository_root(workspace: Path | str) -> Optional[Path]:
    """The repository containing ``workspace``, or None outside one."""
    try:
        return Path(_text("rev-parse", "--show-toplevel", cwd=Path(workspace).expanduser())).resolve()
    except (TaskWorktreeError, OSError):
        return None


def snapshot(repository: Path | str) -> tuple[str, str]:
    """Record the working tree as a commit; returns ``(commit, tree)``.

    Tracked and untracked files alike, ignored ones excluded, exactly as
    ``git add -A`` would stage them — into a scratch index, so the user's
    index and files are left as they were.
    """
    repo = Path(repository)
    with tempfile.TemporaryDirectory(prefix="aria-snapshot-") as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index"), **_SNAPSHOT_IDENTITY}
        try:
            head = _text("rev-parse", "--verify", "-q", "HEAD", cwd=repo)
        except TaskWorktreeError:
            head = ""
        if head:
            _git("read-tree", head, cwd=repo, env=env)
        _git("add", "-A", "--", ".", *_NEVER_SNAPSHOT, cwd=repo, env=env)
        tree = _text("write-tree", cwd=repo, env=env)
        parents = ["-p", head] if head else []
        commit = _text("commit-tree", tree, *parents, "-m", "aria: task base snapshot", cwd=repo, env=env)
    return commit, tree


def checkout(repo: Path, commit: str, destination: Path) -> list[str]:
    """A detached worktree of ``commit`` at ``destination``, dependencies linked.

    Returns the dependency directories linked in from ``repo``.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git("worktree", "add", "--detach", str(destination), commit, cwd=repo, timeout=300)
    except TaskWorktreeError:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    linked = []
    for name in DEPENDENCY_DIRS:
        source = repo / name
        if not source.is_dir() or (destination / name).exists():
            continue
        try:
            _git("check-ignore", "-q", name, cwd=repo)
        except TaskWorktreeError:
            continue  # tracked or not ignored: it came with the checkout
        (destination / name).symlink_to(source, target_is_directory=True)
        linked.append(name)
    return linked


def remove_checkout(repo: Path | str, destination: Path | str, linked: Sequence[str] = ()) -> None:
    for name in linked:
        link = Path(destination) / name
        if link.is_symlink():
            link.unlink()
    try:
        _git("worktree", "remove", "--force", str(destination), cwd=repo)
    except TaskWorktreeError:
        shutil.rmtree(destination, ignore_errors=True)
        try:
            _git("worktree", "prune", cwd=repo)
        except TaskWorktreeError:
            pass


def _load_toml(text: str) -> dict:
    """tomllib on 3.11+; on 3.10, tomli or the copy pip vendors."""
    try:
        import tomllib as toml
    except ImportError:
        try:
            import tomli as toml
        except ImportError:
            from pip._vendor import tomli as toml
    return toml.loads(text)


def python_source_roots(root: Path | str) -> list[Path]:
    """Where a Python project's importable code lives, most specific first.

    A project installed with ``pip install -e`` imports from the checkout it
    was installed from. Run its tests in a task worktree and they import the
    user's files, not the task's edits, and pass or fail for the wrong code.
    Putting these roots first on PYTHONPATH makes the worktree's copy win:
    PYTHONPATH precedes site-packages and the editable install's hooks.

    Read from pytest's ``pythonpath``, setuptools' ``package-dir`` (in
    pyproject.toml or setup.cfg), a ``src/`` layout, and the root itself.
    """
    root = Path(root)
    found: list[Path] = []

    def add(relative) -> None:
        for item in ([relative] if isinstance(relative, str) else list(relative or ())):
            candidate = (root / str(item)).resolve()
            if candidate.is_dir() and candidate not in found:
                found.append(candidate)

    try:
        data = _load_toml((root / "pyproject.toml").read_text(encoding="utf-8"))
        tool = data.get("tool") or {}
        add((tool.get("pytest", {}).get("ini_options") or {}).get("pythonpath"))
        setuptools = tool.get("setuptools") or {}
        add((setuptools.get("package-dir") or {}).get(""))
        add(((setuptools.get("packages") or {}).get("find") or {}).get("where") if isinstance(
            setuptools.get("packages"), dict) else None)
    except (OSError, ValueError, ImportError, AttributeError):
        pass
    try:
        import configparser

        parser = configparser.ConfigParser()
        parser.read(root / "setup.cfg", encoding="utf-8")
        mapping = parser.get("options", "package_dir", fallback="")
        for line in mapping.splitlines():
            key, _, value = line.partition("=")
            if not key.strip() and value.strip():
                add(value.strip())
    except (configparser.Error, OSError):
        pass
    src = root / "src"
    if src.is_dir() and any(child.suffix == ".py" or (child / "__init__.py").is_file() for child in src.iterdir()):
        add("src")
    add(".")
    return found


def worktree_command_env(workspace: str, origin: str, base_env: Optional[dict] = None) -> Optional[dict]:
    """The environment for a command run in a task worktree; None elsewhere."""
    if not workspace or not origin:
        return None
    top = repository_root(workspace) or Path(workspace)
    env = dict(os.environ if base_env is None else base_env)
    roots = [str(path) for path in python_source_roots(top)]
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([*roots, *([existing] if existing else [])])
    return env


def _exclusions(task: TaskWorktree) -> list[str]:
    return [f":(exclude){name}" for name in task.linked]


class TaskWorktrees:
    """The active task worktree for each repository, persisted across restarts."""

    def __init__(self, root: Path | str, state_file: Path | str | None = None) -> None:
        self.root = Path(root).expanduser()
        self.state_file = Path(state_file).expanduser() if state_file else self.root / "tasks.json"

    # ── state ──────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.state_file)

    def active(self, workspace: Path | str) -> Optional[TaskWorktree]:
        """The open task for ``workspace``'s repository, if its worktree still exists."""
        repo = repository_root(workspace)
        if repo is None:
            return None
        record = self._load().get(str(repo))
        if not record:
            return None
        try:
            task = TaskWorktree.from_dict(record)
        except (KeyError, TypeError, ValueError):
            return None
        return task if Path(task.path).is_dir() else None

    def _remember(self, task: TaskWorktree) -> None:
        data = self._load()
        data[task.repository] = task.to_dict()
        self._save(data)

    def _forget(self, task: TaskWorktree) -> None:
        data = self._load()
        if data.pop(task.repository, None) is not None:
            self._save(data)

    # ── lifecycle ──────────────────────────────────────────────────────────

    def ensure(self, workspace: Path | str) -> Optional[TaskWorktree]:
        """The worktree this turn should work in; None outside a repository.

        Keeps a task that has changes. One without changes is kept only while
        the workspace still matches its snapshot, and rebuilt otherwise.
        """
        repo = repository_root(workspace)
        if repo is None:
            return None
        task = self.active(repo)
        if task is not None and self.changes(task):
            return task
        commit, tree = snapshot(repo)
        if task is not None:
            if task.base_tree == tree:
                return task
            self.discard(task)
        return self._create(repo, commit, tree)

    def _create(self, repo: Path, commit: str, tree: str) -> TaskWorktree:
        task_id = uuid.uuid4().hex[:8]
        digest = hashlib.sha1(str(repo).encode("utf-8")).hexdigest()[:8]
        destination = (self.root / f"{repo.name}-{digest}" / task_id).resolve()
        linked = checkout(repo, commit, destination)
        task = TaskWorktree(task_id=task_id, repository=str(repo), path=str(destination),
                            base=commit, base_tree=tree, created_at=time.time(),
                            linked=tuple(linked))
        self._remember(task)
        return task

    def _stage(self, task: TaskWorktree) -> None:
        # The worktree's own index; the user's is never involved.
        _git("add", "-A", "--", ".", *_exclusions(task), cwd=task.path)

    def changes(self, task: TaskWorktree) -> list[TaskChange]:
        """What the task changed relative to the snapshot it started from."""
        self._stage(task)
        output = _text("diff", "--cached", "--name-status", "--no-renames", task.base, cwd=task.path)
        changes = []
        for line in output.splitlines():
            status, _, path = line.partition("\t")
            if path:
                changes.append(TaskChange(status=status.strip(), path=path.strip()))
        return changes

    def diff(self, task: TaskWorktree, *, stat: bool = False) -> str:
        self._stage(task)
        args = ["diff", "--cached", "--no-renames"]
        if stat:
            args.append("--stat")
        return _text(*args, task.base, cwd=task.path)

    def patch(self, task: TaskWorktree) -> str:
        """The task's changes as a binary-safe patch against its snapshot."""
        self._stage(task)
        return _git("diff", "--cached", "--binary", "--no-renames", task.base,
                    cwd=task.path).decode("utf-8", errors="replace")

    def reset_to_patch(self, task: TaskWorktree, patch: str) -> None:
        """Make the worktree exactly its snapshot plus ``patch``.

        Covers every change since, including those a shell command made, which
        have no file checkpoint. Linked dependency directories are left alone.
        """
        _git("reset", "-q", "--hard", task.base, cwd=task.path)
        _git("clean", "-fdq", *[arg for name in task.linked for arg in ("-e", name)], cwd=task.path)
        self.restore_patch(task, patch)

    def restore_patch(self, task: TaskWorktree, patch: str) -> None:
        """Put a saved patch back into a task's worktree (a rewound task)."""
        if patch.strip():
            _git("apply", "-", cwd=task.path, input_data=patch.encode("utf-8"))

    def apply(self, task: TaskWorktree, *, session_id: str = "") -> list[TaskChange]:
        """Apply the task to the workspace, record checkpoints, then remove it.

        Refuses, keeping the worktree, when the workspace changed the same
        lines in the meantime.
        """
        changes = self.changes(task)
        if not changes:
            raise TaskWorktreeError("The task has no changes to apply.")
        repo = Path(task.repository)
        patch = _git("diff", "--cached", "--binary", "--no-renames", task.base, cwd=task.path)
        try:
            _git("apply", "--check", "-", cwd=repo, input_data=patch)
        except TaskWorktreeError as exc:
            raise TaskWorktreeError(
                f"Your files changed where the task did, so applying would overwrite them: {exc}. "
                f"The task is kept at {task.path}."
            ) from exc
        before = {}
        for change in changes:
            target = repo / change.path
            try:
                before[change.path] = (True, target.read_text(encoding="utf-8"), target.stat().st_mode & 0o777)
            except FileNotFoundError:
                before[change.path] = (False, "", None)
            except (OSError, UnicodeDecodeError):
                continue
        _git("apply", "-", cwd=repo, input_data=patch)
        _record_checkpoints(repo, changes, before, task=task, session_id=session_id)
        self.discard(task)
        return changes

    def discard(self, task: TaskWorktree) -> None:
        """Remove the worktree and forget the task. The workspace is untouched."""
        remove_checkout(task.repository, task.path, task.linked)
        self._forget(task)


def _record_checkpoints(repo: Path, changes, before: dict, *, task: TaskWorktree, session_id: str) -> None:
    """Make an applied task undoable with ``/rewind``, as an edit would be."""
    try:
        from .checkpoints import CheckpointStore
        store = CheckpointStore()
    except Exception:
        return
    for change in changes:
        target = repo / change.path
        prior = before.get(change.path)
        if prior is None or not target.is_file():
            continue
        try:
            store.record_change(
                path=target, before_content=prior[1],
                after_content=target.read_text(encoding="utf-8"),
                existed_before=prior[0], before_mode=prior[2], source="task_apply",
                session_id=session_id, label=f"task {task.task_id}: {change.path}",
                metadata={"task_id": task.task_id, "worktree": task.path},
            )
        except Exception:
            continue


def default_task_worktrees() -> TaskWorktrees:
    from ..packages.aria_core.paths import aria_home

    return TaskWorktrees(aria_home() / "worktrees" / "tasks")
