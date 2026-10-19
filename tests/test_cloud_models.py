"""Cloud model lists: parsed per provider, cached, stale-while-revalidate, offline-safe."""
import json
import os

import pytest

from aria_code.apps.cli import cloud_models as cm


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "cache_dir", lambda: tmp_path / "cloud_models")
    monkeypatch.delenv("ARIA_OFFLINE", raising=False)
    cm._refreshing.clear()


GOOGLE = {"models": [
    {"name": "models/gemini-3.8-flash", "displayName": "Gemini 3.8 Flash", "inputTokenLimit": 1048576,
     "supportedGenerationMethods": ["generateContent", "countTokens"]},
    {"name": "models/gemini-4-argon", "displayName": "Gemini 4 Argon", "inputTokenLimit": 1048576,
     "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/text-embedding-005", "supportedGenerationMethods": ["embedContent"]},
    {"name": "models/gemini-3.8-live", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.8-flash-tts", "supportedGenerationMethods": ["generateContent"]},
]}


def test_google_keeps_chat_models_newest_first_with_context():
    rows = cm.parse("google", GOOGLE)
    assert [r["id"] for r in rows] == ["gemini-4-argon", "gemini-3.8-flash"]
    assert rows[0]["detail"] == "ctx 1M" and rows[0]["name"] == "Gemini 4 Argon"


def test_openai_style_lists_drop_non_chat_models_and_sort_naturally():
    payload = {"data": [{"id": "gpt-4o"}, {"id": "text-embedding-3-small"}, {"id": "whisper-1"},
                        {"id": "gpt-5"}, {"id": "gpt-10"}, {"id": "dall-e-3"}]}
    ids = [r["id"] for r in cm.parse("openai", payload)]
    assert "whisper-1" not in ids and "dall-e-3" not in ids and "text-embedding-3-small" not in ids
    # Newest first, compared as numbers: 10 is above 5, which is above 4o.
    assert ids == ["gpt-10", "gpt-5", "gpt-4o"]


def test_together_returns_a_bare_list_and_anthropic_uses_display_names():
    assert [r["id"] for r in cm.parse("together", [{"id": "meta/llama-3"}])] == ["meta/llama-3"]
    rows = cm.parse("anthropic", {"data": [{"id": "claude-x", "display_name": "Claude X"}]})
    assert rows == [{"id": "claude-x", "name": "Claude X", "detail": ""}]


def test_live_request_is_cached_and_reused_until_it_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(cm, "fetch", lambda provider, key, timeout=0: calls.append(provider) or [{"id": "m", "name": "m", "detail": ""}])
    assert cm.models_for("openai", "k", now=1000.0)[0]["id"] == "m"
    assert cm.models_for("openai", "k", now=1000.0 + 60) and calls == ["openai"]


def test_stale_cache_is_served_at_once_and_refreshed_behind(monkeypatch):
    cm._write_cache("openai", [{"id": "old", "name": "old", "detail": ""}], 0.0)
    started = []
    monkeypatch.setattr(cm, "_refresh_later", lambda provider, key: started.append(provider))
    got = cm.models_for("openai", "k", now=cm.CACHE_TTL_S + 5)
    assert got[0]["id"] == "old" and started == ["openai"]


def test_offline_uses_any_cache_and_never_asks(monkeypatch):
    monkeypatch.setattr(cm, "fetch", lambda *a, **k: pytest.fail("offline must not make a request"))
    assert cm.models_for("openai", "k", offline=True) == []
    cm._write_cache("openai", [{"id": "old", "name": "old", "detail": ""}], 0.0)
    assert cm.models_for("openai", "k", offline=True)[0]["id"] == "old"


def test_failed_request_leaves_the_provider_out_and_the_key_is_never_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "fetch", lambda *a, **k: None)
    assert cm.collect(lambda p: "secret" if p == "openai" else "") == {}
    monkeypatch.setattr(cm, "fetch", lambda *a, **k: [{"id": "m", "name": "m", "detail": ""}])
    out = cm.collect(lambda p: "sk-very-secret" if p == "groq" else "")
    assert list(out) == ["groq"]
    cached = (tmp_path / "cloud_models" / "groq.json").read_text()
    assert "sk-very-secret" not in cached and json.loads(cached)["models"][0]["id"] == "m"
