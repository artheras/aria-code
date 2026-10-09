"""Explicit, verified CLI updates. Startup only checks and reminds."""
from __future__ import annotations

import argparse
import base64
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
import urllib.error

from aria_code._version import __version__
from aria_code.apps.cli.update_check import (
    _RELEASE_URL, _NPM_URL, _install_channel, _newer,
    _parse, _write_cache,
)


def _request(url: str, *, accept: str = "application/json") -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": "aria-code-updater", "Accept": accept})


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(_request(url), timeout=15) as response:
        return json.load(response)


def check_update(channel: str, *, version: str | None = None) -> tuple[str, dict]:
    import time

    source = _NPM_URL if channel == "npm" else _RELEASE_URL
    if version:
        if _parse(version) is None:
            raise ValueError("Version must look like 0.110.0 or v0.110.0")
        version = version.removeprefix("v")
        source = (f"https://registry.npmjs.org/@artheras%2Faria-code/{version}" if channel == "npm" else
                  f"https://api.github.com/repos/artheras/aria-code/releases/tags/v{version}")
    data = _fetch_json(source)
    latest = data.get("version") if channel == "npm" else data.get("tag_name")
    if not isinstance(latest, str) or _parse(latest) is None:
        raise ValueError("The update source did not return a stable Aria Code version")
    if version and latest.removeprefix("v") != version:
        raise ValueError("The release metadata does not match the requested version")
    if channel != "npm" and (data.get("draft") or data.get("prerelease")):
        raise ValueError("The release is not a stable published version")
    if channel == "pip":
        # The old 4.x numbering is still PyPI's largest version. Never use its
        # /json latest endpoint to select a replacement for the current 0.x line.
        package = _fetch_json(f"https://pypi.org/pypi/aria-code/{latest.removeprefix('v')}/json")
        if package.get("info", {}).get("version") != latest.removeprefix("v"):
            raise ValueError("This GitHub release is not ready on PyPI; your installed version is kept")
    if not version:
        _write_cache({"source": source, "latest": latest, "checked_at": time.time()})
    return latest.removeprefix("v"), data


def _download(url: str, target: Path) -> None:
    from aria_code.apps.cli.release_download import download

    download(url, target)


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


def _app_root() -> Path:
    return Path(os.environ.get("ARIA_CODE_HOME", str(Path.home() / ".local/share/aria-code"))).expanduser().resolve()


def _switch_native(command: Path, binary: Path, app_root: Path) -> None:
    """Switch atomically and remember the former managed release for rollback."""
    previous = command.resolve() if command.is_symlink() else None
    if previous and previous != binary and previous.is_file():
        try:
            relative = previous.relative_to((app_root / "releases").resolve())
        except ValueError:
            relative = None
        if relative is not None:
            state = app_root / f".previous-{os.getpid()}.json"
            state.write_text(json.dumps({"previous": str(relative)}))
            os.replace(state, app_root / "previous.json")
    link = command.parent / f".aria-code-update-{os.getpid()}"
    try:
        link.symlink_to(binary)
        os.replace(link, command)
    finally:
        link.unlink(missing_ok=True)


def rollback_native() -> Path:
    """Restore the previous managed binary; no network and no directory deletion."""
    app_root = _app_root().resolve()
    try:
        data = json.loads((app_root / "previous.json").read_text())
        binary = (app_root / "releases" / data["previous"]).resolve()
        binary.relative_to(app_root / "releases")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("No previous managed release is recorded; use aria update --to VERSION instead") from exc
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("Previous release is missing or not executable; the installed version was kept")
    check = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=90, check=True)
    text = check.stdout.strip()
    if not text.startswith("aria-code ") or _parse(text.removeprefix("aria-code ")) is None:
        raise ValueError("Previous release failed its version check; the installed version was kept")
    command = _install_dir() / "aria-code"
    _switch_native(command, binary, app_root)
    return command


def _npm_native(latest: str, system: str, arch: str, stage: Path, cache: Path) -> Path:
    """Official scoped npm artifact fallback, verified against its SHA-512 SRI."""
    package = f"aria-code-{system}-{arch}"
    metadata = _fetch_json(f"https://registry.npmjs.org/@artheras%2F{package}/{latest}")
    if metadata.get("name") != f"@artheras/{package}" or metadata.get("version") != latest:
        raise ValueError("npm fallback does not match this Aria Code platform release")
    dist = metadata.get("dist") or {}
    expected_url = f"https://registry.npmjs.org/@artheras/{package}/-/{package}-{latest}.tgz"
    if dist.get("tarball") != expected_url:
        raise ValueError("npm fallback URL does not belong to this Aria Code release")
    integrity = str(dist.get("integrity") or "")
    if not integrity.startswith("sha512-"):
        raise ValueError("npm fallback has no SHA-512 integrity")
    expected = base64.b64decode(integrity[7:], validate=True)
    if len(expected) != 64:
        raise ValueError("Invalid npm fallback integrity")
    archive = cache / f"{package}-{latest}.tgz"
    _download(expected_url, archive)
    digest = hashlib.sha512()
    with archive.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.digest() != expected:
        archive.unlink(missing_ok=True)
        raise ValueError("npm fallback checksum verification failed")
    unpacked = stage / "npm"
    unpacked.mkdir()
    _extract(archive, unpacked)
    return unpacked / "package/bin/aria-code-bin"


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
    app_root = _app_root()
    releases = app_root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    install_dir.mkdir(parents=True, exist_ok=True)
    cache = app_root / "downloads" / f"v{latest}"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".update-", dir=releases) as staging:
        stage = Path(staging)
        archive = cache / asset_name
        sums = cache / "SHA256SUMS"
        for name in (asset_name, "SHA256SUMS"):
            url = assets[name]["browser_download_url"]
            expected_prefix = f"https://github.com/artheras/aria-code/releases/download/v{latest}/"
            if not url.startswith(expected_prefix):
                raise ValueError("Release asset URL does not belong to this Aria Code release")
        unpacked = stage / "unpacked"
        unpacked.mkdir()
        try:
            _download(assets["SHA256SUMS"]["browser_download_url"], sums)
            cached = False
            if archive.is_file():
                try:
                    _checksum(archive, sums.read_text(), asset_name)
                    cached = True
                except ValueError:
                    archive.unlink()
            if not cached:
                _download(assets[asset_name]["browser_download_url"], archive)
            _checksum(archive, sums.read_text(), asset_name)
        except (OSError, urllib.error.URLError) as error:
            print(f"GitHub transfer failed ({type(error).__name__}); trying the verified official npm artifact.", file=sys.stderr)
            build = _npm_native(latest, system, arch, stage, cache)
            shutil.move(str(build), unpacked / "aria-code-bin")
        except ValueError:
            archive.unlink(missing_ok=True)
            raise
        else:
            _extract(archive, unpacked)
        binary = unpacked / "aria-code-bin/aria-code-bin"
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("Release archive does not contain a runnable Aria Code binary")
        check = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=90, check=True)
        if check.stdout.strip() != f"aria-code {latest}":
            raise ValueError("Downloaded binary version does not match the release")
        active = releases / f"v{latest}-{os.urandom(4).hex()}"
        unpacked.rename(active)
        # --version exits before the CLI imports. Validate those imports at
        # the final path, so missing bundled modules fail before activation
        # and macOS's first-load checks happen during the visible installation.
        print("Preparing the CLI for its first startup...")
        try:
            subprocess.run([str(active / "aria-code-bin/aria-code-bin"), "--help"],
                           cwd=active, capture_output=True, text=True, timeout=120, check=True)
        except (OSError, subprocess.SubprocessError) as error:
            shutil.rmtree(active)
            raise ValueError("Downloaded CLI failed its startup check; the installed version was kept") from error
    alias = install_dir / "aria"
    if not alias.exists():
        alias.write_text('#!/bin/sh\nif [ "${1-}" = code ]; then shift; fi\nexec "$(dirname "$0")/aria-code" "$@"\n')
        alias.chmod(0o755)
    _switch_native(install_dir / "aria-code", active / "aria-code-bin/aria-code-bin", app_root)
    return install_dir / "aria-code"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aria update", description="Check for or install a verified Aria Code update")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Check and report without installing")
    mode.add_argument("--rollback", action="store_true", help="Restore the previous managed native release without network")
    mode.add_argument("--to", metavar="VERSION", help="Install a specific verified stable release")
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
        if args.rollback:
            if channel != "native":
                raise ValueError("--rollback is for native installs; use your package manager to select an older version")
            command = rollback_native()
            print(f"Restored previous release: {command}. Restart Aria to use it.")
            return 0
        latest, release = check_update(channel, version=args.to) if args.to else check_update(channel)
        print(f"Aria Code {__version__} · latest {latest} · {channel}")
        print(f"Running executable: {Path(sys.executable).resolve()}")
        if not args.to and not _newer(latest, __version__):
            print("You are up to date.")
            return 0
        if args.check:
            print("Update available. Run: aria update")
            return 0
        if channel == "source":
            print("Source checkout: update your Git branch, then run: python3 -m pip install -e .")
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
