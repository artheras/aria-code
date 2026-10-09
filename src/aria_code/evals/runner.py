"""Command-line entry point for the verifiable eval suites.

    python3 -m aria_code.evals.runner evals/suites/core.yaml
    python3 -m aria_code.evals.runner evals/suites/core.yaml --tag logistics
    python3 -m aria_code.evals.runner evals/suites/core.yaml --check   # no agent

``--check`` is the mode worth knowing about.  It runs every task's pre-flight
and nothing else, so it answers "is this suite still measuring anything?"
without a model, an API key, or a minute of runtime.  Run it in CI: a fixture
that has drifted green is a silent scoreboard inflation, and this is what
catches it on the commit that caused it rather than a month later.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .harness import (
    ERROR,
    FAIL,
    INVALID,
    PASS,
    SuiteResult,
    TaskResult,
    load_suite,
    run_suite,
    write_report,
)

_ICONS = {PASS: "✓", FAIL: "✗", INVALID: "⚠", ERROR: "!"}


class _TimedOut:
    """Marker: the agent was cut off before it could finish.

    Carries a non-zero ``returncode`` so the harness's existing solver-failure
    check classifies it as ERROR — excluded from the score — rather than as a
    task the model failed.
    """

    returncode = 124

    def __init__(self, seconds: int, stdout: str = "", stderr: str = "") -> None:
        self.stderr = f"{stderr}\nthe agent was still working when the {seconds}s budget ran out".strip()
        self.stdout = stdout


class Trajectories:
    """Each attempt's events as JSONL, closed with the outcome the check gave.

    The material a model learns an agent's job from is the sequence, not the
    final answer: which file it read, what it ran, what came back, what it
    changed, and whether the check then passed. `-p --format jsonl` already
    emits that; this keeps one file per task attempt and, once the suite is
    scored, appends an `eval.outcome` line, so passing runs can be selected.
    """

    def __init__(self, root: Path, suite: str, model: str) -> None:
        self.root = Path(root) / suite
        self.root.mkdir(parents=True, exist_ok=True)
        self.model = model
        self._files: dict[str, list[Path]] = {}

    def record(self, task_id: str, prompt: str, stdout: str) -> Path:
        attempt = len(self._files.get(task_id, [])) + 1
        path = self.root / f"{task_id}-{attempt}.jsonl"
        header = json.dumps({"type": "eval.task", "task_id": task_id, "attempt": attempt,
                             "model": self.model, "prompt": prompt}, ensure_ascii=False)
        body = "\n".join(line for line in str(stdout or "").splitlines() if line.startswith("{"))
        path.write_text(header + "\n" + (body + "\n" if body else ""), encoding="utf-8")
        self._files.setdefault(task_id, []).append(path)
        return path

    def close(self, results) -> None:
        """Append each attempt's outcome, in the order the attempts ran."""
        seen: dict[str, int] = {}
        for result in results:
            files = self._files.get(result.task_id, [])
            index = seen.get(result.task_id, 0)
            seen[result.task_id] = index + 1
            if index >= len(files):
                continue
            line = json.dumps({"type": "eval.outcome", "outcome": result.outcome, "detail": result.detail,
                               "changed": list(result.changed), "seconds": round(result.seconds, 2)},
                              ensure_ascii=False)
            with files[index].open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def check_only_solver(prompt: str, workspace: Path) -> None:
    """A solver that does nothing, so only the pre-flight is exercised.

    Every task should come back FAIL under it. A PASS means the fixture is
    already green; anything else means the fixture or its check is broken.
    """
    return None


def build_agent_solver(*, model: str = "", timeout: int = 900, local: bool = False,
                       trajectories: "Trajectories | None" = None, events: bool = False):
    """Drive the real Aria agent over one task, in the task's own workspace.

    The agent is invoked as a subprocess in headless mode rather than in this
    process on purpose: a task that leaves the interpreter's cwd, module cache
    or event loop in a strange state then cannot contaminate the next task's
    score.

    ``--dangerously-skip-permissions`` is not optional here, and it is safe for
    the same reason the flag is dangerous everywhere else: the agent is pointed
    at a throwaway copy of a fixture. Without it every write waits on an
    approval prompt that no one is there to answer, so every task times out and
    the suite scores zero for a reason that has nothing to do with the model.
    """
    import subprocess

    def _solve(prompt: str, workspace: Path):
        command = [
            sys.executable, "-m", "aria_code.aria_cli",
            "-p", prompt,
            "--dangerously-skip-permissions",
            "--no-banner",
        ]
        if trajectories is not None or events:
            # One JSON event per tool call on stdout; the screen output stays
            # on stderr, which is what a failed task's log tail shows. Behaviour
            # checks read these events too.
            command.extend(["--format", "jsonl"])
        if model:
            command.extend(["--model", model])
        if local:
            command.append("--local")
        try:
            env = None
            if trajectories is not None or events:
                import os
                env = {**os.environ, "ARIA_EVENTS_FULL": "1"}
            done = subprocess.run(
                command,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
            if trajectories is not None:
                trajectories.record(workspace.name, prompt, done.stdout)
            return done
        except subprocess.TimeoutExpired as exc:
            # A truncated run is not a measured failure.
            #
            # I first scored this as FAIL, reasoning "it had its time and
            # produced nothing". That holds only when the budget is generous
            # relative to how long the work takes. It was not: the same task
            # completed in 78–146s alone and exceeded 600s inside a suite,
            # where back-to-back turns contend and get throttled. Scoring that
            # as FAIL blamed the model for the harness cutting it off — which
            # is precisely the confusion PASS/FAIL vs ERROR exists to prevent.
            # TimeoutExpired may carry bytes even with text=True. Keep partial
            # tool events and diagnostics so a stalled run can be investigated.
            def _text(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")
            stdout, stderr = _text(exc.stdout), _text(exc.stderr)
            if trajectories is not None:
                trajectories.record(workspace.name, prompt, stdout)
            return _TimedOut(timeout, stdout, stderr)

    return _solve


def _print_result(result: TaskResult) -> None:
    icon = _ICONS.get(result.outcome, "?")
    line = f"  {icon} {result.task_id}  ({result.seconds:.1f}s)"
    if result.detail:
        line += f"  — {result.detail}"
    print(line)
    for item in result.behavior:
        print(f"      {'✓' if item['passed'] else '✗'} {item['check']} — {item['detail']}")
    if result.outcome == FAIL and result.changed:
        print(f"      changed: {', '.join(result.changed)}")
    if result.outcome in (FAIL, INVALID, ERROR) and result.log:
        for log_line in result.log.splitlines()[-8:]:
            print(f"      {log_line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aria-evals", description=__doc__)
    parser.add_argument("suite", help="path to a suite YAML file")
    parser.add_argument("--fixtures", default="", help="fixtures root (default: <suite>/../fixtures)")
    parser.add_argument("--only", nargs="*", default=[], help="run only these task ids")
    parser.add_argument("--tag", nargs="*", default=[], help="run only tasks with these tags")
    parser.add_argument("--check", action="store_true",
                        help="run only the pre-flight: verify every task still starts red")
    parser.add_argument("--model", default="", help="model to pass to the agent")
    parser.add_argument("--local", action="store_true",
                        help="run the agent local-only (Ollama), skipping the cloud backend")
    parser.add_argument("--solve-timeout", type=int, default=900,
                        help="seconds to give the agent per task (default 900)")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="run the suite N times; pass@1 is then passes/attempts (default 1)")
    parser.add_argument("--report", default="", help="write the scoreboard to this JSON path")
    parser.add_argument("--keep", action="store_true", help="keep task workspaces for inspection")
    parser.add_argument("--trajectories", default="",
                        help="write each attempt's events (JSONL) and outcome under this folder")
    parser.add_argument("--scratch", default="", help="scratch root (implies --keep)")
    args = parser.parse_args(argv)

    suite_path = Path(args.suite).expanduser().resolve()
    fixtures = Path(args.fixtures).expanduser().resolve() if args.fixtures else (
        suite_path.parent.parent / "fixtures"
    )

    try:
        name, tasks = load_suite(suite_path)
    except Exception as exc:
        print(f"could not load suite: {exc}", file=sys.stderr)
        return 2

    if not tasks:
        print(f"suite {name} has no tasks", file=sys.stderr)
        return 2

    trajectories = (Trajectories(Path(args.trajectories), name, args.model)
                    if args.trajectories and not args.check else None)
    solver = check_only_solver if args.check else build_agent_solver(
        model=args.model, timeout=args.solve_timeout, local=args.local, trajectories=trajectories,
        events=any(task.behavior for task in tasks),
    )
    mode = "pre-flight only" if args.check else f"agent{f' ({args.model})' if args.model else ''}"
    print(f"\n{name} — {len(tasks)} task(s), {mode}\n")

    repeats = max(1, args.repeat)
    suite: SuiteResult | None = None
    for attempt in range(repeats):
        if repeats > 1:
            print(f"  — attempt {attempt + 1}/{repeats} —")
        run = run_suite(
            tasks,
            solver=solver,
            fixtures_root=fixtures,
            name=name,
            only=args.only,
            tags=args.tag,
            scratch_root=args.scratch or None,
            keep_workspace=args.keep,
            on_result=_print_result,
        )
        if suite is None:
            suite = run
        else:
            suite.merge(run)
    assert suite is not None
    if trajectories is not None:
        trajectories.close(suite.results)
        print(f"trajectories → {trajectories.root}")

    print(f"\n{suite.summary_line()}")
    if repeats > 1:
        # Print the per-task record, because an aggregate hides the tasks that
        # flip. A task at 2/3 is a different engineering problem from two tasks
        # at 1/1 and 1/2.
        for task_id, stats in sorted(suite.per_task().items()):
            if stats["attempts"]:
                print(f"    {task_id:<28} {stats['passed']}/{stats['attempts']}")
    by_tag = suite.by_tag()
    if by_tag and not args.check:
        for tag, stats in by_tag.items():
            if stats["scored"]:
                print(f"    {tag:<16} {stats['passed']}/{stats['scored']}  ({stats['pass_rate']:.0%})")

    if args.report:
        print(f"\nreport → {write_report(suite, args.report)}")

    if args.check:
        # In check mode the only acceptable outcome is "every task is still
        # broken". A pass here means a fixture drifted green and the suite is
        # quietly measuring less than it claims.
        #
        # "Could not run" is reported separately from "drifted green" because
        # they call for opposite responses and were previously indistinguishable:
        # CI installed the package without pytest, every task errored with
        # "environment is missing pytest", and the verdict said five fixtures had
        # stopped being red. That reads as a fixture problem and sends whoever
        # sees it to look at the fixtures, which are fine.
        errored = [r for r in suite.results if r.outcome == ERROR]
        drifted = [r.task_id for r in suite.results
                   if r.outcome != FAIL and r.outcome != ERROR]

        if errored:
            print(f"\n✗ {len(errored)} task(s) could not run — this is an "
                  f"environment problem, not a drifted fixture:")
            for result in errored:
                print(f"    {result.task_id}: {result.detail}")
            print("\n  The suite declares what it needs with `requires:`. "
                  "Install it and run again;")
            print("  `pip install -e \".[dev]\"` covers the pytest the shipped "
                  "suite asks for.")
        if drifted:
            print(f"\n✗ {len(drifted)} task(s) no longer start red: {', '.join(drifted)}")
        if errored or drifted:
            return 1
        print(f"\n✓ all {len(suite.results)} task(s) still start red")
        return 0

    return 0 if suite.failed == 0 and suite.errored == 0 and suite.invalid == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
