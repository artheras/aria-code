"""The model scorecard: pass rates with intervals, and what routing could save."""
import json
import subprocess
from pathlib import Path

import pytest

from aria_code.evals import scorecard as sc


def report(suite, results):
    """results: [(task_id, outcome, seconds, tags)]"""
    return {"suite": suite, "results": [
        {"task_id": t, "outcome": o, "seconds": s, "tags": list(tags)} for t, o, s, tags in results]}


def write(root, label, suite, results):
    path = Path(root) / label / f"{suite}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report(suite, results)))


def test_wilson_interval_is_wide_for_few_samples_and_tight_for_many():
    assert sc.wilson(0, 0) == (0.0, 1.0)
    low, high = sc.wilson(3, 3)
    assert 0.4 < low < 0.5 and high == pytest.approx(1.0)  # three passes do not prove a perfect model
    low, high = sc.wilson(90, 100)
    assert 0.82 < low < 0.84 and 0.94 < high < 0.96


def test_labelled_arguments_are_parsed_in_order_and_rejected_when_malformed():
    assert list(sc.parse_labelled(["a=x/y", "b=z"], "--model")) == ["a", "b"]
    for bad in ("nolabel", "=value", "label="):
        with pytest.raises(ValueError):
            sc.parse_labelled([bad], "--model")


def test_model_stats_count_only_scored_attempts_and_split_by_domain():
    suites = {"core": report("core", [
        ("t1", "pass", 10, ["coding"]), ("t1", "fail", 20, ["coding"]), ("t2", "pass", 30, ["finance"]),
        ("t3", "error", 99, ["coding"]), ("t4", "invalid", 1, ["coding"])])}
    s = sc.model_stats(suites)
    assert (s["passed"], s["scored"], s["errored"], s["invalid"]) == (2, 3, 1, 1)
    assert s["mean_seconds"] == pytest.approx(20.0), "errors and invalid runs do not count towards time"
    assert s["domains"]["coding"] == {"passed": 1, "scored": 2, "pass_rate": 0.5}
    assert s["domains"]["finance"]["passed"] == 1


def test_a_model_passes_a_task_when_it_passes_at_least_half_its_attempts():
    assert sc.passes((1, 2)) and sc.passes((2, 3)) and not sc.passes((1, 3)) and not sc.passes((0, 0)) and not sc.passes(None)


def _reports():
    # cheap solves the easy tasks, strong solves everything but 'impossible'
    return {
        "cheap": {"core": report("core", [("easy1", "pass", 5, []), ("easy2", "pass", 5, []),
                                          ("hard1", "fail", 9, []), ("impossible", "fail", 9, [])])},
        "strong": {"core": report("core", [("easy1", "pass", 20, []), ("easy2", "pass", 20, []),
                                           ("hard1", "pass", 40, []), ("impossible", "fail", 40, [])])},
    }


def test_the_oracle_sends_each_task_to_the_cheapest_model_that_passes_it():
    matrix = sc.task_matrix(_reports())
    a = sc.routing_analysis(matrix, ["cheap", "strong"], {"cheap": 0.02, "strong": 0.30})
    assert a["tasks"] == 4 and a["strong"] == "strong" and a["strong_solved"] == 3 and a["oracle_solved"] == 3
    assert a["sent_to"] == {"cheap": 2, "strong": 1}
    assert a["unsolved"] == ["core/impossible"] and a["only_one_model"] == ["core/hard1"]
    # 2 easy tasks on cheap, hard1 on strong, and the unsolved one is still tried on strong.
    assert a["strong_cost"] == pytest.approx(1.20) and a["oracle_cost"] == pytest.approx(2 * 0.02 + 0.30 + 0.30)


def test_without_costs_the_given_order_decides_and_no_spend_is_claimed():
    a = sc.routing_analysis(sc.task_matrix(_reports()), ["cheap", "strong"])
    assert a["sent_to"] == {"cheap": 2, "strong": 1} and a["has_costs"] is False


def test_routing_is_worthless_when_the_cheap_model_never_passes_what_strong_does_not_need_it_for():
    reports = {"cheap": {"s": report("s", [("a", "fail", 1, []), ("b", "fail", 1, [])])},
               "strong": {"s": report("s", [("a", "pass", 1, []), ("b", "pass", 1, [])])}}
    a = sc.routing_analysis(sc.task_matrix(reports), ["cheap", "strong"], {"cheap": 0.01, "strong": 0.1})
    assert a["sent_to"] == {"strong": 2} and a["oracle_cost"] == a["strong_cost"]


def test_the_markdown_has_the_table_the_domains_and_the_caveat(tmp_path):
    for label, by_suite in _reports().items():
        for suite, rep in by_suite.items():
            write(tmp_path, label, suite, [(r["task_id"], r["outcome"], r["seconds"], ["coding"]) for r in rep["results"]])
    text = sc.render_markdown(sc.load_reports(tmp_path), {"cheap": 0.02, "strong": 0.30})
    assert "| cheap | 50% |" in text and "| strong | 75% |" in text
    assert "coding" in text and "Sent to: cheap 2, strong 1." in text and "upper bound" in text
    assert "less)" in text


def test_load_reports_skips_files_that_are_not_reports(tmp_path):
    write(tmp_path, "m", "core", [("t", "pass", 1, [])])
    (tmp_path / "m" / "junk.json").write_text("{not json")
    (tmp_path / "m" / "other.json").write_text(json.dumps({"hello": 1}))
    assert list(sc.load_reports(tmp_path)["m"]) == ["core"]


# ── run ──────────────────────────────────────────────────────────────────────

SUITE = Path(__file__).resolve().parents[1] / "evals" / "suites" / "core.yaml"


def test_run_without_yes_only_prints_the_plan_and_runs_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("nothing may run without --yes"))
    code = sc.main(["run", "--model", "a=google/x", "--model", "b=anthropic/y", "--suite", str(SUITE),
                    "--repeat", "2", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0 and "Nothing was run" in out and "spends real money" in out
    assert "60 agent runs" in out or "agent runs" in out  # 30 tasks x 2 repeats, per model
    assert "a " in out and "b " in out


def test_run_with_yes_runs_each_pair_once_and_skips_finished_ones(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_run(command, check=False):
        calls.append(command)
        report_path = Path(command[command.index("--report") + 1])
        report_path.write_text(json.dumps(report("core", [("t", "pass", 1, [])])))
        return subprocess.CompletedProcess(command, 1)  # a task the model failed exits 1: not an error

    monkeypatch.setattr(subprocess, "run", fake_run)
    args = ["run", "--model", "a=google/x", "--model", "b=anthropic/y", "--suite", str(SUITE),
            "--only", "csv-quoted-fields", "--repeat", "1", "--out", str(tmp_path), "--yes"]
    assert sc.main(args) == 0
    assert len(calls) == 2
    first = calls[0]
    assert first[first.index("--model") + 1] == "google/x" and "--trajectories" in first and "csv-quoted-fields" in first
    assert sc.main(args) == 0 and len(calls) == 2, "finished runs are not repeated"
    assert sc.main(args + ["--force"]) == 0 and len(calls) == 4


def test_a_runner_that_cannot_start_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda command, check=False: subprocess.CompletedProcess(command, 2))
    assert sc.main(["run", "--model", "a=google/x", "--suite", str(SUITE), "--only", "csv-quoted-fields",
                    "--out", str(tmp_path), "--yes"]) == 1


def test_report_command_needs_reports(tmp_path, capsys):
    assert sc.main(["report", str(tmp_path)]) == 2
    write(tmp_path, "m", "core", [("t", "pass", 1, [])])
    assert sc.main(["report", str(tmp_path), "--cost", "m=0.1"]) == 0
    assert "Model scorecard" in capsys.readouterr().out


def test_a_local_label_runs_the_agent_local_only(tmp_path):
    job = {"label": "qwen", "model": "qwen2.5:7b", "suite": "s.yaml", "report": tmp_path / "r.json"}
    assert "--local" in sc.command_for(job, 1, tmp_path, 60, "python", [], ["qwen"])
    assert "--local" not in sc.command_for(job, 1, tmp_path, 60, "python", [], ["other"])


def test_a_task_id_that_is_in_no_suite_is_an_error_not_a_silent_zero(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    code = sc.main(["run", "--model", "a=google/x", "--suite", str(SUITE), "--only", "csv_quoted_fields",
                    "--out", str(tmp_path), "--yes"])
    assert code == 2 and "no such task in the suites: csv_quoted_fields" in capsys.readouterr().err


def test_a_task_nobody_solves_is_not_free_in_the_cost_comparison():
    reports = {"only": {"s": report("s", [("a", "fail", 1, [])])}}
    a = sc.routing_analysis(sc.task_matrix(reports), ["only"], {"only": 0.05})
    assert a["oracle_solved"] == 0 and a["oracle_cost"] == pytest.approx(0.05) == a["strong_cost"]
