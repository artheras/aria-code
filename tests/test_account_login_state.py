from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest
from aria_code.apps.cli.commands.auth_cmds import AuthCommandsMixin
from test_account_memory_sync import token, config


def handler():
    h = AuthCommandsMixin()
    h.terminal = SimpleNamespace(
        config=config("a"),
        api_url="https://api.arthera.xyz",
        conversation=[{"role": "user", "content": "private A"}],
        _last_response="private A",
    )
    h.context = SimpleNamespace(save_config=Mock(), console=Mock(), has_rich=False)
    return h


def test_switching_accounts_replaces_email_provider_and_clears_old_refresh_and_history():
    h = handler()
    h.terminal.config["refresh_token"] = "refresh-a"
    h._accept_session({"auth_token": token("b"), "user_id": "b", "email": "wrong@example.com", "provider": "firebase"})
    assert h.terminal.config["user_id"] == "b"
    assert h.terminal.config["email"] == "b@example.com"
    assert "refresh_token" not in h.terminal.config
    assert h.terminal.conversation == [] and h.terminal._last_response == ""


def test_logout_clears_all_account_information_but_preserves_personal_memory_files():
    h = handler()
    h._accept_session({"auth_token": token("a"), "user_id": "a", "refresh_token": "refresh-a"})
    h.terminal.memory_mgr.append("profile", "Only A prefers compact answers")
    old_root = h.terminal.memory_mgr.root
    h.cmd_logout("")
    assert old_root.exists()
    assert not h.terminal.config["auth_token"] and "email" not in h.terminal.config
    assert "refresh_token" not in h.terminal.config
    assert "Only A" not in h.terminal.memory_mgr.load_context()


def test_invalid_uid_is_rejected_before_touching_existing_session():
    h = handler()
    previous = dict(h.terminal.config)
    with pytest.raises(ValueError):
        h._accept_session({"auth_token": token("b"), "user_id": "a"})
    assert h.terminal.config == previous


@pytest.mark.asyncio
async def test_default_login_uses_the_account_page_not_the_google_only_flow(monkeypatch):
    h = handler()
    login = Mock(return_value={"auth_token": token("b"), "user_id": "b", "provider": "firebase"})
    google = Mock(side_effect=AssertionError("must not use Google-only login"))
    monkeypatch.setattr("aria_code.apps.cli.web_login.run_web_login", login)
    monkeypatch.setattr("aria_code.apps.cli.google_login.run_google_login", google)
    await h._login_with_google()
    assert login.call_count == 1 and h.terminal.config["user_id"] == "b"
