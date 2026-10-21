"""Consented trajectories become a routing dataset under backtest-style rules."""

import json

import pytest

from aria_code.training import trajectory_dataset as T


def record(owner="o1", *, title="分析 NVDA 估值", tools=(("get_market_data", True), ("get_financials", True)),
           status="succeeded", failed=0, day="2026-10-20", consent=True, extra=None, feedback=None):
    steps = [{"type": "plan", "payload": {"steps": [{"title": "Fetch data", "status": "done"}]}}]
    for tool, ok in tools:
        steps.append({"type": "tool_call", "payload": {"tool": tool}})
        steps.append({"type": "tool_result", "payload": {"tool": tool, "ok": ok, "summary": "done"}})
    steps.append({"type": "artifact", "payload": {"card_type": "chart"}})
    data = {"schema": T.SCHEMA, "trajectory_id": f"{owner}-{title}-{day}-{status}", "recorded_at": f"{day}T10:00:00+00:00",
            "owner": owner, "consent": {"training_opt_in": consent}, "task": {"title": title, "mode": "work"},
            "outcome": {"status": status, "failed_step_count": failed}, "steps": steps,
            "labels": {"user_feedback": feedback, "verifier": None}}
    if extra:
        data["task"]["title"] += extra
    return data


def test_only_consented_clean_runs_with_tools_become_examples():
    records = [record(), record(consent=False), record(owner=""), record(extra=" ping me at a@b.io"),
               record(tools=()), record(title="  "), {"schema": "other"}, None]
    result = T.build(records)
    kept = sum(len(v) for v in result["splits"].values())
    assert kept == 1
    assert result["drops"] == {"no_consent": 1, "no_owner": 1, "residual_secret": 1, "no_tools": 1,
                               "no_request": 1, "unknown_schema": 1, "unreadable": 1}


def test_an_owner_never_spans_two_splits():
    records = [record(owner=f"owner-{n % 40}", title=f"task {n}") for n in range(400)]
    result = T.build(records)
    seen = {}
    for split, items in result["splits"].items():
        for item in items:
            owner = item["id"].split("-task")[0]
            assert seen.setdefault(owner, split) == split
    assert all(result["splits"][split] for split in T.SPLITS)


def test_the_future_is_held_out_whoever_made_it():
    records = [record(owner=f"owner-{n}", title=f"task {n}", day="2026-10-01") for n in range(30)]
    records += [record(owner=f"owner-{n}", title=f"later {n}", day="2026-11-05") for n in range(30)]
    result = T.build(records, holdout_after="2026-11-01")
    later = [i for split in T.SPLITS for i in result["splits"][split] if i["request"].startswith("later")]
    assert later and all(i["split"] == "test" for i in later)


def test_failures_are_kept_apart_not_taught_and_not_lost():
    records = [record(), record(title="b", status="failed"), record(title="c", status="cancelled"),
               record(title="d", feedback="thumbs_down"), record(title="e", failed=1,
                                                                 tools=(("get_market_data", False), ("get_market_data", True)))]
    result = T.build(records)
    examples = [i for items in result["splits"].values() for i in items]
    assert sorted(i["outcome"] for i in examples) == ["recovered", "success"]
    assert sorted(i["request"] for i in result["negatives"]) == ["b", "c", "d"]


def test_one_heavy_user_is_capped_and_duplicates_are_dropped():
    records = [record(title=f"t{n}") for n in range(10)] + [record(title="t0")]
    result = T.build(records, per_owner_cap=4)
    assert sum(len(v) for v in result["splits"].values()) == 4
    assert result["drops"]["duplicate"] == 1 and result["drops"]["per_owner_cap"] == 6


def test_the_tuning_target_is_the_successful_tool_order(tmp_path):
    item = T.example(record(tools=(("get_market_data", False), ("get_market_data", True), ("calculate_factors", True))), "train")
    target = json.loads(T.vertex_record(item)["contents"][1]["parts"][0]["text"])
    assert target == {"tools": ["get_market_data", "calculate_factors"]}
    assert item["plan"] == ["Fetch data"] and item["artifacts"] == ["chart"]


def test_test_examples_never_reach_the_tuning_files(tmp_path):
    source = tmp_path / "export.jsonl"
    records = [record(owner=f"owner-{n}", title=f"task {n}") for n in range(60)]
    source.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    assert T.main([str(source), "--out", str(tmp_path / "out")]) == 0
    out = tmp_path / "out"
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["source"]["sha256"] and sum(manifest["examples"].values()) == 60
    test_requests = {json.loads(l)["request"] for l in (out / "test.jsonl").read_text().splitlines()}
    tuned = [json.loads(l)["contents"][0]["parts"][0]["text"]
             for name in ("train", "validation") for l in (out / "vertex" / f"{name}.jsonl").read_text().splitlines()]
    assert test_requests and not test_requests & set(tuned)
    assert not (out / "vertex" / "test.jsonl").exists()


def test_a_bad_holdout_date_is_refused(tmp_path):
    source = tmp_path / "e.jsonl"
    source.write_text("")
    with pytest.raises(SystemExit):
        T.main([str(source), "--out", str(tmp_path / "o"), "--holdout-after", "next week"])
