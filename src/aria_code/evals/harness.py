"""Run verifiable tasks and score them by exit code.

The shape of one task
---------------------
A fixture directory, a prompt, and a command::

    - id: fix-failing-test
      prompt: "tests/test_math.py 有一个测试失败了，修好它"
      fixture: fix_failing_test
      verify: "{python} -m pytest -q"
      requires: [pytest]

The fixture is copied to a scratch directory, the agent is turned loose in the
copy, and ``verify`` decides the outcome.  Exit 0 is a pass.  Nothing else
counts — not a confident summary, not a diff that looks right.

The property that makes a suite trustworthy
-------------------------------------------
**Every task must start red.**  Before the agent runs, the harness runs
``verify`` on the untouched fixture.  If it already passes, the task is
reported ``INVALID`` and scored as neither pass nor fail.

This check is worth more than any individual task.  A suite that silently
accumulates already-green tasks reports a rising pass rate while measuring
less and less, and the failure is invisible precisely because the number looks
good — you cannot tell a task the agent solved from a task that was never
broken.  Running the check every time costs one command per task and makes the
number mean something.

"Red" has to mean the check ran
------------------------------
The pre-flight alone is not enough, and the first run of this harness proved
it: every task reported red, and every task was red because the ``python3`` on
PATH had no pytest.  The suite looked healthy while measuring nothing at all —
the same silent-inflation failure the pre-flight exists to prevent, arriving
through the back door.

Two things close it.  ``{python}`` in a command resolves to the interpreter
running the harness, so a suite verifies against the environment it was
launched with rather than whatever ``python3`` happens to mean on this
machine.  And a task may declare ``requires: [pytest]``; a missing import is
reported as ``ERROR`` — excluded from the score — instead of being counted as
a red test the agent is expected to fix.

Isolation
---------
Tasks run in a copy under a scratch root, never in the fixture and never in
the repository.  An agent that deletes the workspace, writes outside it, or
leaves it in a broken state affects nothing but its own run's directory, which
is what makes it safe to keep destructive tasks in the suite — those are the
ones worth having.
"""

from __future__ import annotations

import importlib.util
import json
import re
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Sequence

__all__ = [
    "SuiteResult",
    "TaskResult",
    "TaskSpec",
    "Solver",
    "load_suite",
    "run_suite",
    "run_task",
]

# Outcomes.  Kept as four values rather than a boolean because "the agent
# failed" and "the task was broken" call for opposite responses, and collapsing
# them is how a rotten suite goes unnoticed.
PASS = "pass"
FAIL = "fail"
INVALID = "invalid"   # the fixture was already green: the task measures nothing
ERROR = "error"       # the harness or the solver blew up; not the agent's score

_MAX_LOG_CHARS = 4000

# Residue that must not travel from a fixture into a task workspace.
# Directory names that hold build/run residue rather than source.
_RUN_RESIDUE = frozenset({
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", "node_modules", ".DS_Store",
})

_IGNORED_FIXTURE_ENTRIES = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".git", "node_modules", ".DS_Store",
)


def _trim(text: str, limit: int = _MAX_LOG_CHARS) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    half = limit // 2
    return f"{body[:half]}\n… [truncated] …\n{body[-half:]}"


@dataclass(frozen=True)
class TaskSpec:
    """One verifiable task."""

    id: str
    prompt: str
    verify: str
    fixture: str = ""
    timeout: int = 300
    solve_timeout: int = 900
    tags: tuple[str, ...] = ()
    setup: tuple[str, ...] = ()
    # Importable modules the check needs. Missing ones make the task ERROR
    # rather than FAIL: an absent pytest is not a bug for the agent to fix.
    requires: tuple[str, ...] = ()
    # Files the agent must not modify. Defaults to the tests, because the
    # check IS the tests: an agent that edits them can turn any task green
    # without doing the work, and the score would be indistinguishable from a
    # real solve. Editing one is scored FAIL no matter what the check says.
    protect: tuple[str, ...] = ("test_*.py", "*_test.py", "tests/**")
    # A task may legitimately start green when it is a regression guard: the
    # point is that the agent must not *break* it. Opting out is explicit so
    # that it is a decision someone made, not a fixture that quietly rotted.
    allow_green_start: bool = False
    # Files the agent never sees: present for the pre-flight, removed (with
    # the pre-flight's caches) for the agent's turn, put back from the fixture
    # for the score. A grader that holds the expected answers — EXPECTED =
    # {"F-201": 5000.00, ...} — or spells out the trap in its docstrings is
    # an answer key when it sits in the workspace. gemini-3.5-flash passed 23
    # of the first 24 tasks with every grader in view.
    hidden: tuple[str, ...] = ()
    # How the agent should go about it, judged on its tool calls and reported
    # beside the outcome without changing it. See evals/behavior.py.
    behavior: tuple[dict, ...] = ()

    @classmethod
    def from_dict(cls, data: dict) -> "TaskSpec":
        missing = [key for key in ("id", "prompt", "verify") if not data.get(key)]
        if missing:
            raise ValueError(f"task is missing required field(s): {', '.join(missing)}")
        return cls(
            id=str(data["id"]),
            prompt=str(data["prompt"]),
            verify=str(data["verify"]),
            fixture=str(data.get("fixture") or ""),
            timeout=int(data.get("timeout") or 300),
            solve_timeout=int(data.get("solve_timeout") or 900),
            tags=tuple(str(t) for t in (data.get("tags") or ())),
            setup=tuple(str(c) for c in (data.get("setup") or ())),
            requires=tuple(str(m) for m in (data.get("requires") or ())),
            protect=(
                tuple(str(g) for g in data["protect"])
                if "protect" in data else cls.protect
            ),
            allow_green_start=bool(data.get("allow_green_start", False)),
            hidden=tuple(str(g) for g in (data.get("hidden") or ())),
            behavior=tuple(_validate_behavior(c) for c in (data.get("behavior") or ())),
        )


def _validate_behavior(raw) -> dict:
    from .behavior import validate

    return validate(raw)


_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret)(\s*[=:]\s*)\S+"
    r"|\b(?:bearer)\s+\S+"
    r"|\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}|\bgh[pousr]_[A-Za-z0-9]{12,}|\bAIza[0-9A-Za-z_-]{20,}"
    r"|\bya29\.[0-9A-Za-z_-]+"
)


def _mask(line: str) -> str:
    def repl(match: re.Match) -> str:
        return f"{match.group(1)}{match.group(2)}***" if match.group(1) else "***"
    return _SECRET.sub(repl, line)


def _log_tail(log: str, lines: int = 23) -> list:
    """The last lines of a task's log, secret-shaped values masked."""
    kept = [line.rstrip() for line in str(log or "").splitlines() if line.strip()]
    return [_mask(line)[:300] for line in kept[-lines:]]


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    outcome: str
    seconds: float = 0.0
    exit_code: Optional[int] = None
    detail: str = ""
    log: str = ""
    tags: tuple[str, ...] = ()
    # What the agent actually touched. A red check with an empty list is a
    # different failure from a red check after real edits: the first says the
    # agent never engaged, the second says it engaged and got it wrong. Without
    # this the two are indistinguishable in a report, and they call for
    # opposite investigations.
    changed: tuple[str, ...] = ()
    # Behaviour checks on the agent's tool calls: ({check, passed, detail}, …).
    behavior: tuple = ()

    @property
    def counted(self) -> bool:
        """Whether this result belongs in the pass rate at all."""
        return self.outcome in (PASS, FAIL)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "outcome": self.outcome,
            "seconds": round(self.seconds, 2),
            "exit_code": self.exit_code,
            "detail": self.detail,
            "tags": list(self.tags),
            "changed": list(self.changed),
            # Why it did not pass. The first CI run's report said only "the
            # agent did not complete (exit 1)"; the reason was in the job log.
            "log_tail": _log_tail(self.log) if self.outcome != PASS else [],
            **({"behavior": [dict(b) for b in self.behavior]} if self.behavior else {}),
        }


@dataclass
class SuiteResult:
    name: str
    results: List[TaskResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.outcome == PASS)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.outcome == FAIL)

    @property
    def invalid(self) -> int:
        return sum(1 for r in self.results if r.outcome == INVALID)

    @property
    def errored(self) -> int:
        return sum(1 for r in self.results if r.outcome == ERROR)

    @property
    def scored(self) -> int:
        return sum(1 for r in self.results if r.counted)

    @property
    def pass_rate(self) -> float:
        """Passes over *scored* tasks.

        Invalid and errored tasks are excluded rather than counted as
        failures. Counting them down would let one broken fixture look like an
        agent regression; counting them up would let it hide one.
        """
        return (self.passed / self.scored) if self.scored else 0.0

    def by_tag(self) -> dict:
        """Pass rate per tag — the per-industry scoreboard."""
        buckets: dict[str, list[TaskResult]] = {}
        for result in self.results:
            for tag in result.tags:
                buckets.setdefault(tag, []).append(result)
        out = {}
        for tag, results in sorted(buckets.items()):
            scored = [r for r in results if r.counted]
            out[tag] = {
                "passed": sum(1 for r in scored if r.outcome == PASS),
                "scored": len(scored),
                "pass_rate": round(
                    sum(1 for r in scored if r.outcome == PASS) / len(scored), 4
                ) if scored else 0.0,
            }
        return out

    def to_dict(self) -> dict:
        return {
            "suite": self.name,
            "pass_rate": round(self.pass_rate, 4),
            "passed": self.passed,
            "failed": self.failed,
            "invalid": self.invalid,
            "errored": self.errored,
            "scored": self.scored,
            "by_tag": self.by_tag(),
            "per_task": self.per_task(),
            **({"behavior": self.behavior()} if any(r.behavior for r in self.results) else {}),
            "results": [r.to_dict() for r in self.results],
        }

    def behavior(self) -> dict:
        """Passes over runs for each behaviour check, keyed ``task: check``."""
        out: dict = {}
        for result in self.results:
            for item in result.behavior:
                stats = out.setdefault(f"{result.task_id}: {item['check']}", {"passed": 0, "runs": 0})
                stats["runs"] += 1
                stats["passed"] += bool(item["passed"])
        return out

    def merge(self, other: "SuiteResult") -> None:
        """Fold another run of the same suite into this one."""
        self.results.extend(other.results)

    def per_task(self) -> dict:
        """Passes over attempts for each task id, across every repeat.

        A single sample is not a score. Observed on the first real run: one
        task passed alone and failed in the same suite minutes later, from the
        same model. Reporting 2/5 without saying how many attempts produced it
        invites reading noise as a regression.
        """
        buckets: dict[str, list[TaskResult]] = {}
        for result in self.results:
            buckets.setdefault(result.task_id, []).append(result)
        out = {}
        for task_id, results in buckets.items():
            scored = [r for r in results if r.counted]
            out[task_id] = {
                "passed": sum(1 for r in scored if r.outcome == PASS),
                "attempts": len(scored),
                "outcomes": [r.outcome for r in results],
            }
        return out

    def summary_line(self) -> str:
        parts = [f"{self.name}: {self.passed}/{self.scored} ({self.pass_rate:.0%})"]
        if self.invalid:
            parts.append(f"{self.invalid} invalid")
        if self.errored:
            parts.append(f"{self.errored} error")
        checks = [item for r in self.results for item in r.behavior]
        if checks:
            parts.append(f"behaviour {sum(bool(i['passed']) for i in checks)}/{len(checks)}")
        return " · ".join(parts)


# ``(prompt, workspace) -> anything``.  Injected so the harness can be tested,
# and so the same suite can score a different agent — the CLI, a subagent
# backend, or a competitor — without the suite knowing which.
Solver = Callable[[str, Path], Any]


def _resolve(command: str) -> str:
    """Substitute ``{python}`` with the interpreter running the harness.

    Without this a suite silently verifies against whatever ``python3`` means
    on PATH, which is how five tasks once reported red because that
    interpreter had no pytest installed.

    The path is quoted, because the command runs through a shell. Unquoted, an
    interpreter under "Application Support" — where the macOS app keeps its
    projects — split at the space, every check exited 126, and the pre-flight
    reported all four tasks red: measuring nothing while saying it was fine.
    """
    if os.name == "nt":
        interpreter = subprocess.list2cmdline([sys.executable])
    else:
        interpreter = shlex.quote(sys.executable)
    return (command or "").replace("{python}", interpreter)


# Exit codes with which the shell says it never ran the check at all: 125 is
# _run's own "could not start", 126 not executable, 127 not found, 9009 the
# Windows not-found. A red result for any of these is not a broken fixture and
# not a failed agent — counting it as either lets an environment problem pose
# as a measurement.
_COULD_NOT_RUN = {125: "could not start", 126: "not executable", 127: "command not found",
                  9009: "command not found"}


def _could_not_run(code: int, log: str) -> str:
    reason = _COULD_NOT_RUN.get(code)
    if reason is None:
        return ""
    last = (log or "").strip().splitlines()[-1:] or [""]
    return f"the check itself could not run (exit {code}, {reason}): {last[0]}".rstrip(": ")


def _missing_modules(names: Iterable[str]) -> list[str]:
    missing = []
    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    return missing


def _matches(rel: str, globs: Iterable[str]) -> bool:
    import fnmatch

    name = rel.replace("\\", "/")
    base = name.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(base, g) for g in globs)


def _violates_protection(changed: Iterable[str], patterns: Iterable[str]) -> tuple[str, ...]:
    """Protected files the agent modified."""
    globs = tuple(patterns)
    return tuple(rel for rel in changed if _matches(rel, globs))


def _hide(workspace: Path, globs: Sequence[str]) -> tuple[str, ...]:
    """Remove the hidden files, and every cache the pre-flight left, from *workspace*.

    The caches matter as much as the files: .pytest_cache records the failing
    tests by name and __pycache__ holds the grader compiled, docstrings and
    expected values included.
    """
    if not globs:
        return ()
    hidden = tuple(
        str(path.relative_to(workspace)) for path in sorted(workspace.rglob("*"))
        if path.is_file() and _matches(str(path.relative_to(workspace)), globs)
    )
    for rel in hidden:
        (workspace / rel).unlink()
    for cache in sorted(workspace.rglob("*"), reverse=True):
        if cache.is_dir() and cache.name in ("__pycache__", ".pytest_cache"):
            shutil.rmtree(cache, ignore_errors=True)
    return hidden


def _restore(hidden: Sequence[str], source: Path, workspace: Path) -> None:
    """Put the hidden files back from the fixture, over anything the agent wrote there."""
    for rel in hidden:
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)


def _snapshot(root: Path) -> dict[str, str]:
    """Content digests for every file in *root*, keyed by relative path."""
    import hashlib

    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        # Residue the *check* writes, not the agent's work. Reporting it makes
        # every task look like it touched a dozen files and buries the one edit
        # that matters.
        if any(part in _RUN_RESIDUE for part in rel.replace("\\", "/").split("/")):
            continue
        try:
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


def _changed_paths(before: dict[str, str], after: dict[str, str]) -> tuple[str, ...]:
    """Files the agent created, edited, or deleted."""
    touched = [
        rel for rel in sorted(set(before) | set(after))
        if before.get(rel) != after.get(rel)
    ]
    return tuple(touched)


def _solver_failure(outcome: Any) -> Optional[tuple[int, str]]:
    """``(exit_code, log)`` when the solver reports it did not complete.

    Duck-typed on ``returncode`` so a plain callable that returns nothing —
    every in-process solver, and the check-only one — is unaffected, while a
    ``subprocess.CompletedProcess`` from the real agent is inspected.
    """
    if outcome is None:
        return None
    code = getattr(outcome, "returncode", None)
    if not isinstance(code, int) or code == 0:
        return None
    log = "\n".join(
        part for part in (
            str(getattr(outcome, "stderr", "") or ""),
            str(getattr(outcome, "stdout", "") or ""),
        ) if part.strip()
    )
    return code, log


def _run(command: str, cwd: Path, timeout: int) -> tuple[int, str]:
    command = _resolve(command)
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # A check that never finishes is a failed check. Reporting it as
        # anything else would let an infinite loop score as a pass.
        return 124, f"verification timed out after {timeout}s"
    except Exception as exc:
        return 125, f"could not run command: {exc}"
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part.strip())
    return proc.returncode, output


def load_suite(path: str | Path) -> tuple[str, list[TaskSpec]]:
    """Read a YAML suite file into task specs."""
    import yaml

    file_path = Path(path).expanduser().resolve()
    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    name = str(data.get("suite") or file_path.stem)
    tasks = [TaskSpec.from_dict(entry) for entry in (data.get("tasks") or [])]

    duplicates = {t.id for t in tasks if [x.id for x in tasks].count(t.id) > 1}
    if duplicates:
        raise ValueError(f"duplicate task id(s) in {file_path.name}: {', '.join(sorted(duplicates))}")
    return name, tasks


def _prepare_workspace(task: TaskSpec, fixtures_root: Path, scratch: Path) -> Path:
    workspace = scratch / task.id
    if workspace.exists():
        shutil.rmtree(workspace)
    if task.fixture:
        source = fixtures_root / task.fixture
        if not source.is_dir():
            raise FileNotFoundError(f"fixture not found: {source}")
        # Never copy build residue. A __pycache__ carried over from a run of
        # the fixture in place holds bytecode for the *broken* source, and
        # Python will happily import it — so a task could score red after a
        # correct fix, or green before one.
        shutil.copytree(source, workspace, ignore=_IGNORED_FIXTURE_ENTRIES)
    else:
        workspace.mkdir(parents=True)
    return workspace


def run_task(
    task: TaskSpec,
    *,
    solver: Solver,
    fixtures_root: str | Path,
    scratch_root: str | Path | None = None,
    keep_workspace: bool = False,
) -> TaskResult:
    """Prepare, pre-check, solve, and score one task."""
    started = time.time()
    fixtures = Path(fixtures_root).expanduser().resolve()
    scratch = Path(scratch_root).expanduser().resolve() if scratch_root else Path(
        tempfile.mkdtemp(prefix="aria-eval-")
    )
    scratch.mkdir(parents=True, exist_ok=True)

    behaviour: list = []   # filled once the agent has had its turn

    def _result(outcome: str, **kwargs) -> TaskResult:
        return TaskResult(
            task_id=task.id, outcome=outcome,
            seconds=time.time() - started, tags=task.tags, behavior=tuple(behaviour), **kwargs,
        )

    # Everything below runs inside this try so the scratch directory is removed
    # on EVERY path, not just the happy one.
    #
    # It used to open after the setup checks, and the two error returns above
    # it — a missing fixture, a missing dependency — leaked one temp directory
    # each. That is once per error path per run, and the test suite exercises
    # both deliberately, so a full test run leaked two directories every time.
    # 57 of them had accumulated in $TMPDIR before anyone noticed, which is the
    # shape of this class of bug: too small to be felt, never self-correcting.
    try:
        try:
            workspace = _prepare_workspace(task, fixtures, scratch)
        except Exception as exc:
            return _result(ERROR, detail=f"workspace setup failed: {exc}")

        missing = _missing_modules(task.requires)
        if missing:
            return _result(
                ERROR,
                detail=f"environment is missing {', '.join(missing)} — the check cannot run",
            )

        for command in task.setup:
            code, output = _run(command, workspace, task.timeout)
            if code != 0:
                return _result(ERROR, exit_code=code,
                               detail=f"setup command failed: {command}",
                               log=_trim(output))

        # ── the pre-flight ────────────────────────────────────────────────
        # Confirm the task is actually broken before asking anyone to fix it.
        before_code, before_log = _run(task.verify, workspace, task.timeout)
        broken = _could_not_run(before_code, before_log)
        if broken:
            return _result(ERROR, exit_code=before_code, detail=broken, log=_trim(before_log))
        if before_code == 0 and not task.allow_green_start:
            return _result(
                INVALID, exit_code=0,
                detail="fixture already passes its own check — this task measures nothing",
                log=_trim(before_log),
            )

        # ── the agent's turn ──────────────────────────────────────────────
        hidden = _hide(workspace, task.hidden)
        before = _snapshot(workspace)
        try:
            outcome = solver(task.prompt, workspace)
        except Exception as exc:
            # A solver crash is not the agent failing the task; scoring it as
            # a fail would blame the model for a harness or provider outage.
            return _result(ERROR, detail=f"solver raised: {exc}")
        # Only when the solver ran an agent: the pre-flight solver returns
        # nothing, and "write_file was never called" would read as a pass.
        if task.behavior and outcome is not None:
            from .behavior import evaluate

            behaviour.extend(evaluate(task.behavior, str(getattr(outcome, "stdout", "") or "")))

        # ── the score ─────────────────────────────────────────────────────
        # The check runs no matter what the solver reported, because the check
        # is the source of truth and the solver's exit code is not. A run that
        # edited the file correctly and then exited 1 on an empty final message
        # is a PASS: the work is on disk and the tests are green.
        changed = _changed_paths(before, _snapshot(workspace))
        if hidden:
            _restore(hidden, fixtures / task.fixture, workspace)

        # Checked before the verdict, and it overrides a green check. Observed
        # in a real run: an agent edited test_settlement.py rather than the
        # module under test. The suite would have called that a PASS and the
        # number would have been a lie.
        #
        # Only files that were there for the agent to tamper with: a test file
        # it writes for itself is ordinary work, and with hidden graders the
        # tests it is told not to edit are not there at all.
        tampered = _violates_protection([rel for rel in changed if rel in before], task.protect)
        if tampered:
            return _result(
                FAIL,
                detail=f"modified protected file(s): {', '.join(tampered)}",
                changed=changed,
            )

        after_code, after_log = _run(task.verify, workspace, task.timeout)
        if after_code == 0:
            return _result(PASS, exit_code=0, changed=changed)
        broken = _could_not_run(after_code, after_log)
        if broken:
            return _result(ERROR, exit_code=after_code, detail=broken,
                           log=_trim(after_log), changed=changed)

        # Red — now the solver's status decides who is to blame. A subprocess
        # that died in seconds on a provider outage or a crash never gave the
        # agent its turn, and scoring that as FAIL makes an infrastructure
        # problem look like an incapable model. That is exactly the confusion
        # this harness separates PASS/FAIL from ERROR to avoid.
        failure = _solver_failure(outcome)
        if failure is not None:
            code, log = failure
            return _result(
                ERROR, exit_code=code,
                detail=f"the agent did not complete (exit {code}); check still red",
                log=_trim(log), changed=changed,
            )

        detail = f"`{task.verify}` exited {after_code}"
        if not changed:
            detail += " — the agent changed nothing"
        # The check's output says what is wrong; the agent's last words say
        # what it thought it did. inventory-reorder failed twice on "reorder.json
        # was not written" with nothing to show whether the agent computed the
        # policy and only reported it, or never got that far.
        agent_said = "\n".join(
            part for part in (str(getattr(outcome, "stdout", "") or ""), str(getattr(outcome, "stderr", "") or ""))
            if part.strip()
        )
        agent_tail = "\n".join([line for line in agent_said.splitlines() if line.strip()][-8:])
        log = _trim(after_log)
        if agent_tail:
            log = f"{log}\n--- agent, last lines ---\n{agent_tail}"
        return _result(
            FAIL, exit_code=after_code, detail=detail,
            log=log, changed=changed,
        )
    finally:
        if not keep_workspace and scratch_root is None:
            shutil.rmtree(scratch, ignore_errors=True)


def run_suite(
    tasks: Sequence[TaskSpec],
    *,
    solver: Solver,
    fixtures_root: str | Path,
    name: str = "suite",
    scratch_root: str | Path | None = None,
    only: Iterable[str] = (),
    tags: Iterable[str] = (),
    on_result: Optional[Callable[[TaskResult], None]] = None,
    keep_workspace: bool = False,
) -> SuiteResult:
    """Run every task and collect the scoreboard."""
    wanted_ids = {str(i) for i in only if i}
    wanted_tags = {str(t) for t in tags if t}

    selected = [
        task for task in tasks
        if (not wanted_ids or task.id in wanted_ids)
        and (not wanted_tags or wanted_tags & set(task.tags))
    ]

    suite = SuiteResult(name=name)
    for task in selected:
        result = run_task(
            task,
            solver=solver,
            fixtures_root=fixtures_root,
            scratch_root=scratch_root,
            keep_workspace=keep_workspace,
        )
        suite.results.append(result)
        if on_result is not None:
            on_result(result)
    return suite


def write_report(suite: SuiteResult, path: str | Path) -> Path:
    """Persist the scoreboard as JSON so runs can be compared over time."""
    out = Path(path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(suite.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return out
