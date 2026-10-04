"""Explicit, verified CLI updates. Startup only checks and reminds."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from aria_code._version import __version__
from aria_code.apps.cli.update_check import (
    _RELEASE_URL, _NPM_URL, _PYPI_URL, _install_channel, _newer,
    _parse, _write_cache,
)


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": "aria-code-updater", "Accept": "application/vnd.github+json"})


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(_request(url), timeout=15) as response:
        return json.load(response)


def check_update(channel: str) -> tuple[str, dict]:
    import time

    source = {"native": _RELEASE_URL, "npm": _NPM_URL, "pip": _PYPI_URL}[channel]
    data = _fetch_json(source)
    latest = data.get("tag_name") if channel == "native" else (
        data.get("info", {}).get("version") if channel == "pip" else data.get("version")
    )
    if not isinstance(latest, str) or _parse(latest) is None:
        raise ValueError("The update source did not return a stable Aria Code version")
    if channel == "native" and (data.get("draft") or data.get("prerelease")):
        raise ValueError("The release is not a stable published version")
    _write_cache({"source": source, "latest": latest, "checked_at": time.time()})
    return latest.removeprefix("v"), data


def _download(url: str, target: Path) -> None:
    with urllib.request.urlopen(_request(url), timeout=60) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output)


def _checksum(path: Path, checksums: str, name: str) -> None:
    entries = [line.split() for line in checksums.splitlines()]
    expected = next((parts[0] for parts in entries if len(parts) == 2 and parts[1] == name), "")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if len(expected) != 64 or digest.hexdigest() != expected.lower():
        raise ValueError(f"Release checksum verification failed for {name}")


def _extract(archive: Path, directory: Path) -> None:
    with tarfile.open(archive) as bundle:
        # Validate each entry immediately before extracting, so Python 3.10
        # resolves links that earlier archive entries have already created.
        for member in bundle.getmembers():
            target = (directory / member.name).resolve()
            target.relative_to(directory.resolve())
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError("Unexpected file type in release archive")
            if member.issym() or member.islnk():
                link = (target.parent / member.linkname).resolve() if member.issym() else (directory / member.linkname).resolve()
                link.relative_to(directory.resolve())
            if hasattr(tarfile, "data_filter"):
                bundle.extract(member, directory, filter="data")
            else:
                bundle.extract(member, directory)


def _install_dir() -> Path:
    if os.environ.get("ARIA_CODE_INSTALL_DIR"):
        return Path(os.environ["ARIA_CODE_INSTALL_DIR"]).expanduser()
    current = Path(sys.executable).resolve()
    for path in (Path(sys.argv[0]).expanduser(), Path.home() / ".local/bin/aria-code"):
        if path.is_file() and path.resolve() == current:
            return path.absolute().parent
    if current.parent.name == "aria-code-bin":
        raise ValueError("Cannot locate the command symlink; set ARIA_CODE_INSTALL_DIR to your CLI bin directory")
    return current.parent


def install_native(latest: str, release: dict) -> Path:
    system = {"Darwin": "macos", "Linux": "linux"}.get(platform.system())
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(platform.machine().lower())
    if not system or not arch:
        raise ValueError("Use the PowerShell installer to update this platform")
    asset_name = f"aria-code-{system}-{arch}.tar.gz"
    assets = {a["name"]: a for a in release.get("assets", [])}
    if asset_name not in assets or "SHA256SUMS" not in assets:
        raise ValueError(f"The release is not ready for {system}-{arch}; your installed version is kept")
    install_dir = _install_dir()
    app_root = Path(os.environ.get("ARIA_CODE_HOME", str(Path.home() / ".local/share/aria-code"))).expanduser()
    releases = app_root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    install_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".update-", dir=releases) as staging:
        stage = Path(staging)
        archive = stage / asset_name
        sums = stage / "SHA256SUMS"
        for name, target in ((asset_name, archive), ("SHA256SUMS", sums)):
            url = assets[name]["browser_download_url"]
            expected_prefix = f"https://github.com/artheras/aria-code/releases/download/v{latest}/"
            if not url.startswith(expected_prefix):
                raise ValueError("Release asset URL does not belong to this Aria Code release")
            _download(url, target)
        _checksum(archive, sums.read_text(), asset_name)
        unpacked = stage / "unpacked"
        unpacked.mkdir()
        _extract(archive, unpacked)
        binary = unpacked / "aria-code-bin/aria-code-bin"
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("Release archive does not contain a runnable Aria Code binary")
        check = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=90, check=True)
        if check.stdout.strip() != f"aria-code {latest}":
            raise ValueError("Downloaded binary version does not match the release")
        active = releases / f"v{latest}-{os.urandom(4).hex()}"
        unpacked.rename(active)
    alias = install_dir / "aria"
    if not alias.exists():
        alias.write_text('#!/bin/sh\nif [ "${1-}" = code ]; then shift; fi\nexec "$(dirname "$0")/aria-code" "$@"\n')
        alias.chmod(0o755)
    link = install_dir / f".aria-code-update-{os.getpid()}"
    try:
        link.symlink_to(active / "aria-code-bin/aria-code-bin")
        os.replace(link, install_dir / "aria-code")
    finally:
        link.unlink(missing_ok=True)
    return install_dir / "aria-code"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aria update", description="Check for or install a verified Aria Code update")
    parser.add_argument("--check", action="store_true", help="Check and report without installing")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_request:
        # The same entrypoint runs inside /update: help and invalid arguments
        # must return to the prompt instead of terminating the whole session.
        return int(exit_request.code or 0)
    from aria_code.apps.cli.bootstrap import initialize_cli_environment

    initialize_cli_environment()
    channel = _install_channel()
    try:
        latest, release = check_update(channel)
        print(f"Aria Code {__version__} · latest {latest} · {channel}")
        if not _newer(latest, __version__):
            print("You are up to date.")
            return 0
        if args.check:
            print("Update available. Run: aria update")
            return 0
        if channel == "native":
            command = install_native(latest, release)
            print(f"Installed {latest}: {command}. Restart Aria to use the new version.")
            return 0
        executable = shutil.which("npm") if channel == "npm" else sys.executable
        if not executable:
            raise ValueError("npm is not on PATH; run the update from the shell where npm is installed")
        command = [executable, "install", "-g", f"@artheras/aria-code@{latest}"] if channel == "npm" else [executable, "-m", "pip", "install", "--upgrade", f"aria-code=={latest}"]
        return subprocess.call(command)
    except Exception as error:
        print(f"Update failed: {error}. The installed version was kept.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
