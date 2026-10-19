"""Background self-update: native only, opt-out, no retry storms, no double installs."""
from pathlib import Path

from aria_code.apps.cli import auto_update as au


def run(tmp_path, *, channel="native", config=None, latest="0.2.0", current="0.1.0", install=None, now=1000.0):
    calls = []

    def fake_install(version, release):
        calls.append(version)
        if install:
            install()
        return Path("x")

    result = au.install_if_newer(
        current, channel, config or {}, check=lambda *_a, **_k: (latest, {}),
        install=fake_install, newer=lambda a, b: a > b, app_root=tmp_path, now=now,
    )
    return result, calls


def test_installs_a_newer_native_release(tmp_path):
    assert run(tmp_path) == ("0.2.0", ["0.2.0"])
    assert not (tmp_path / "auto-update.lock").exists()


def test_up_to_date_does_nothing(tmp_path):
    assert run(tmp_path, latest="0.1.0") == (None, [])


def test_other_channels_and_opt_out_never_install(tmp_path, monkeypatch):
    assert run(tmp_path, channel="pip") == (None, [])
    assert run(tmp_path, config={"auto_update": False}) == (None, [])
    monkeypatch.setenv("ARIA_AUTO_UPDATE", "0")
    assert run(tmp_path) == (None, [])


def test_failure_keeps_version_and_is_not_retried_for_a_day(tmp_path):
    def boom():
        raise RuntimeError("checksum")

    assert run(tmp_path, install=boom, now=1000.0) == (None, ["0.2.0"])
    assert run(tmp_path, now=2000.0) == (None, [])  # too soon
    assert run(tmp_path, now=1000.0 + au.RETRY_AFTER_FAILURE_S + 1)[0] == "0.2.0"


def test_a_second_session_does_not_install_at_the_same_time(tmp_path):
    (tmp_path / "auto-update.lock").write_text("")
    import os
    os.utime(tmp_path / "auto-update.lock", (1000.0, 1000.0))
    assert run(tmp_path, now=1010.0) == (None, [])
    # A lock left by a crashed session expires.
    assert run(tmp_path, now=1000.0 + au.LOCK_STALE_S + 5)[0] == "0.2.0"
