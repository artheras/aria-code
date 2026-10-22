import asyncio
import json
import os
import stat

import pytest

from aria_code import intent_classifier as C
from aria_code.apps.cli import intent_shadow as S


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIA_PRELUDE_HOME", str(tmp_path))
    monkeypatch.delenv("ARIA_PRELUDE_MLX", raising=False)
    calls = {"mlx": 0, "default": 0}

    async def fake_default(message, url="", *, timeout=0):
        calls["default"] += 1
        return "general"

    async def fake_mlx(message, **kwargs):
        calls["mlx"] += 1
        return calls.get("answer")

    monkeypatch.setattr(C, "_classify_default_async", fake_default)
    monkeypatch.setattr(C, "_classify_mlx", fake_mlx)
    return calls


def run(message="分析一下腾讯"):
    return asyncio.run(C.classify_intent_async(message))


def test_off_by_default_never_asks_the_model(env):
    assert run() == "general"
    assert env == {"mlx": 0, "default": 1}


def test_on_uses_the_models_label_and_falls_back_when_it_is_silent(env, monkeypatch):
    monkeypatch.setenv("ARIA_PRELUDE_MLX", "on")
    env["answer"] = "analysis"
    assert run() == "analysis" and env["default"] == 0
    env["answer"] = None
    assert run() == "general" and env["default"] == 1


def test_shadow_keeps_the_default_answer_and_records_no_text(env, monkeypatch, tmp_path):
    monkeypatch.setenv("ARIA_PRELUDE_MLX", "shadow")
    env["answer"] = "analysis"
    secret = "分析一下腾讯 my account number is 12345"
    assert run(secret) == "general"
    file = tmp_path / "shadow.jsonl"
    body = file.read_text(encoding="utf-8")
    assert "腾讯" not in body and "12345" not in body
    row = json.loads(body)
    assert row == {**row, "model": "analysis", "default": "general", "agree": False, "chars": len(secret)}
    assert stat.S_IMODE(os.stat(file).st_mode) == 0o600


def test_summary_counts_agreement_and_silence():
    lines = [{"model": "finance", "default": "finance", "agree": True},
             {"model": "analysis", "default": "general", "agree": False},
             {"model": None, "default": "general", "agree": None}]
    result = S.summary(lines)
    assert result["agreement"] == 0.5 and result["model_silent"] == 1
    assert result["most_common_disagreements"] == {"general -> analysis": 1}


@pytest.mark.parametrize("raw, label", [("finance", "finance"), (" Realtime.\n", "realtime"),
                                        ("coding or analysis", None), ("no idea", None), ("", None)])
def test_a_reply_must_name_exactly_one_label(raw, label):
    assert C.parse_label(raw) == label


def test_a_visual_artifact_request_skips_the_experiment(env, monkeypatch):
    monkeypatch.setenv("ARIA_PRELUDE_MLX", "on")
    env["answer"] = "finance"
    assert run("生成苹果近一年K线图 png") == "general"   # the default path owns this rule
    assert env["mlx"] == 0
