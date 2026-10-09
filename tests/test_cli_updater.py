import hashlib
import base64
import io
import json
from pathlib import Path
import tarfile
import time
import subprocess
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.error import URLError

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


@pytest.mark.parametrize("background", [False, True])
def test_npm_updates_survive_registry_content_negotiation(monkeypatch, tmp_path, background):
    """npm rejects the GitHub-only media type with 406, including /latest."""
    monkeypatch.setattr(update_check, "_CACHE_FILE", tmp_path / "update.json")
    monkeypatch.setattr(update_check, "_notice", None)
    requests = []

    def registry(request, **kwargs):
        requests.append(request)
        if request.get_header("Accept") != "application/json":
            raise HTTPError(request.full_url, 406, "Not Acceptable", {}, None)
        return io.BytesIO(b'{"version":"0.108.1"}')

    monkeypatch.setattr(updater.urllib.request, "urlopen", registry)
    if background:
        update_check._worker("0.108.0", "en", "npm")
        assert "v0.108.1" in update_check.get_update_notice()
    else:
        latest, _ = updater.check_update("npm")
        assert latest == "0.108.1"
    assert len(requests) == 1
    assert requests[0].full_url == update_check._NPM_URL
    assert update_check._read_cache()["latest"] == "0.108.1"


def test_update_downloads_request_the_raw_asset(monkeypatch, tmp_path):
    payload = b"release checksum or archive bytes"

    def asset(request, **kwargs):
        if request.get_header("Accept") != "*/*":
            raise HTTPError(request.full_url, 406, "Not Acceptable", {}, None)
        return io.BytesIO(payload)

    monkeypatch.setattr(updater.urllib.request, "urlopen", asset)
    target = tmp_path / "SHA256SUMS"
    updater._download("https://github.com/artheras/aria-code/releases/download/v0.108.1/SHA256SUMS", target)
    assert target.read_bytes() == payload


def test_pip_update_selects_github_version_and_checks_the_pinned_package(monkeypatch):
    calls = []
    def fetch(url):
        calls.append(url)
        return {"tag_name": "v0.75.0"} if url == update_check._RELEASE_URL else {"info": {"version": "0.75.0"}}
    monkeypatch.setattr(updater, "_fetch_json", fetch)
    monkeypatch.setattr(updater, "_write_cache", lambda data: None)
    latest, _ = updater.check_update("pip")
    assert latest == "0.75.0"
    assert calls == [update_check._RELEASE_URL, "https://pypi.org/pypi/aria-code/0.75.0/json"]


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


@pytest.mark.parametrize("failure", [None, "version", "imports", "timeout"])
def test_native_update_verifies_binary_before_switching(monkeypatch, tmp_path, failure):
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
    probes = []
    def probe(argv, **kwargs):
        assert command.read_text() == "previous working CLI"
        probes.append(argv)
        assert kwargs["check"]
        if argv[-1] == "--help":
            binary = Path(argv[0])
            assert binary.parents[1].name.startswith("v0.75.0-")
            assert kwargs["timeout"] == 120
            if failure == "imports":
                raise subprocess.CalledProcessError(1, argv, stderr="ModuleNotFoundError")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 120)
            return SimpleNamespace(stdout="usage: aria-code")
        assert argv[-1] == "--version"
        return SimpleNamespace(stdout="aria-code " + ("0.74.0" if failure == "version" else "0.75.0"))
    monkeypatch.setattr(updater.subprocess, "run", probe)
    names = ["aria-code-macos-arm64.tar.gz", "SHA256SUMS"]
    assets = [{"name": n, "browser_download_url": "https://github.com/artheras/aria-code/releases/download/v0.75.0/" + n} for n in names]
    if failure is None:
        assert updater.install_native("0.75.0", {"assets": assets}) == command
        assert command.is_symlink()
        assert command.read_bytes() == b"binary"
        assert (command.parent / "aria").is_file()
        assert [argv[-1] for argv in probes] == ["--version", "--help"]
    else:
        with pytest.raises(ValueError, match="version" if failure == "version" else "startup check"):
            updater.install_native("0.75.0", {"assets": assets})
        assert not command.is_symlink()
        assert command.read_text() == "previous working CLI"
        assert not (tmp_path / "app/previous.json").exists()
        assert not list((tmp_path / "app/releases").glob("v0.75.0-*"))


def test_pinned_release_does_not_replace_the_latest_cache(monkeypatch):
    calls = []
    def fetch(url):
        calls.append(url)
        return {"tag_name": "v0.109.0"} if "github.com" in url else {"info": {"version": "0.109.0"}}
    monkeypatch.setattr(updater, "_fetch_json", fetch)
    monkeypatch.setattr(updater, "_write_cache", lambda _: pytest.fail("pinned version is not latest"))
    latest, _ = updater.check_update("pip", version="v0.109.0")
    assert latest == "0.109.0"
    assert calls == ["https://api.github.com/repos/artheras/aria-code/releases/tags/v0.109.0",
                     "https://pypi.org/pypi/aria-code/0.109.0/json"]


@pytest.mark.parametrize("valid_integrity", [True, False])
def test_native_network_fallback_verifies_official_npm_artifact(monkeypatch, tmp_path, valid_integrity):
    command = tmp_path / "bin/aria-code"
    command.parent.mkdir()
    command.write_text("previous")
    monkeypatch.setattr(updater, "_install_dir", lambda: command.parent)
    monkeypatch.setenv("ARIA_CODE_HOME", str(tmp_path / "app"))
    monkeypatch.setattr(updater.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(updater.platform, "machine", lambda: "arm64")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        entry = tarfile.TarInfo("package/bin/aria-code-bin/aria-code-bin")
        entry.size, entry.mode = 6, 0o755
        bundle.addfile(entry, io.BytesIO(b"binary"))
    payload = archive.getvalue()
    name = "aria-code-macos-arm64"
    url = f"https://registry.npmjs.org/@artheras/{name}/-/{name}-0.110.0.tgz"
    checksum = hashlib.sha512(payload if valid_integrity else b"wrong").digest()
    metadata = {"name": f"@artheras/{name}", "version": "0.110.0",
                "dist": {"tarball": url, "integrity": "sha512-" + base64.b64encode(checksum).decode()}}
    monkeypatch.setattr(updater, "_fetch_json", lambda requested: metadata)
    def download(requested, target):
        if "github.com" in requested:
            raise URLError("CDN unavailable")
        assert requested == url
        target.write_bytes(payload)
    monkeypatch.setattr(updater, "_download", download)
    def check(*args, **kwargs):
        assert command.read_text() == "previous"
        return SimpleNamespace(stdout="aria-code 0.110.0")
    monkeypatch.setattr(updater.subprocess, "run", check)
    assets = [{"name": n, "browser_download_url": f"https://github.com/artheras/aria-code/releases/download/v0.110.0/{n}"}
              for n in ("aria-code-macos-arm64.tar.gz", "SHA256SUMS")]
    if valid_integrity:
        updater.install_native("0.110.0", {"assets": assets})
        assert command.read_bytes() == b"binary"
    else:
        with pytest.raises(ValueError, match="checksum"):
            updater.install_native("0.110.0", {"assets": assets})
        assert command.read_text() == "previous"


def test_native_rollback_checks_previous_binary_and_switches_without_network(monkeypatch, tmp_path):
    app_root = tmp_path / "app"
    old = app_root / "releases/old/aria-code-bin/aria-code-bin"
    current = app_root / "releases/current/aria-code-bin/aria-code-bin"
    for binary in (old, current):
        binary.parent.mkdir(parents=True)
        binary.write_text("binary")
        binary.chmod(0o755)
    command = tmp_path / "bin/aria-code"
    command.parent.mkdir()
    command.symlink_to(current)
    (app_root / "previous.json").write_text(json.dumps({"previous": str(old.relative_to(app_root / "releases"))}))
    monkeypatch.setenv("ARIA_CODE_HOME", str(app_root))
    monkeypatch.setattr(updater, "_install_dir", lambda: command.parent)
    monkeypatch.setattr(updater, "_fetch_json", lambda _: pytest.fail("rollback must be offline"))
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="aria-code 0.109.0"))
    assert updater.rollback_native() == command
    assert command.resolve() == old
    state = json.loads((app_root / "previous.json").read_text())
    assert (app_root / "releases" / state["previous"]).resolve() == current


def test_rollback_rejects_paths_outside_managed_releases(monkeypatch, tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "previous.json").write_text('{"previous":"../../outside"}')
    monkeypatch.setenv("ARIA_CODE_HOME", str(app_root))
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **kw: pytest.fail("must not run an untrusted path"))
    with pytest.raises(ValueError, match="No previous"):
        updater.rollback_native()
