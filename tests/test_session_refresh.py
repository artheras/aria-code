"""A Firebase sign-in is renewed before it expires, the way Claude Code and Codex do it."""
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from aria_code.apps.cli import session_refresh as sr

NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(delta_seconds):
    return (NOW + timedelta(seconds=delta_seconds)).isoformat().replace("+00:00", "Z")


def _write(path, **fields):
    base = {"auth_token": "old", "refresh_token": "r1", "token_expires_at": _stamp(3600), "model": "qwen3:8b", "api_url": "x"}
    base.update(fields)
    path.write_text(json.dumps(base))
    return path


def _exchange(result):
    calls = []

    def exchange(refresh_token, api_key):
        calls.append((refresh_token, api_key))
        return result

    exchange.calls = calls
    return exchange


def test_a_token_with_plenty_of_time_left_is_used_as_is(tmp_path):
    p = _write(tmp_path / "config.json", token_expires_at=_stamp(1800))
    ex = _exchange({"id_token": "new"})
    assert sr.refresh_config_file(p, now=NOW, exchange=ex) == "old"
    assert ex.calls == []


@pytest.mark.parametrize("left", [200, 0, -3600])
def test_a_token_about_to_expire_or_expired_is_renewed_and_written_back(tmp_path, left):
    p = _write(tmp_path / "config.json", token_expires_at=_stamp(left))
    ex = _exchange({"id_token": "new", "refresh_token": "r2", "expires_in": "3600"})
    assert sr.refresh_config_file(p, now=NOW, exchange=ex) == "new"
    assert ex.calls[0][0] == "r1"
    saved = json.loads(p.read_text())
    assert saved["auth_token"] == "new" and saved["refresh_token"] == "r2"
    assert saved["token_expires_at"] == _stamp(3600)
    # The rest of the user's configuration is untouched.
    assert saved["model"] == "qwen3:8b" and saved["api_url"] == "x"
    if os.name == "posix":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert not (tmp_path / "config.json.refresh").exists()


def test_a_failed_refresh_returns_the_stored_token_and_changes_nothing(tmp_path):
    p = _write(tmp_path / "config.json", token_expires_at=_stamp(-10))
    before = p.read_text()

    def boom(*_):
        raise OSError("offline")

    assert sr.refresh_config_file(p, now=NOW, exchange=boom) == "old"
    assert p.read_text() == before


def test_a_response_without_a_token_is_not_stored(tmp_path):
    p = _write(tmp_path / "config.json", token_expires_at=_stamp(-10))
    before = p.read_text()
    assert sr.refresh_config_file(p, now=NOW, exchange=_exchange({"error": "TOKEN_EXPIRED"})) == "old"
    assert p.read_text() == before


def test_signed_out_missing_refresh_token_and_unknown_expiry_are_left_alone(tmp_path):
    ex = _exchange({"id_token": "new"})
    assert sr.refresh_config_file(tmp_path / "missing.json", now=NOW, exchange=ex) is None
    p = _write(tmp_path / "a.json", auth_token=None)
    assert sr.refresh_config_file(p, now=NOW, exchange=ex) is None
    p = _write(tmp_path / "b.json", refresh_token=None, token_expires_at=_stamp(-10))
    assert sr.refresh_config_file(p, now=NOW, exchange=ex) == "old"
    p = _write(tmp_path / "c.json", token_expires_at=None)
    assert sr.refresh_config_file(p, now=NOW, exchange=ex) == "old"
    assert ex.calls == []


def test_the_gateway_provider_gets_the_renewed_token(tmp_path, monkeypatch):
    from aria_code.providers.llm import arthera_gateway

    monkeypatch.setenv("ARIA_HOME", str(tmp_path))
    expired = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    _write(tmp_path / "config.json", token_expires_at=expired)
    monkeypatch.setattr(sr, "exchange_refresh_token", lambda *_a, **_k: {"id_token": "fresh", "expires_in": 3600})
    assert arthera_gateway.session_token() == "fresh"
