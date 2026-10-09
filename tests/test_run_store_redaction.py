"""Credentials never reach the persisted run history, in any field shape.

Ported from the intent of artheras/aria-code#11, whose branch had diverged
from main: key matching ignored camelCase (apiKey), and prompts, errors and
free-text event values were written as they came.
"""

import sqlite3

import pytest

from aria_code.runtime.checkpoints import CheckpointStore
from aria_code.runtime.run_store import RunStore, _redact, _redact_text


@pytest.mark.parametrize("key", ["api_key", "apiKey", "API-KEY", "accessToken", "clientSecret",
                                 "refresh_token", "Authorization", "privateKey", "passwd"])
def test_secret_fields_are_redacted_whatever_their_spelling(key):
    assert _redact({"outer": [{key: "value-123"}]}) == {"outer": [{key: "[REDACTED]"}]}


@pytest.mark.parametrize("text, leaked", [
    ("curl -H 'Authorization: Bearer abc.def-123'", "abc.def-123"),
    ("login with password=hunter2 then", "hunter2"),
    ('api_key: "AKIA-1234567"', "AKIA-1234567"),
    ("token=ghp_abcdefghijklmnop1234", "ghp_abcdefghijklmnop1234"),
    ("use sk-live-0123456789abcdef", "sk-live-0123456789abcdef"),
    ("clone https://alice:s3cret@github.com/x/y", "s3cret"),
    ("gcloud gave ya29.a0Abc-def_123", "ya29.a0Abc-def_123"),
    ("key AIzaSyA0123456789abcdefghijk", "AIzaSyA0123456789abcdefghijk"),
])
def test_credentials_in_text_are_redacted(text, leaked):
    redacted = _redact_text(text)
    assert leaked not in redacted and "[REDACTED]" in redacted


@pytest.mark.parametrize("text", [
    "The token count was 1200 and the secret santa list is long.",
    "Refactor the password reset flow in auth.py",
    "https://github.com/artheras/aria-code/pull/11",
])
def test_ordinary_text_is_left_alone(text):
    assert _redact_text(text) == text


def test_prompts_errors_and_events_are_stored_redacted(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    run = store.create_run(session_id="s", workspace=str(tmp_path),
                           prompt="deploy with password=hunter2 please")
    store.append_event(run.run_id, "tool_call", {"params": {"command": "curl -H 'Authorization: Bearer abc123'",
                                                            "apiKey": "k-1"}})
    store.transition(run.run_id, "running")
    store.transition(run.run_id, "failed", error="401 for token=ghp_abcdefghijklmnop1234")

    with sqlite3.connect(tmp_path / "runs.sqlite3") as db:
        dump = "\n".join(str(row) for table in ("runs", "run_events")
                         for row in db.execute(f"SELECT * FROM {table}"))
    for secret in ("hunter2", "abc123", "k-1", "ghp_abcdefghijklmnop1234"):
        assert secret not in dump
    assert "deploy with password=[REDACTED] please" in dump


def test_checkpoints_keep_file_contents_exactly(tmp_path):
    # A rewind writes these bytes back; masking them would corrupt the file.
    source = 'PASSWORD = "hunter2"\nheaders = {"Authorization": "Bearer abc123"}\n'
    target = tmp_path / "settings.py"
    target.write_text(source)
    store = CheckpointStore(tmp_path / "runs.sqlite3")
    record = store.record_change(path=target, before_content="", after_content=source,
                                 existed_before=False, source="test", session_id="s")
    assert store.get(record.checkpoint_id).files[0].after_content == source
