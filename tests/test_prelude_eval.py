from aria_code.training import prelude_eval as P


def test_risk_labels_come_from_the_rules():
    from aria_code.safety.risk import assess_command

    rows = P.load("risk")
    assert rows and all(r["label"] == assess_command(r["input"]).level for r in rows[:50])
    assert {r["label"] for r in rows} == {0, 1, 2, 3, 4}


def test_a_family_never_straddles_the_split():
    seen = {}
    for task in ("risk", "intent"):
        for row in P.load(task):
            assert seen.setdefault((task, row["family"]), row["split"]) == row["split"]


def test_committed_files_match_the_builder():
    assert [r["id"] for r in P.build_risk()] == [r["id"] for r in P.load("risk")]
    assert P.build_intent() == P.load("intent")


def test_predictions_parse_strictly():
    assert P.parse_prediction("risk", " 3.") == 3
    assert P.parse_prediction("risk", "five") is None
    assert P.parse_prediction("intent", "Realtime\n") == "realtime"
    assert P.parse_prediction("intent", "no idea") is None


def test_under_call_counts_a_missing_answer_as_a_miss():
    rows = [{"id": "a", "label": 4, "split": "dev"}, {"id": "b", "label": 4, "split": "test"},
            {"id": "c", "label": 0, "split": "dev"}]
    report = P.score("risk", rows, {"a": 4, "b": None, "c": 3})
    assert report["all"]["under_calls"] == "1/2"
    assert report["all"]["over_call_rate"] == 1.0
    assert report["test"]["n"] == 1
