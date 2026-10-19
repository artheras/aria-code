"""Tests for the verifiable eval harness.

The behaviour that matters most here is not that a passing task scores a pass.
It is that a task which cannot measure anything says so, loudly, instead of
inflating the number.
"""

import pathlib
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aria_code.evals import SuiteResult, TaskSpec, load_suite, run_suite, run_task
from aria_code.evals.harness import ERROR, FAIL, INVALID, PASS, TaskResult, write_report


def _fixture(root: Path, name: str, files: dict) -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# A check that fails until `fixed.txt` exists next to it.
_GUARD = '''
    import pathlib, sys
    sys.exit(0 if (pathlib.Path(__file__).parent / "fixed.txt").exists() else 1)
'''


class HarnessBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.fixtures = self.root / "fixtures"
        self.fixtures.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _task(self, **kwargs):
        base = dict(
            id="t1",
            prompt="fix it",
            verify="{python} check.py",
            fixture="broken",
        )
        base.update(kwargs)
        return TaskSpec(**base)

    def _run(self, task, solver):
        return run_task(task, solver=solver, fixtures_root=self.fixtures)


class ScoringTests(HarnessBase):
    def test_a_solver_that_fixes_it_passes(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})

        def solver(prompt, workspace):
            (workspace / "fixed.txt").write_text("done", encoding="utf-8")

        result = self._run(self._task(), solver)
        self.assertEqual(result.outcome, PASS)
        self.assertEqual(result.exit_code, 0)

    def test_a_solver_that_does_nothing_fails(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        result = self._run(self._task(), lambda p, w: None)
        self.assertEqual(result.outcome, FAIL)
        self.assertEqual(result.exit_code, 1)

    def test_a_confident_summary_is_not_a_pass(self):
        # The whole point of exit-code scoring: saying it is done does nothing.
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        result = self._run(self._task(), lambda p, w: "任务完成，已修复")
        self.assertEqual(result.outcome, FAIL)


class PreflightTests(HarnessBase):
    def test_a_fixture_that_starts_green_is_invalid_not_a_pass(self):
        _fixture(self.fixtures, "broken", {
            "check.py": "import sys; sys.exit(0)\n",
        })
        result = self._run(self._task(), lambda p, w: None)
        self.assertEqual(result.outcome, INVALID)
        self.assertIn("measures nothing", result.detail)

    def test_an_invalid_task_is_excluded_from_the_pass_rate(self):
        suite = SuiteResult(name="s", results=[
            TaskResult(task_id="a", outcome=PASS),
            TaskResult(task_id="b", outcome=INVALID),
            TaskResult(task_id="c", outcome=ERROR),
        ])
        # 1 of 1 scored, not 1 of 3 — a broken fixture must neither look like
        # an agent regression nor hide one.
        self.assertEqual(suite.scored, 1)
        self.assertEqual(suite.pass_rate, 1.0)

    def test_a_regression_guard_may_opt_out_of_starting_red(self):
        _fixture(self.fixtures, "broken", {"check.py": "import sys; sys.exit(0)\n"})
        result = self._run(self._task(allow_green_start=True), lambda p, w: None)
        self.assertEqual(result.outcome, PASS)

    def test_the_solver_never_runs_on_an_invalid_task(self):
        _fixture(self.fixtures, "broken", {"check.py": "import sys; sys.exit(0)\n"})
        calls = []
        self._run(self._task(), lambda p, w: calls.append(p))
        self.assertEqual(calls, [])


class EnvironmentTests(HarnessBase):
    def test_a_missing_requirement_errors_rather_than_scoring_a_fail(self):
        # The failure this exists for: five tasks once reported red because
        # the python3 on PATH had no pytest. That is not the agent's score.
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        result = self._run(self._task(requires=("definitely_not_installed_xyz",)), lambda p, w: None)
        self.assertEqual(result.outcome, ERROR)
        self.assertIn("definitely_not_installed_xyz", result.detail)

    def test_python_placeholder_resolves_to_the_running_interpreter(self):
        import sys

        _fixture(self.fixtures, "broken", {
            "check.py": f"import sys; sys.exit(0 if sys.executable == {sys.executable!r} else 1)\n",
        })
        result = self._run(self._task(), lambda p, w: None)
        # Green start, so INVALID — which proves the interpreter matched.
        self.assertEqual(result.outcome, INVALID)

    def test_an_interpreter_path_with_a_space_still_runs_the_check(self):
        # The macOS app keeps projects under "Application Support". Unquoted,
        # {python} split there and every check exited 126.
        import os
        import sys
        from unittest import mock

        from aria_code.evals import harness

        if os.name == "nt":
            self.skipTest("symlinking an interpreter needs privileges on Windows")
        spaced = self.fixtures.parent / "Application Support" / "python"
        spaced.parent.mkdir(parents=True)
        spaced.symlink_to(sys.executable)
        _fixture(self.fixtures, "broken", {"check.py": "import sys; sys.exit(1)\n"})
        with mock.patch.object(harness.sys, "executable", str(spaced)):
            result = self._run(self._task(), lambda p, w: None)
        self.assertEqual(result.outcome, FAIL, result.detail)
        self.assertEqual(result.exit_code, 1)

    def test_a_check_the_shell_cannot_run_is_an_error_not_a_red_task(self):
        # A pre-flight that counts "command not found" as red reports a healthy
        # suite while measuring nothing.
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        result = self._run(self._task(verify="definitely_not_a_command_xyz"), lambda p, w: None)
        self.assertEqual(result.outcome, ERROR)
        self.assertIn("could not run", result.detail)

    def test_a_check_that_never_finishes_is_a_failure(self):
        _fixture(self.fixtures, "broken", {"check.py": "import time; time.sleep(30)\n"})
        result = self._run(self._task(timeout=1), lambda p, w: None)
        self.assertEqual(result.outcome, FAIL)
        self.assertEqual(result.exit_code, 124)

    def test_a_missing_fixture_errors(self):
        result = self._run(self._task(fixture="nope"), lambda p, w: None)
        self.assertEqual(result.outcome, ERROR)
        self.assertIn("fixture not found", result.detail)

    def test_a_failing_setup_command_errors(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        result = self._run(self._task(setup=("exit 3",)), lambda p, w: None)
        self.assertEqual(result.outcome, ERROR)
        self.assertIn("setup command failed", result.detail)

    def test_a_solver_crash_is_an_error_not_a_failed_task(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})

        def solver(prompt, workspace):
            raise RuntimeError("provider outage")

        result = self._run(self._task(), solver)
        self.assertEqual(result.outcome, ERROR)
        self.assertIn("provider outage", result.detail)


class IsolationTests(HarnessBase):
    def test_the_agent_works_on_a_copy_not_the_fixture(self):
        source = _fixture(self.fixtures, "broken", {"check.py": _GUARD})

        def vandal(prompt, workspace):
            (workspace / "check.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")

        self._run(self._task(), vandal)
        self.assertIn("fixed.txt", source.joinpath("check.py").read_text(encoding="utf-8"))

    def test_two_runs_do_not_see_each_others_changes(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        first = self._run(self._task(), lambda p, w: (w / "fixed.txt").write_text("x"))
        second = self._run(self._task(), lambda p, w: None)
        self.assertEqual(first.outcome, PASS)
        self.assertEqual(second.outcome, FAIL)


class SuiteTests(HarnessBase):
    def _suite_tasks(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        return [
            self._task(id="a", tags=("software",)),
            self._task(id="b", tags=("logistics",)),
            self._task(id="c", tags=("logistics", "hard")),
        ]

    def _fix_only(self, *ids):
        def solver(prompt, workspace):
            if workspace.name in ids:
                (workspace / "fixed.txt").write_text("x", encoding="utf-8")
        return solver

    def test_pass_rate_and_per_tag_scoreboard(self):
        suite = run_suite(
            self._suite_tasks(), solver=self._fix_only("a", "b"),
            fixtures_root=self.fixtures, name="demo",
        )
        self.assertEqual((suite.passed, suite.failed), (2, 1))
        self.assertAlmostEqual(suite.pass_rate, 2 / 3)
        self.assertEqual(suite.by_tag()["logistics"], {"passed": 1, "scored": 2, "pass_rate": 0.5})
        self.assertEqual(suite.by_tag()["software"]["pass_rate"], 1.0)

    def test_tasks_can_be_filtered_by_tag(self):
        suite = run_suite(
            self._suite_tasks(), solver=lambda p, w: None,
            fixtures_root=self.fixtures, tags=["hard"],
        )
        self.assertEqual([r.task_id for r in suite.results], ["c"])

    def test_tasks_can_be_filtered_by_id(self):
        suite = run_suite(
            self._suite_tasks(), solver=lambda p, w: None,
            fixtures_root=self.fixtures, only=["b"],
        )
        self.assertEqual([r.task_id for r in suite.results], ["b"])

    def test_report_is_written_as_json(self):
        import json

        suite = run_suite(
            self._suite_tasks(), solver=self._fix_only("a"),
            fixtures_root=self.fixtures, name="demo",
        )
        path = write_report(suite, self.root / "out" / "report.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["suite"], "demo")
        self.assertEqual(data["passed"], 1)
        self.assertIn("by_tag", data)


class SuiteFileTests(HarnessBase):
    def _write(self, body: str) -> Path:
        path = self.root / "suite.yaml"
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def test_loads_tasks(self):
        self._write("""
            suite: demo
            tasks:
              - id: one
                prompt: do it
                verify: "{python} check.py"
                tags: [software]
        """)
        name, tasks = load_suite(self.root / "suite.yaml")
        self.assertEqual(name, "demo")
        self.assertEqual(tasks[0].tags, ("software",))

    def test_a_task_missing_a_required_field_is_rejected(self):
        self._write("""
            suite: demo
            tasks:
              - id: one
                prompt: do it
        """)
        with self.assertRaises(ValueError) as ctx:
            load_suite(self.root / "suite.yaml")
        self.assertIn("verify", str(ctx.exception))

    def test_duplicate_task_ids_are_rejected(self):
        self._write("""
            suite: demo
            tasks:
              - {id: one, prompt: a, verify: "true"}
              - {id: one, prompt: b, verify: "true"}
        """)
        with self.assertRaises(ValueError) as ctx:
            load_suite(self.root / "suite.yaml")
        self.assertIn("duplicate", str(ctx.exception))


class ShippedSuiteTests(unittest.TestCase):
    """The suite that ships with the repo must keep measuring something."""

    def test_core_suite_loads_and_every_task_is_well_formed(self):
        repo_root = Path(__file__).resolve().parent.parent
        name, tasks = load_suite(repo_root / "evals" / "suites" / "core.yaml")
        self.assertEqual(name, "core")
        self.assertGreaterEqual(len(tasks), 5)
        for task in tasks:
            with self.subTest(task=task.id):
                self.assertTrue(task.tags, "every task needs a tag for the scoreboard")
                self.assertTrue((repo_root / "evals" / "fixtures" / task.fixture).is_dir())

    def test_the_suite_covers_more_than_software(self):
        repo_root = Path(__file__).resolve().parent.parent
        _, tasks = load_suite(repo_root / "evals" / "suites" / "core.yaml")
        tags = {tag for task in tasks for tag in task.tags}
        self.assertTrue({"logistics", "payments"} <= tags)


if __name__ == "__main__":
    unittest.main()


class RepeatAggregationTests(HarnessBase):
    """A single sample is not a score."""

    def _flaky_suite(self, pass_on):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        attempts = {"n": 0}

        def solver(prompt, workspace):
            attempts["n"] += 1
            if attempts["n"] in pass_on:
                (workspace / "fixed.txt").write_text("x", encoding="utf-8")

        return [self._task(id="flaky")], solver

    def test_merge_folds_a_second_run_into_the_first(self):
        tasks, solver = self._flaky_suite(pass_on={1})
        first = run_suite(tasks, solver=solver, fixtures_root=self.fixtures, name="s")
        second = run_suite(tasks, solver=solver, fixtures_root=self.fixtures, name="s")
        first.merge(second)

        self.assertEqual(len(first.results), 2)
        self.assertEqual(first.per_task()["flaky"], {
            "passed": 1, "attempts": 2, "outcomes": [PASS, FAIL],
        })

    def test_pass_rate_across_repeats_is_passes_over_attempts(self):
        tasks, solver = self._flaky_suite(pass_on={1, 3})
        merged = run_suite(tasks, solver=solver, fixtures_root=self.fixtures, name="s")
        for _ in range(3):
            merged.merge(run_suite(tasks, solver=solver, fixtures_root=self.fixtures, name="s"))

        self.assertEqual(merged.scored, 4)
        self.assertEqual(merged.passed, 2)
        self.assertAlmostEqual(merged.pass_rate, 0.5)

    def test_per_task_appears_in_the_json_report(self):
        import json

        tasks, solver = self._flaky_suite(pass_on={1})
        suite = run_suite(tasks, solver=solver, fixtures_root=self.fixtures, name="s")
        data = json.loads(write_report(suite, self.root / "r.json").read_text(encoding="utf-8"))
        self.assertIn("flaky", data["per_task"])


class FixtureHygieneTests(HarnessBase):
    """Build residue must not travel into a task workspace."""

    def test_pycache_is_not_copied_into_the_workspace(self):
        # A __pycache__ left by running the fixture in place holds bytecode for
        # the *broken* source, and Python will import it — so a task could
        # score red after a correct fix.
        source = _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        (source / "__pycache__").mkdir()
        (source / "__pycache__" / "stale.pyc").write_bytes(b"\x00")
        (source / ".pytest_cache").mkdir()

        seen = {}

        def solver(prompt, workspace):
            seen["entries"] = {p.name for p in workspace.iterdir()}
            (workspace / "fixed.txt").write_text("x", encoding="utf-8")

        self._run(self._task(), solver)
        self.assertNotIn("__pycache__", seen["entries"])
        self.assertNotIn(".pytest_cache", seen["entries"])
        self.assertIn("check.py", seen["entries"])


class ChangeTrackingTests(HarnessBase):
    """A red check with no edits is a different failure from a wrong edit."""

    def test_a_pass_records_what_was_touched(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        result = self._run(
            self._task(),
            lambda p, w: (w / "fixed.txt").write_text("x", encoding="utf-8"),
        )
        self.assertEqual(result.outcome, PASS)
        self.assertIn("fixed.txt", result.changed)

    def test_a_failure_after_real_edits_lists_them(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD, "app.py": "x = 1\n"})
        result = self._run(
            self._task(),
            lambda p, w: (w / "app.py").write_text("x = 2\n", encoding="utf-8"),
        )
        self.assertEqual(result.outcome, FAIL)
        self.assertEqual(result.changed, ("app.py",))
        self.assertNotIn("changed nothing", result.detail)

    def test_a_failure_with_no_edits_says_so(self):
        # The distinction that matters: this one means the agent never
        # engaged, and calls for a completely different investigation from a
        # failure where it engaged and got the answer wrong.
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        result = self._run(self._task(), lambda p, w: None)
        self.assertEqual(result.outcome, FAIL)
        self.assertEqual(result.changed, ())
        self.assertIn("changed nothing", result.detail)

    def test_deletions_count_as_changes(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD, "doomed.py": "x = 1\n"})
        result = self._run(self._task(), lambda p, w: (w / "doomed.py").unlink())
        self.assertIn("doomed.py", result.changed)

    def test_bytecode_written_during_the_run_is_not_reported_as_a_change(self):
        # Running the check compiles the fixture; that residue is not the
        # agent's work and would appear on every single task.
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})

        def solver(prompt, workspace):
            cache = workspace / "__pycache__"
            cache.mkdir(exist_ok=True)
            (cache / "check.cpython-312.pyc").write_bytes(b"\x00")

        result = self._run(self._task(), solver)
        self.assertEqual(result.changed, ())

    def test_changed_appears_in_the_json_report(self):
        import json

        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        suite = run_suite(
            [self._task()],
            solver=lambda p, w: (w / "fixed.txt").write_text("x", encoding="utf-8"),
            fixtures_root=self.fixtures, name="s",
        )
        data = json.loads(write_report(suite, self.root / "r.json").read_text(encoding="utf-8"))
        self.assertIn("fixed.txt", data["results"][0]["changed"])


class ProtectedFileTests(HarnessBase):
    """Editing the check is not solving the task."""

    def test_editing_a_test_file_fails_even_when_the_check_goes_green(self):
        # The failure this exists for, seen in a real run: the agent edited
        # test_settlement.py instead of the module under test. Without this the
        # suite scores a PASS and the number is a lie.
        _fixture(self.fixtures, "broken", {
            "check.py": _GUARD,
            "test_thing.py": "def test_x():\n    assert False\n",
        })

        def cheat(prompt, workspace):
            (workspace / "test_thing.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
            (workspace / "fixed.txt").write_text("x", encoding="utf-8")  # check would pass

        result = self._run(self._task(), cheat)
        self.assertEqual(result.outcome, FAIL)
        self.assertIn("protected", result.detail)
        self.assertIn("test_thing.py", result.detail)

    def test_editing_the_source_is_untouched_by_the_rule(self):
        _fixture(self.fixtures, "broken", {
            "check.py": _GUARD,
            "test_thing.py": "def test_x():\n    pass\n",
        })
        result = self._run(
            self._task(),
            lambda p, w: (w / "fixed.txt").write_text("x", encoding="utf-8"),
        )
        self.assertEqual(result.outcome, PASS)

    def test_a_task_may_declare_its_own_protected_set(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD, "rates.csv": "a,1\n"})
        result = self._run(
            self._task(protect=("*.csv",)),
            lambda p, w: ((w / "rates.csv").write_text("a,999\n", encoding="utf-8"),
                          (w / "fixed.txt").write_text("x", encoding="utf-8")),
        )
        self.assertEqual(result.outcome, FAIL)
        self.assertIn("rates.csv", result.detail)

    def test_protection_can_be_switched_off_for_a_task_that_needs_it(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD, "test_thing.py": "x = 1\n"})
        result = self._run(
            self._task(protect=()),
            lambda p, w: ((w / "test_thing.py").write_text("x = 2\n", encoding="utf-8"),
                          (w / "fixed.txt").write_text("x", encoding="utf-8")),
        )
        self.assertEqual(result.outcome, PASS)

    def test_nested_test_directories_are_protected_too(self):
        _fixture(self.fixtures, "broken", {
            "check.py": _GUARD,
            "tests/test_deep.py": "def test_x():\n    pass\n",
        })
        result = self._run(
            self._task(),
            lambda p, w: ((w / "tests" / "test_deep.py").write_text("# gone\n", encoding="utf-8"),
                          (w / "fixed.txt").write_text("x", encoding="utf-8")),
        )
        self.assertEqual(result.outcome, FAIL)


class HiddenGraderTests(HarnessBase):
    """A grader the agent can read is an answer key."""

    # Passes once answer.txt holds 42 — the expected value lives in the grader.
    _GRADER = """
        import pathlib
        def test_answer():
            assert (pathlib.Path(__file__).parent / "answer.txt").read_text().strip() == "42"
    """

    def _hidden_task(self, **kwargs):
        base = dict(verify="{python} -m pytest -q --noconftest -p no:cacheprovider test_grade.py",
                    hidden=("test_grade.py",), protect=("*.csv",))
        base.update(kwargs)
        return self._task(**base)

    def test_the_agent_never_sees_the_grader_or_the_preflights_caches(self):
        _fixture(self.fixtures, "broken", {"test_grade.py": self._GRADER, "data.csv": "x,1\n"})
        seen = {}

        def solver(prompt, workspace):
            seen["files"] = sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*"))
            (workspace / "answer.txt").write_text("42", encoding="utf-8")

        task = self._hidden_task(verify="{python} -m pytest -q --noconftest test_grade.py")
        result = self._run(task, solver)
        self.assertEqual(seen["files"], ["data.csv"])
        self.assertEqual(result.outcome, PASS, result.log)

    def test_the_grader_comes_back_for_the_score(self):
        _fixture(self.fixtures, "broken", {"test_grade.py": self._GRADER})
        result = self._run(self._hidden_task(), lambda p, w: (w / "answer.txt").write_text("41"))
        self.assertEqual(result.outcome, FAIL)
        self.assertIn("test_answer", result.log)

    def test_a_grader_the_agent_writes_is_replaced_by_the_real_one(self):
        _fixture(self.fixtures, "broken", {"test_grade.py": self._GRADER})

        def forge(prompt, workspace):
            (workspace / "test_grade.py").write_text("def test_answer():\n    pass\n")

        self.assertEqual(self._run(self._hidden_task(), forge).outcome, FAIL)

    def test_its_own_test_files_are_not_tampering(self):
        _fixture(self.fixtures, "broken", {"test_grade.py": self._GRADER})

        def solver(prompt, workspace):
            (workspace / "test_mine.py").write_text("def test_mine():\n    pass\n")
            (workspace / "answer.txt").write_text("42")

        result = self._run(self._hidden_task(protect=("test_*.py",)), solver)
        self.assertEqual(result.outcome, PASS, result.detail)

    def test_the_preflight_still_sees_the_grader(self):
        _fixture(self.fixtures, "broken", {"test_grade.py": self._GRADER, "answer.txt": "42"})
        result = self._run(self._hidden_task(), lambda p, w: None)
        self.assertEqual(result.outcome, INVALID)

    def test_the_suite_file_declares_it(self):
        (self.root / "suite.yaml").write_text(textwrap.dedent("""
            suite: s
            tasks:
              - id: a
                prompt: p
                verify: v
                hidden: ["test_*.py"]
        """), encoding="utf-8")
        _, tasks = load_suite(self.root / "suite.yaml")
        self.assertEqual(tasks[0].hidden, ("test_*.py",))


class SolverTimeoutTests(HarnessBase):
    """Being cut off is not the same as getting it wrong."""

    def test_a_timed_out_agent_is_an_error_not_a_failure(self):
        # Measured: the same task finished in 78-146s alone and exceeded 600s
        # inside a suite, where back-to-back turns contend and get throttled.
        # Scoring that FAIL blames the model for the harness cutting it off.
        from aria_code.evals.runner import _TimedOut

        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        result = self._run(self._task(), lambda p, w: _TimedOut(600))
        self.assertEqual(result.outcome, ERROR)
        self.assertIn("600s", result.log)

    def test_a_timeout_after_a_correct_fix_still_passes(self):
        # The check is the source of truth: if the work landed before the
        # budget ran out, it counts.
        from aria_code.evals.runner import _TimedOut

        _fixture(self.fixtures, "broken", {"check.py": _GUARD})

        def slow_but_correct(prompt, workspace):
            (workspace / "fixed.txt").write_text("x", encoding="utf-8")
            return _TimedOut(600)

        self.assertEqual(self._run(self._task(), slow_but_correct).outcome, PASS)


class ScratchCleanupTests(HarnessBase):
    """Every exit path must remove the temp directory it created.

    The cleanup used to sit below the setup checks, so a missing fixture or a
    missing dependency leaked one directory each — and the tests below exercise
    both deliberately, so a full test run leaked two every time. 57 had piled
    up in $TMPDIR before anyone looked. Too small to feel, never self-correcting.
    """

    def _tmp_eval_dirs(self):
        import tempfile

        return set(pathlib.Path(tempfile.gettempdir()).glob("aria-eval-*"))

    def _assert_leaves_nothing(self, task, solver):
        before = self._tmp_eval_dirs()
        self._run(task, solver)
        self.assertEqual(
            self._tmp_eval_dirs() - before, set(),
            "run_task leaked a scratch directory",
        )

    def test_the_happy_path_cleans_up(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        self._assert_leaves_nothing(
            self._task(),
            lambda p, w: (w / "fixed.txt").write_text("x", encoding="utf-8"),
        )

    def test_a_missing_fixture_cleans_up(self):
        self._assert_leaves_nothing(self._task(fixture="nope"), lambda p, w: None)

    def test_a_missing_dependency_cleans_up(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        self._assert_leaves_nothing(
            self._task(requires=("definitely_not_installed_xyz",)), lambda p, w: None
        )

    def test_a_crashing_solver_cleans_up(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})

        def boom(prompt, workspace):
            raise RuntimeError("provider outage")

        self._assert_leaves_nothing(self._task(), boom)

    def test_an_explicit_scratch_root_is_left_alone(self):
        # --scratch means the caller wants the workspaces afterwards.
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        keep = self.root / "keep"
        run_task(
            self._task(), solver=lambda p, w: None,
            fixtures_root=self.fixtures, scratch_root=keep,
        )
        self.assertTrue((keep / "t1").exists())


class RunResidueTests(HarnessBase):
    """Only the agent's work counts as a change."""

    def test_check_residue_is_not_reported_as_the_agents_work(self):
        # Running pytest writes .pytest_cache; reporting it makes every task
        # look like it touched a dozen files and buries the edit that matters.
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})

        def solver(prompt, workspace):
            for noise in (".pytest_cache/v/cache/nodeids", "__pycache__/x.pyc", ".ruff_cache/y"):
                path = workspace / noise
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("noise", encoding="utf-8")
            (workspace / "fixed.txt").write_text("x", encoding="utf-8")

        self.assertEqual(self._run(self._task(), solver).changed, ("fixed.txt",))


class AgentTailTests(HarnessBase):
    """A failed task's log ends with what the agent said, not only the check's output.

    inventory-reorder failed twice on "reorder.json was not written", with
    nothing showing whether the agent computed the policy and only reported it.
    """

    def test_a_failed_task_keeps_the_agents_last_lines(self):
        from types import SimpleNamespace

        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        said = SimpleNamespace(returncode=0, stdout="Computed reorder points for 6 SKUs.\nA100: order 335",
                               stderr="")
        result = self._run(self._task(), lambda p, w: said)
        self.assertEqual(result.outcome, FAIL)
        self.assertIn("--- agent, last lines ---", result.log)
        self.assertIn("A100: order 335", result.log)
        self.assertIn("A100: order 335", result.to_dict()["log_tail"][-1])

    def test_a_silent_agent_adds_nothing(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        result = self._run(self._task(), lambda p, w: None)
        self.assertNotIn("--- agent", result.log)


class FinishedButRedTests(HarnessBase):
    """A non-zero exit is not a crash when the agent says how its turn ended."""

    def _ended(self, code, stop_reason=None, error=""):
        import json
        from types import SimpleNamespace

        events = [json.dumps({"type": "turn.started"})]
        if stop_reason is not None:
            events.append(json.dumps({"type": "turn.completed", "success": False, "error": error, "stop_reason": stop_reason}))
        return SimpleNamespace(returncode=code, stdout="\n".join(events), stderr="")

    def _score(self, outcome):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        return self._run(self._task(), lambda p, w: outcome)

    def test_an_agent_whose_own_checks_stayed_red_is_scored_not_excluded(self):
        for stop in ("checks_failed", "completed", "max_rounds", "budget_exhausted", "loop_guard"):
            self.assertEqual(self._score(self._ended(1, stop)).outcome, FAIL, stop)

    def test_a_provider_error_is_still_an_error_not_the_models_score(self):
        http = "HTTP 400: Unsupported parameter: 'max_tokens'"
        self.assertEqual(self._score(self._ended(1, http, error=http)).outcome, ERROR)

    def test_a_run_that_never_reported_a_turn_is_still_an_error(self):
        self.assertEqual(self._score(self._ended(1, None)).outcome, ERROR)
        self.assertEqual(self._score(self._ended(124, None)).outcome, ERROR)

    def test_a_green_check_is_a_pass_whatever_the_exit_code(self):
        _fixture(self.fixtures, "broken", {"check.py": _GUARD})
        solved = lambda p, w: (Path(w) / "fixed.txt").write_text("x") or self._ended(1, "checks_failed")  # noqa: E731
        self.assertEqual(self._run(self._task(), solved).outcome, PASS)
