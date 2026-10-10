"""Native state commands must work without Python and preserve Python's data."""
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("ARIA_NATIVE_BINARY"), reason="build the opt-in Rust CLI first")


@pytest.fixture
def state(tmp_path):
    binary = os.environ["ARIA_NATIVE_BINARY"]
    root = tmp_path / "state"
    root.mkdir()
    env = {**os.environ, "ARIA_HOME": str(root), "ARIA_USER_OUTPUT_ROOT": str(tmp_path / "outputs")}

    def call(*args, **kwargs):
        return subprocess.run([binary, "--python", "nonexistent-python", *args], capture_output=True,
                              env=env, cwd=tmp_path, timeout=5, **kwargs)
    return root, env, call


def payload(result):
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return json.loads(result.stdout)


def test_native_paths_match_python_and_do_not_create_state(state):
    from aria_code.apps.cli.config_paths import config_snapshot

    root, env, call = state
    with pytest.MonkeyPatch.context() as patch:
        for key in ("ARIA_HOME", "ARIA_USER_OUTPUT_ROOT"):
            patch.setenv(key, env[key])
        assert payload(call("config", "paths")) == config_snapshot()
    assert payload(call("config", "show"))["settings"] == {}
    assert not list(root.iterdir())


def test_native_home_precedence_and_tilde(state, tmp_path):
    root, env, call = state
    env.pop("ARIA_HOME")
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    assert Path(payload(call("config", "paths"))["config_dir"]) == tmp_path / ".aria-code"
    legacy = tmp_path / ".arthera"
    legacy.mkdir()
    assert Path(payload(call("config", "paths"))["config_dir"]) == legacy
    env["ARIA_HOME"] = "~/custom"
    assert Path(payload(call("config", "paths"))["config_dir"]) == tmp_path / "custom"
    env["ARIA_HOME"] = str(root)
    assert Path(payload(call("config", "paths"))["config_dir"]) == root


def test_native_config_positive_list_never_prints_credentials(state):
    root, _, call = state
    config = {"model": "google/gemini-3.5-flash", "local_provider": "google", "ui_lang": "zh",
              "auth_token": "DO_NOT_PRINT", "refresh_token": "DO_NOT_PRINT",
              "providers": {"api_key": "DO_NOT_PRINT"}, "new_credential": "DO_NOT_PRINT",
              "hooks": {"command": "DO_NOT_PRINT"}, "local_mode": False}
    path = root / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    before = path.read_bytes()
    result = call("config", "show")
    data = payload(result)
    assert data["settings"] == {k: config[k] for k in ("model", "local_provider", "ui_lang", "local_mode")}
    assert data["effective"] is False
    assert b"DO_NOT_PRINT" not in result.stdout + result.stderr
    assert path.read_bytes() == before
    config["model"] = {"secret": "DO_NOT_PRINT"}
    path.write_text(json.dumps(config), encoding="utf-8")
    assert "model" not in payload(call("config", "show"))["settings"]


def test_native_sessions_match_python_and_search_text_blocks(state):
    from aria_code.apps.cli.session_store import SessionManager
    from aria_code.apps.cli.session_jsonl import JsonlSessionStore

    root, _, call = state
    snapshots = SessionManager(root / "sessions")
    snapshots.save_session("abc", [{"role": "user", "content": "你好 Rust"},
                                  {"role": "assistant", "content": [{"text": "你好 世界"}]}],
                           {"title": "中文项目"})
    jsonl = JsonlSessionStore(root / "sessions")
    jsonl.init_session("abc", "duplicate")
    jsonl.append_message("abc", "user", "not the stable snapshot")
    jsonl.init_session("xyz", "history only")
    jsonl.append_message("xyz", "user", "hello")
    jsonl.flush_meta("xyz", "latest title")
    files_before = {p.name: p.read_bytes() for p in (root / "sessions").iterdir()}
    assert payload(call("sessions", "show", "abc")) == snapshots.load_session("abc")
    shown = payload(call("sessions", "show", "xyz"))
    reference = jsonl.load_session("xyz")
    assert shown["messages"] == reference["messages"]
    assert shown["metadata"]["title"] == reference["metadata"]["title"]
    listed = payload(call("sessions", "list"))["sessions"]
    assert {r["id"]: r["resumable"] for r in listed} == {"abc": True, "xyz": False}
    found = payload(call("sessions", "search", "你好", "--limit", "1"))["sessions"]
    assert found[0]["id"] == "abc" and found[0]["match_count"] == 2
    assert len(payload(call("sessions", "list", "--limit", "1"))["sessions"]) == 1
    assert files_before == {p.name: p.read_bytes() for p in (root / "sessions").iterdir()}


def test_native_partial_jsonl_and_corrupt_snapshots(state):
    root, _, call = state
    sessions = root / "sessions"
    sessions.mkdir()
    (sessions / "good.jsonl").write_text('{"type":"meta","id":"good"}\n{"type":"message","content":"完整"}\n{partial', encoding="utf-8")
    (sessions / "broken.json").write_text("{broken", encoding="utf-8")
    assert payload(call("sessions", "show", "good"))["messages"][0]["content"] == "完整"
    data = payload(call("sessions", "list"))
    assert [r["id"] for r in data["sessions"]] == ["good"]
    assert data["skipped"] == 1
    assert call("sessions", "show", "broken").returncode == 2
    assert call("resume", "good").returncode == 2


def test_native_unicode_search_preview_includes_the_match(state):
    root, _, call = state
    sessions = root / "sessions"
    sessions.mkdir()
    text = "很长的上下文" * 80 + "İstanbul 你好" + "继续处理" * 30
    data = {"id": "abc", "messages": [{"role": "user", "content": text}], "metadata": {}}
    (sessions / "abc.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = payload(call("sessions", "search", "你好"))["sessions"][0]
    assert "你好" in result["preview"]
    assert len(result["preview"]) <= 120


def test_native_snapshot_identity_must_match_the_file_before_resume(state):
    root, _, call = state
    sessions = root / "sessions"
    sessions.mkdir()
    for data in [{"id": "../config", "messages": []}, {"messages": []}, {"id": "abc", "messages": {}}]:
        (sessions / "abc.json").write_text(json.dumps(data), encoding="utf-8")
        assert call("sessions", "show", "abc").returncode == 2
        result = call("resume", "abc")
        assert result.returncode == 2 and b"Python runtime" not in result.stderr


@pytest.mark.parametrize("args", [("sessions", "show", "../config"), ("resume", "/tmp/x"),
                                  ("sessions", "list", "--limit", "0"), ("sessions", "list", "--limit", "1001"),
                                  ("sessions", "search", " "), ("update", "check"),
                                  ("update", "check", "--current", "1.2.3-beta"),
                                  ("update", "check", "--current", "0.126.0", "--channel", "other"),
                                  ("update", "check", "--current", "0.126.0", "--offline", "--refresh")])
def test_native_bad_inputs_never_start_python(state, args):
    _, _, call = state
    result = call(*args)
    assert result.returncode == 2
    assert not result.stdout
    assert b"Python runtime" not in result.stderr


def test_native_cache_channel_ttl_and_legacy_versions(state):
    root, _, call = state
    source = "https://api.github.com/repos/artheras/aria-code/releases/latest"
    cache = root / "native_update_check-native.json"
    cached = {"schema": 1, "source": source, "checked_at": time.time() - 1, "latest": "4.4.2"}
    cache.write_text(json.dumps(cached), encoding="utf-8")
    data = payload(call("update", "check", "--current", "0.126.0", "--offline"))
    assert data["update_available"] is False and data["status"] == "cached"
    cached["latest"] = "v0.127.0"
    cache.write_text(json.dumps(cached), encoding="utf-8")
    data = payload(call("update", "check", "--current", "4.4.2", "--offline"))
    assert data["update_available"] is True
    assert payload(call("update", "check", "--current", "0.126.0", "--channel", "npm", "--offline"))["latest"] is None
    cached["checked_at"] = time.time() - 90_000
    cache.write_text(json.dumps(cached), encoding="utf-8")
    assert payload(call("update", "check", "--current", "0.126.0", "--offline"))["status"] == "stale"
    cached["checked_at"] = time.time() + 90_000
    cache.write_text(json.dumps(cached), encoding="utf-8")
    assert payload(call("update", "check", "--current", "0.126.0", "--offline"))["status"] == "unknown"


def test_native_missing_offline_cache_is_unknown_and_does_not_create_files(state):
    root, _, call = state
    data = payload(call("update", "check", "--current", "0.126.0", "--offline"))
    assert data["update_available"] is None and data["status"] == "unknown"
    assert not list(root.iterdir())


def test_native_resume_forwards_validated_snapshot_without_rewriting(state, tmp_path):
    import sys

    root, env, _ = state
    snapshots = root / "sessions"
    snapshots.mkdir()
    path = snapshots / "abc.json"
    path.write_text('{"id":"abc","messages":[{"role":"user","content":"你好"}],"metadata":{}}', encoding="utf-8")
    before = path.read_bytes()
    package = tmp_path / "fake" / "aria_code" / "apps" / "cli"
    package.mkdir(parents=True)
    for directory in [package, package.parent, package.parent.parent]:
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (package / "main.py").write_text(
        "import json, os, sys\ndef main():\n"
        "    print(json.dumps({'args':sys.argv[1:], 'cwd':os.getcwd(), 'home':os.environ['ARIA_HOME'], 'input':sys.stdin.read()}))\n"
        "    sys.exit(17)\n", encoding="utf-8")
    env["PYTHONPATH"] = str(tmp_path / "fake")
    env["PYTHONIOENCODING"] = "cp1252"  # host locale must not corrupt UTF-8 pipes
    env["ARIA_HOME"] = "state"  # relative state must survive -C
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binary = os.environ["ARIA_NATIVE_BINARY"]
    result = subprocess.run([binary, "--python", sys.executable, "-C", str(workspace), "resume", "abc", "--", "--model", "google/gemini", "你好; $(false)"],
                            input="你好\n".encode(), env=env, cwd=tmp_path, capture_output=True, timeout=10)
    assert result.returncode == 17, result.stderr
    data = json.loads(result.stdout)
    assert data["args"] == ["--session", "abc", "--model", "google/gemini", "你好; $(false)"]
    assert Path(data["cwd"]).resolve() == workspace.resolve()
    assert Path(data["home"]).resolve() == root.resolve()
    assert data["input"] == "你好\n" and path.read_bytes() == before
    conflict = subprocess.run([binary, "resume", "abc", "--session=../secret"], env=env, cwd=tmp_path, capture_output=True, timeout=5)
    assert conflict.returncode == 2 and not conflict.stdout


def test_native_symlinked_history_is_not_followed(state, tmp_path):
    root, _, call = state
    sessions = root / "sessions"
    sessions.mkdir()
    outside = tmp_path / "secret.json"
    outside.write_text('{"id":"link","messages":[]}', encoding="utf-8")
    try:
        (sessions / "link.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    assert call("sessions", "show", "link").returncode == 2
    assert payload(call("sessions", "list"))["sessions"] == []
