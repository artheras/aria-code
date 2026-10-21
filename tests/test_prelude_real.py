import json
import os
import stat

import pytest

from aria_code.training import prelude_real as R


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "HOME", tmp_path / "prelude")
    monkeypatch.setattr(R, "CANDIDATES", tmp_path / "prelude" / "real_candidates.jsonl")
    monkeypatch.setattr(R, "LABELLED", tmp_path / "prelude" / "real_test.jsonl")
    return tmp_path / "prelude"


def test_private_looking_text_never_becomes_a_candidate():
    kept = R.candidates([
        "ok", "plan my week please", "plan my week please", "mail me at someone@example.com about it",
        "call +86 138 0013 8000 tomorrow", "use sk-abcdefghijklmnopqrstuv to call it", "password: hunter2 for the db",
        "A" * 40, "分析一下这只股票的基本面",
    ])
    assert kept == ["plan my week please", "分析一下这只股票的基本面"]


def test_extract_reads_logs_and_files_are_owner_only(home, tmp_path):
    logs = tmp_path / "logs" / "2026-01-01"
    logs.mkdir(parents=True)
    (logs / "a.json").write_text(json.dumps({"instruction": "explain what a monad is", "user_id": "u1"}))
    (logs / "b.json").write_text("not json")
    result = R.extract(tmp_path / "logs")
    assert result["candidates"] == 1
    assert stat.S_IMODE(os.stat(R.CANDIDATES).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    assert "user_id" not in R.CANDIDATES.read_text()


def test_labelling_is_resumable_and_skips_stay_out(home):
    R._write_private(R.CANDIDATES, [{"text": t} for t in ("one", "two three", "four five")])
    answers = iter(["c", "s", "q"])
    assert R.label(ask=lambda _p: next(answers), show=lambda *_: None) == {"labelled": 1}
    again = iter(["f", "q"])
    assert R.label(ask=lambda _p: next(again), show=lambda *_: None) == {"labelled": 2}
    labels = [json.loads(line) for line in R.LABELLED.read_text().splitlines()]
    assert [r["label"] for r in labels] == ["coding", "finance"]
