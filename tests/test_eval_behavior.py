"""Behaviour checks on eval runs, and the reviewer probe — without a model."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aria_code.evals import behavior, review_probe
from aria_code.evals.harness import FAIL, PASS, TaskSpec, load_suite, run_task

ROOT = Path(__file__).resolve().parent.parent


def events(*calls, done=True):
    lines = [json.dumps({"type": "tool.started", "tool": tool, "params": params}) for tool, params in calls]
    if done:
        lines.append(json.dumps({"type": "turn.completed", "success": True}))
    return "\n".join(["not json", *lines])


RUN = events(("read_file", {"path": "pricing.py"}),
             ("mcp__aria__impact_analysis", {"targets": ["pricing.py"]}),
             ("edit_file", {"path": "/w/pricing.py", "symbol": "apply_discount"}))


@pytest.mark.parametrize("raw, passed", [
    ({"called_before": {"tool": "impact_analysis", "path": "pricing.py"}}, True),
    ({"called_before": {"tool": "find_symbol", "path": "pricing.py"}}, False),
    ({"called_before": {"tool": "impact_analysis", "path": "other.py"}}, False),
    ({"param_used": {"tool": "edit_file", "param": "symbol"}}, True),
    ({"param_used": {"tool": "edit_file", "param": "position"}}, False),
    ({"called": "impact_analysis"}, True),
    ({"not_called": "write_file"}, True),
    ({"not_called": ["edit_file", "write_file"]}, False),
])
def test_checks_read_the_tool_calls(raw, passed):
    (result,) = behavior.evaluate([behavior.validate(raw)], RUN)
    assert result["passed"] is passed, result


@pytest.mark.parametrize("raw, message", [
    ({"called": "a", "not_called": "b"}, "exactly one"),
    ({"calld": "a"}, "exactly one"),
    ({"param_used": {"tool": "edit_file"}}, "needs a param"),
    ({"called_before": {"path": "x"}}, "needs a tool"),
])
def test_a_wrong_check_is_refused_when_the_suite_loads(raw, message):
    with pytest.raises(ValueError, match=message):
        TaskSpec.from_dict({"id": "t", "prompt": "p", "verify": "true", "behavior": [raw]})


def _fixture(tmp_path):
    (tmp_path / "fx").mkdir()
    (tmp_path / "fx" / "flag.txt").write_text("red")
    return tmp_path


VERIFY = "python3 -c \"import sys; sys.exit(open('flag.txt').read() != 'green')\""


def test_behaviour_is_reported_beside_the_outcome_never_instead_of_it(tmp_path):
    fixtures = _fixture(tmp_path)
    task = TaskSpec.from_dict({"id": "t", "prompt": "p", "fixture": "fx", "verify": VERIFY, "behavior": [
        {"called_before": {"tool": "impact_analysis", "path": "flag.txt"}, "label": "looked first"},
        {"not_called": "write_file"}]})

    def solver(prompt, workspace):
        (workspace / "flag.txt").write_text("green")
        return SimpleNamespace(returncode=0, stdout=events(("write_file", {"path": "flag.txt"})), stderr="")

    result = run_task(task, solver=solver, fixtures_root=fixtures)
    assert result.outcome == PASS                                    # the work is right
    assert [b["passed"] for b in result.behavior] == [False, False]  # how it got there is not
    assert result.to_dict()["behavior"][0]["check"] == "looked first"


def test_the_pre_flight_judges_no_behaviour(tmp_path):
    from aria_code.evals.runner import check_only_solver

    task = TaskSpec.from_dict({"id": "t", "prompt": "p", "fixture": "fx", "verify": VERIFY,
                               "behavior": [{"not_called": "write_file"}]})
    result = run_task(task, solver=check_only_solver, fixtures_root=_fixture(tmp_path))
    assert result.outcome == FAIL and result.behavior == ()


def test_suite_reports_carry_behaviour_rates(tmp_path):
    from aria_code.evals.harness import SuiteResult, TaskResult

    item = {"check": "looked first", "passed": True, "detail": ""}
    suite = SuiteResult("s", [TaskResult("t", PASS, behavior=(item,)),
                              TaskResult("t", FAIL, behavior=({**item, "passed": False},))])
    assert suite.to_dict()["behavior"] == {"t: looked first": {"passed": 1, "runs": 2}}
    assert "behaviour 1/2" in suite.summary_line()


def test_the_behaviour_suite_loads_and_names_its_fixtures():
    name, tasks = load_suite(ROOT / "evals" / "suites" / "behavior.yaml")
    assert name == "behavior" and len(tasks) == 3
    for task in tasks:
        assert (ROOT / "evals" / "fixtures" / task.fixture).is_dir()
    assert any(task.behavior for task in tasks)


# ── the reviewer probe ─────────────────────────────────────────────────────

def test_review_cases_load():
    cases = review_probe.load_cases(ROOT / "evals" / "review" / "cases.yaml")
    assert {c.expect for c in cases} == {"pass", "block"}
    assert all(c.diff.startswith("--- a/") for c in cases)


def _report(*issues, verdict=None):
    findings = tuple(SimpleNamespace(file="stats.py", issue=i, blocking=True) for i in issues)
    return SimpleNamespace(verdict=verdict or ("blocking" if issues else "pass"), blocking=findings)


def test_the_probe_counts_false_positives_and_misses():
    cases = review_probe.load_cases(ROOT / "evals" / "review" / "cases.yaml")
    answers = {"correct-empty-average": _report("style nit treated as blocking"),
               "empty-average-still-raises": _report()}

    async def review(*, goal, diff):
        return answers[next(c.id for c in cases if c.diff == diff)]

    data = review_probe.summary(asyncio.run(review_probe.run_cases(cases, review)))
    assert data["correct_changes_not_blocked"] == {"passed": 0, "scored": 1}   # false positive
    assert data["wrong_changes_blocked"] == {"passed": 0, "scored": 1}         # miss

    answers.update({"correct-empty-average": _report(),
                    "empty-average-still-raises": _report("An empty list still divides by zero")})
    data = review_probe.summary(asyncio.run(review_probe.run_cases(cases, review)))
    assert data["correct_changes_not_blocked"]["passed"] == 1
    assert data["wrong_changes_blocked"]["passed"] == 1


def test_a_reviewer_error_is_not_a_verdict():
    case = review_probe.Case("c", "g", "d", "pass")
    assert review_probe.score(case, SimpleNamespace(verdict="error", error="timeout", blocking=()))[0] == "error"

    async def broken(**_kwargs):
        raise RuntimeError("quota")

    data = review_probe.summary(asyncio.run(review_probe.run_cases([case], broken)))
    assert data["errors"] == 1 and data["correct_changes_not_blocked"]["scored"] == 0


def test_the_scoreboard_lists_behaviour_checks():
    from aria_code.evals.summary import summarise

    report = {"suite": "behavior", "passed": 1, "scored": 1, "pass_rate": 1.0,
              "results": [{"task_id": "t", "outcome": "pass", "seconds": 3}],
              "behavior": {"t: looked first": {"passed": 2, "runs": 3}}}
    text, code = summarise([report])
    assert "| t: looked first | 2/3 |" in text and code == 0
