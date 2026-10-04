import hashlib
import io
import json
from pathlib import Path
import tarfile
import time
from types import SimpleNamespace

import pytest

from aria_code.apps.cli import update_check, updater


def test_startup_uses_cached_notice_without_waiting_for_network(monkeypatch, tmp_path):
    monkeypatch.setattr(update_check, "_CACHE_FILE", tmp_path / "update.json")
    update_check._write_cache({"source": update_check._RELEASE_URL, "latest": "v0.75.0", "checked_at": time.time()})
    monkeypatch.setattr(update_check, "_install_channel", lambda: "native")
    started = []
    monkeypatch.setattr(update_check.threading.Thread, "start", lambda self: started.append(self))
    update_check.start_update_check("0.74.0")
    assert "v0.75.0" in update_check.get_update_notice()
    assert "aria update" in update_check.get_update_notice()
    assert len(started) == 1
    update_check.start_update_check("0.74.0", enabled=False)
    assert update_check.get_update_notice() is None
    assert len(started) == 1


def test_npm_launcher_channel_wins_over_frozen_executable(monkeypatch):
    monkeypatch.setenv("ARIA_CODE_INSTALL_CHANNEL", "npm")
    monkeypatch.setattr(update_check.sys, "frozen", True, raising=False)
    assert update_check._install_channel() == "npm"


def test_manual_check_reports_a_network_error(monkeypatch, capsys):
    monkeypatch.setattr(updater, "check_update", lambda channel: (_ for _ in ()).throw(OSError("offline")))
    assert updater.main(["--check"]) == 1
    assert "offline" in capsys.readouterr().err


def test_check_only_never_installs(monkeypatch, capsys):
    monkeypatch.setattr(updater, "check_update", lambda channel: ("99.0.0", {}))
    monkeypatch.setattr(updater, "install_native", lambda *args: pytest.fail("must not install"))
    assert updater.main(["--check"]) == 0
    assert "aria update" in capsys.readouterr().out


@pytest.mark.parametrize("args,code", [(["--help"], 0), (["--unknown"], 2)])
def test_updater_argument_handling_does_not_exit_the_repl(args, code):
    assert updater.main(args) == code


def test_checksum_failure_keeps_the_current_cli(monkeypatch, tmp_path):
    command = tmp_path / "bin/aria-code"
    command.parent.mkdir()
    command.write_text("previous working CLI")
    monkeypatch.setattr(updater, "_install_dir", lambda: command.parent)
    monkeypatch.setenv("ARIA_CODE_HOME", str(tmp_path / "app"))
    monkeypatch.setattr(updater.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(updater.platform, "machine", lambda: "arm64")
    def download(url, target):
        target.write_text("bad archive" if target.name.endswith(".tar.gz") else "0" * 64 + "  aria-code-macos-arm64.tar.gz\n")
    monkeypatch.setattr(updater, "_download", download)
    assets = [{"name": name, "browser_download_url": "https://github.com/artheras/aria-code/releases/download/v0.75.0/" + name} for name in ["aria-code-macos-arm64.tar.gz", "SHA256SUMS"]]
    with pytest.raises(ValueError, match="checksum"):
        updater.install_native("0.75.0", {"assets": assets})
    assert command.read_text() == "previous working CLI"


def test_archive_traversal_is_rejected(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        entry = tarfile.TarInfo("../escape")
        entry.size = 3
        bundle.addfile(entry, io.BytesIO(b"bad"))
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with pytest.raises(ValueError):
        updater._extract(archive, unpacked)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("version_matches", [True, False])
def test_native_update_verifies_binary_before_switching(monkeypatch, tmp_path, version_matches):
    command = tmp_path / "bin/aria-code"
    command.parent.mkdir()
    command.write_text("previous working CLI")
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        entry = tarfile.TarInfo("aria-code-bin/aria-code-bin")
        entry.mode = 0o755
        entry.size = 6
        bundle.addfile(entry, io.BytesIO(b"binary"))
    data = archive.read_bytes()
    def download(url, target):
        if target.name.endswith(".tar.gz"):
            target.write_bytes(data)
        else:
            target.write_text(hashlib.sha256(data).hexdigest() + "  aria-code-macos-arm64.tar.gz\n")
    monkeypatch.setattr(updater, "_download", download)
    monkeypatch.setattr(updater, "_install_dir", lambda: command.parent)
    monkeypatch.setenv("ARIA_CODE_HOME", str(tmp_path / "app"))
    monkeypatch.setattr(updater.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(updater.platform, "machine", lambda: "arm64")
    def probe(argv, **kwargs):
        assert command.read_text() == "previous working CLI"
        assert argv[-1] == "--version"
        return SimpleNamespace(stdout="aria-code " + ("0.75.0" if version_matches else "0.74.0"))
    monkeypatch.setattr(updater.subprocess, "run", probe)
    names = ["aria-code-macos-arm64.tar.gz", "SHA256SUMS"]
    assets = [{"name": n, "browser_download_url": "https://github.com/artheras/aria-code/releases/download/v0.75.0/" + n} for n in names]
    if version_matches:
        assert updater.install_native("0.75.0", {"assets": assets}) == command
        assert command.is_symlink()
        assert command.read_bytes() == b"binary"
        assert (command.parent / "aria").is_file()
    else:
        with pytest.raises(ValueError, match="version"):
            updater.install_native("0.75.0", {"assets": assets})
        assert not command.is_symlink()
        assert command.read_text() == "previous working CLI"
