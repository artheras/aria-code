"""The real manager/HTTP client, with two fake server accounts and no network."""

import base64
import json
import time
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aria_code.account_memory import AccountMemory
from aria_code.account_session import account_root, get_account_token, profile_path, PROJECT_ID
from aria_code.memory_manager import MemoryManager


def token(uid, *, exp=None, email=None):
    claims = {
        "aud": PROJECT_ID,
        "iss": f"https://securetoken.google.com/{PROJECT_ID}",
        "sub": uid,
        "exp": exp or time.time() + 3600,
        "email": email or uid + "@example.com",
    }
    return "header." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".fixture"


def config(uid):
    return {
        "user_id": uid,
        "auth_token": token(uid),
        "api_url": "https://api.example.com",
        "email": uid + "@example.com",
    }


class Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return json.dumps(self.body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    state = {"a": [], "b": []}
    enabled = {"a": True, "b": True}
    calls = []

    def request(req, timeout=8):
        from aria_code.account_session import claims_for

        uid = claims_for(req.get_header("Authorization").split(" ")[1])["sub"]
        calls.append((uid, req.method, req.full_url))
        if req.full_url.endswith("/preferences"):
            return Response({"enabled": enabled[uid], "nickname": uid + " nickname"})
        if req.full_url.endswith("/summary"):
            return Response({"facts": list(state[uid])})
        if req.method == "POST":
            item = {**json.loads(req.data), "id": f"{uid}-{len(state[uid]) + 1}"}
            state[uid].append(item)
            return Response(item)
        if req.method == "DELETE":
            state[uid][:] = [f for f in state[uid] if not req.full_url.endswith("/" + f["id"])]
            return Response({})
        raise AssertionError(req.full_url)

    monkeypatch.setattr("urllib.request.urlopen", request)
    return SimpleNamespace(state=state, enabled=enabled, calls=calls, request=request)


def test_web_facts_and_preferences_enter_code_and_code_facts_reach_same_account(server):
    cfg = config("a")
    memory = AccountMemory(cfg)
    server.state["a"] = [{"id": "web-a", "content": "Prefer brief Chinese answers"}]
    memory.add("Prefer concise code reviews")
    assert memory.sync(force=True)["ok"]
    assert "Prefer brief Chinese answers" in memory.context()
    assert "a nickname" in memory.context()
    assert any(f["content"] == "Prefer concise code reviews" for f in server.state["a"])
    assert all(call[0] == "a" for call in server.calls)


def test_switching_a_b_a_and_signout_never_mix_local_or_cloud_facts(server):
    cfg = config("a")
    manager = MemoryManager(config=cfg)
    manager.append("profile", "Only A likes quiet interfaces")
    assert "Only A" in manager.load_context()
    old_root = manager.root
    cfg.clear()
    cfg.update(config("b"))
    assert manager.root != old_root
    assert "Only A" not in manager.load_context()
    manager.append("profile", "Only B likes detailed answers")
    manager.account.sync(force=True)
    assert all("Only A" not in f["content"] for f in server.state["b"])
    cfg.clear()
    cfg.update(config("a"))
    assert "Only A" in manager.load_context() and "Only B" not in manager.load_context()
    cfg.clear()
    assert not manager.load_context()


def test_late_old_response_is_discarded_when_user_switches(server, monkeypatch):
    cfg = config("a")
    memory = AccountMemory(cfg)

    def switching(req, timeout=8):
        result = server.request(req, timeout)
        if req.full_url.endswith("/summary"):
            cfg.clear()
            cfg.update(config("b"))
        return result

    monkeypatch.setattr("urllib.request.urlopen", switching)
    memory.add("Private A note")
    assert memory.sync(force=True)["reason"] == "account-changed"
    assert not memory.facts()
    assert server.state["b"] == []
    assert not any(call[1] == "POST" for call in server.calls)


def test_network_failure_retains_pending_and_retry_does_not_duplicate(server):
    cfg = config("a")
    memory = AccountMemory(cfg)
    memory.add("Prefer restrained motion")
    with patch("urllib.request.urlopen", side_effect=OSError("offline")):
        assert not memory.sync(force=True)["ok"]
    assert memory.status()["pending"] == 1
    assert "restrained motion" in memory.context()
    assert memory.sync(force=True)["ok"]
    memory.sync(force=True)
    assert len(server.state["a"]) == 1 and memory.status()["pending"] == 0


def test_lost_post_response_is_recovered_by_client_id_without_reposting(server, monkeypatch):
    memory = AccountMemory(config("a"))
    memory.add("Prefer keyboard shortcuts")

    def lost(req, timeout=8):
        response = server.request(req, timeout)
        if req.method == "POST":
            raise OSError("response lost after commit")
        return response

    monkeypatch.setattr("urllib.request.urlopen", lost)
    assert not memory.sync(force=True)["ok"]
    monkeypatch.setattr("urllib.request.urlopen", server.request)
    assert memory.sync(force=True)["ok"]
    assert len(server.state["a"]) == 1
    assert len([c for c in server.calls if c[1] == "POST"]) == 1


def test_web_deletion_is_not_resurrected_from_local_files(server):
    manager = MemoryManager(config=config("a"))
    manager.append("profile", "Prefer compact menus")
    manager.account.sync(force=True)
    assert "compact menus" in manager.load_context()
    server.state["a"].clear()  # Web deletes the same user's fact
    manager.account.sync(force=True)
    assert "compact menus" not in manager.load_context()
    assert manager.account.status()["pending"] == 0


def test_memory_switch_from_web_stops_injection_and_upload(server):
    memory = AccountMemory(config("a"))
    memory.add("Prefer simple layouts")
    server.enabled["a"] = False
    result = memory.sync(force=True)
    assert result["ok"] and result["enabled"] is False
    assert not memory.context() and server.state["a"] == []
    assert memory.status()["pending"] == 1
    assert not memory.add("Do not save this")


def test_forget_updates_cloud_and_other_devices_on_next_read(server):
    memory = AccountMemory(config("a"))
    memory.add("Forget this note")
    memory.sync(force=True)
    item = memory.facts()[0]["id"]
    memory.forget(item)
    assert not memory.facts()
    assert memory.sync(force=True)["ok"]
    assert not server.state["a"]


def test_legacy_anonymous_files_are_never_adopted_or_uploaded(tmp_path, monkeypatch, server):
    monkeypatch.setenv("ARIA_HOME", str(tmp_path))
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "user_profile.md").write_text("Old private profile")
    (tmp_path / "ARIA.md").write_text("Old unowned account profile")
    manager = MemoryManager(config=config("b"))
    manager.account.sync(force=True)
    assert "Old private profile" not in manager.load_context()
    assert not server.state["b"]
    assert (tmp_path / "ARIA.md").read_text() == "Old unowned account profile"


def test_uid_path_is_safe_and_email_change_keeps_same_memory(server):
    cfg = config("a")
    memory = AccountMemory(cfg)
    memory.add("Prefer direct answers")
    root = account_root(cfg)
    cfg["email"] = "new@example.com"
    cfg["auth_token"] = token("a", email="new@example.com")
    assert account_root(cfg) == root
    assert "direct answers" in memory.context()
    cfg["user_id"] = "../unsafe"
    cfg["auth_token"] = token("../unsafe")
    assert account_root(cfg).parent.name == "accounts"
    assert ".." not in account_root(cfg).name


def test_mismatched_token_uid_cannot_read_or_write_memory(server):
    cfg = config("a")
    cfg["auth_token"] = token("b")
    memory = AccountMemory(cfg)
    assert not memory.add("Private A")
    assert not memory.sync(force=True)["ok"]
    assert not server.calls


def test_expired_token_refresh_preserves_uid_and_rotates_refresh_token(monkeypatch):
    cfg = config("a")
    cfg["auth_token"] = token("a", exp=time.time() - 60)
    cfg["refresh_token"] = "old-refresh"
    fresh = token("a", email="new@example.com")
    saved = []

    def refresh(req, timeout=8):
        assert req.full_url.startswith("https://securetoken.googleapis.com/v1/token?")
        assert b"grant_type=refresh_token" in req.data
        return Response({"id_token": fresh, "refresh_token": "rotated", "user_id": "a"})

    monkeypatch.setattr("urllib.request.urlopen", refresh)
    assert get_account_token(cfg, lambda value: saved.append(dict(value))) == fresh
    assert cfg["user_id"] == "a" and cfg["email"] == "new@example.com"
    assert saved[0]["refresh_token"] == "rotated"


def test_late_refresh_cannot_overwrite_new_account(monkeypatch):
    cfg = config("a")
    cfg["auth_token"] = token("a", exp=time.time() - 60)
    cfg["refresh_token"] = "old"

    def refresh(req, timeout=8):
        cfg.clear()
        cfg.update(config("b"))
        return Response({"id_token": token("a"), "refresh_token": "rotated", "user_id": "a"})

    monkeypatch.setattr("urllib.request.urlopen", refresh)
    saved = []
    assert get_account_token(cfg, lambda value: saved.append(value)) is None
    assert cfg["user_id"] == "b" and not saved


def test_disable_takes_effect_even_when_the_summary_request_fails(server, monkeypatch):
    memory = AccountMemory(config("a"))
    memory.add("Prefer compact cards")
    memory.sync(force=True)
    server.enabled["a"] = False

    def broken_summary(req, timeout=8):
        if req.full_url.endswith("/summary"):
            raise OSError("offline")
        return server.request(req, timeout)

    monkeypatch.setattr("urllib.request.urlopen", broken_summary)
    assert not memory.sync(force=True)["ok"]
    assert not memory.context()


def test_explicit_forget_still_syncs_while_memory_is_disabled(server):
    memory = AccountMemory(config("a"))
    memory.add("Forget while disabled")
    memory.sync(force=True)
    item = memory.facts()[0]["id"]
    memory.forget(item)
    server.enabled["a"] = False
    assert memory.sync(force=True)["ok"]
    assert not server.state["a"]


def test_deleted_idempotent_response_is_acknowledged_without_readding_the_fact(server, monkeypatch):
    memory = AccountMemory(config("a"))
    memory.add("Deleted in another client")

    def tombstone(req, timeout=8):
        if req.method == "POST":
            return Response({"id": "deleted-1", "content": "Deleted in another client", "deleted_at": "now"})
        return server.request(req, timeout)

    monkeypatch.setattr("urllib.request.urlopen", tombstone)
    assert memory.sync(force=True)["ok"]
    assert memory.status()["pending"] == 0
    assert "Deleted in another client" not in memory.context()


def test_memory_commands_use_the_current_account_and_report_pending_sync(server, capsys):
    from aria_code.apps.cli.commands.workspace_cmds import WorkspaceCommandsMixin

    command = WorkspaceCommandsMixin()
    command.terminal = SimpleNamespace(config=config("a"))
    command.context = SimpleNamespace(save_config=lambda cfg: None)
    command.cmd_memory("account add Prefer calm animations")
    assert "待同步" in capsys.readouterr().out
    command.cmd_memory("status")
    assert "待同步：1" in capsys.readouterr().out
    command.cmd_memory("sync")
    assert "已同步" in capsys.readouterr().out
    command.cmd_memory("account")
    assert "Prefer calm animations" in capsys.readouterr().out
    assert server.state["a"][0]["content"] == "Prefer calm animations"


def test_two_cli_processes_keep_both_pending_queues(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    monkeypatch.setenv("ARIA_HOME", str(tmp_path))
    cfg = config("a")
    script = """import json,sys
from aria_code.account_memory import AccountMemory
m=AccountMemory(json.loads(sys.argv[1]))
for i in range(20):m.add(sys.argv[2]+str(i))
"""
    env = {**os.environ, "ARIA_HOME": str(tmp_path)}
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", script, json.dumps(cfg), prefix],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for prefix in ["one-", "two-"]
    ]
    for worker in workers:
        _out, err = worker.communicate(timeout=15)
        assert worker.returncode == 0, err.decode()
    memory = AccountMemory(cfg)
    assert memory.status()["pending"] == 40
    assert len(memory.facts()) == 40
    path = account_root(cfg) / "cloud-memory.json"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_forgetting_during_upload_does_not_reappear_when_the_upload_finishes(server, monkeypatch):
    memory = AccountMemory(config("a"))
    memory.add("Cancel this pending fact")
    item_id = memory.facts()[0]["id"]

    def interrupted(req, timeout=8):
        if req.method == "POST":
            memory.forget(item_id)
        return server.request(req, timeout)

    monkeypatch.setattr("urllib.request.urlopen", interrupted)
    assert memory.sync(force=True)["ok"]
    assert "Cancel this pending fact" not in memory.context()
    assert memory.status()["pending"] == 1
    monkeypatch.setattr("urllib.request.urlopen", server.request)
    assert memory.sync(force=True)["ok"]
    assert not server.state["a"]
