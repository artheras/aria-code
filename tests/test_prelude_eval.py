from aria_code.training import prelude_eval as P


def test_risk_labels_come_from_the_rules():
    from aria_code.safety.risk import assess_command

    rows = P.load("risk")
    assert rows and all(r["label"] == assess_command(r["input"]).level for r in rows[:50])
    assert {r["label"] for r in rows} == {0, 1, 2, 3, 4}


def test_no_family_is_on_both_sides_of_the_train_test_line():
    side = {"dev": "train", "extra": "train", "extra2": "train", "test": "test", "test2": "test", "test3": "test"}
    seen = {}
    for task in ("risk", "intent"):
        for row in P.load(task):
            assert seen.setdefault((task, row["family"]), side[row["split"]]) == side[row["split"]], row


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


def test_training_data_never_touches_a_test_split():
    from aria_code.training import prelude_train as T

    rows = P.load("risk")
    held = {r["input"] for r in rows if r["split"] in ("test", "test2", "test3")}
    families = {r["family"] for r in rows if r["split"] in ("test", "test2", "test3")}
    train, valid, _ = T.build_training_data()
    commands = {x["messages"][1]["content"] for x in train + valid}
    assert commands and not commands & held
    assert not any(P.family(c) in families for c in commands)


def test_the_checkpoint_is_chosen_by_validation_loss_alone():
    from aria_code.training import prelude_train as T

    log = "\x1b[1m   1\x1b[0m    val 7.9   1s\n 100    val 0.42   1s\n 200    val 0.17   1s\n 300    val 0.25   1s\n"
    losses = T.validation_losses(log)
    assert losses == {100: 0.42, 200: 0.17, 300: 0.25}
    assert min(losses, key=losses.get) == 200


def test_intent_training_never_reads_the_test_split():
    from aria_code.training import prelude_train as T

    test = {r["input"] for r in P.load("intent") if r["split"] == "test"}
    train, valid, _ = T.build_intent_data()
    used = {x["messages"][1]["content"] for x in train + valid}
    assert used and not used & test
