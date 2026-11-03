"""scripts/platform_wheels.py: platform wheels carrying the frontend and the Go engine."""
from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import os
import struct
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("platform_wheels", ROOT / "scripts" / "platform_wheels.py")
platform_wheels = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(platform_wheels)


def elf(*glibc: str) -> bytes:
    return b"\x7fELF" + b"\x00" * 60 + b"".join(f"GLIBC_{v}\x00".encode() for v in glibc)


def macho(major: int, minor: int) -> bytes:
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 24, 0, 0)
    build = struct.pack("<IIIIII", 0x32, 24, 1, (major << 16) | (minor << 8), 0, 0)
    return header + build + b"\x00" * 64


def pure_wheel(tmp: Path) -> Path:
    path = tmp / "aria_code-1.2.3-py3-none-any.whl"
    files = {
        "aria_code/__init__.py": b"",
        "aria_code/runtime/native_index.py": b"# stub\n",
        "aria_code-1.2.3.dist-info/METADATA": b"Metadata-Version: 2.1\nName: aria-code\nVersion: 1.2.3\n",
        "aria_code-1.2.3.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: setuptools\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
        z.writestr("aria_code-1.2.3.dist-info/RECORD", "")
    return path


def tar_archive(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            t.addfile(info, io.BytesIO(data))


def release_archives(tmp: Path) -> Path:
    archives = tmp / "native"
    archives.mkdir()
    for slug, native, runtime in [
        ("linux-x64", elf("2.17", "2.28", "2.34"), elf()),
        ("linux-arm64", elf("2.17", "2.39"), elf()),
        ("macos-arm64", macho(11, 0), macho(12, 0)),
        ("macos-x64", macho(10, 12), macho(12, 0)),
    ]:
        tar_archive(archives / f"aria-code-{slug}.tar.gz", {
            "aria-code-bin/aria-code-bin": native,
            "aria-code-bin/aria-code-worker": b"frozen python",
            "aria-code-bin/aria-runtime": runtime,
            "aria-code-bin/_internal/aria-native": native,
        })
    with zipfile.ZipFile(archives / "aria-code-windows-x64.zip", "w") as z:
        z.writestr("aria-code-bin/aria-code-bin.exe", b"MZ frontend")
        z.writestr("aria-code-bin/aria-runtime.exe", b"MZ engine")
        z.writestr("aria-code-bin/_internal/aria-native.exe", b"MZ frontend")
    # The MCP server archives are not wheels' business.
    tar_archive(archives / "aria-code-mcp-linux-x64.tar.gz", {"aria-code-mcp-bin/aria-code-mcp-bin": b"x"})
    return archives


def test_platform_wheels_carry_both_binaries_with_honest_tags(tmp_path):
    wheels = platform_wheels.build(pure_wheel(tmp_path), release_archives(tmp_path), tmp_path / "out")
    names = sorted(w.name for w in wheels)
    assert names == sorted([
        "aria_code-1.2.3-py3-none-manylinux_2_34_x86_64.whl",
        "aria_code-1.2.3-py3-none-manylinux_2_39_aarch64.whl",
        "aria_code-1.2.3-py3-none-macosx_12_0_arm64.whl",
        "aria_code-1.2.3-py3-none-macosx_12_0_x86_64.whl",
        "aria_code-1.2.3-py3-none-win_amd64.whl",
    ])
    for wheel in wheels:
        windows = "win_amd64" in wheel.name
        suffix = ".exe" if windows else ""
        with zipfile.ZipFile(wheel) as z:
            names = z.namelist()
            assert f"aria_code/_bin/aria-native{suffix}" in names and f"aria_code/_bin/aria-runtime{suffix}" in names
            assert not any("worker" in n for n in names), "the frozen Python worker stays out of the wheel"
            for n in (f"aria_code/_bin/aria-native{suffix}", f"aria_code/_bin/aria-runtime{suffix}"):
                assert (z.getinfo(n).external_attr >> 16) & 0o111, "pip keeps the executable bit from the zip mode"
            meta = z.read("aria_code-1.2.3.dist-info/WHEEL").decode()
            tag = wheel.name[:-4].split("-", 2)[2]
            assert f"Tag: {tag}\n" in meta and "Root-Is-Purelib: false" in meta and "py3-none-any" not in meta
            rows = list(csv.reader(io.StringIO(z.read("aria_code-1.2.3.dist-info/RECORD").decode())))
            recorded = {r[0] for r in rows}
            assert recorded == set(names), "RECORD lists every file once"
            for path, digest, size in rows:
                if path.endswith("RECORD"):
                    continue
                data = z.read(path)
                assert digest == "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                assert int(size) == len(data)


def test_nothing_is_written_when_a_platform_is_missing_or_wrong(tmp_path):
    archives = release_archives(tmp_path)
    (archives / "aria-code-macos-x64.tar.gz").unlink()
    with pytest.raises(FileNotFoundError, match="macos-x64"):
        platform_wheels.build(pure_wheel(tmp_path), archives, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    tar_archive(archives / "aria-code-macos-x64.tar.gz", {"aria-code-bin/aria-runtime": macho(12, 0)})
    with pytest.raises(ValueError, match="no aria-native"):
        platform_wheels.build(pure_wheel(tmp_path), archives, tmp_path / "out")
    tar_archive(archives / "aria-code-macos-x64.tar.gz", {"aria-code-bin/aria-runtime": elf(), "aria-code-bin/_internal/aria-native": elf()})
    with pytest.raises(ValueError, match="Mach-O"):
        platform_wheels.build(pure_wheel(tmp_path), archives, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_cli_refuses_a_platform_wheel_as_input(tmp_path, capsys):
    wheel = tmp_path / "aria_code-1.2.3-py3-none-win_amd64.whl"
    wheel.write_bytes(pure_wheel(tmp_path).read_bytes())
    assert platform_wheels.main(["--wheel", str(wheel), "--archives", str(tmp_path), "--out", str(tmp_path / "o")]) == 1
    assert "not a pure" in capsys.readouterr().err


def test_static_elf_needs_no_newer_glibc():
    assert platform_wheels.glibc_minor(elf()) == 17
    assert platform_wheels.macos_minimum(macho(10, 15)) == (10, 15)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable bit")
def test_native_binary_finds_the_copy_in_a_platform_wheel(tmp_path, monkeypatch):
    from aria_code.runtime import native_index

    package = tmp_path / "aria_code"
    (package / "runtime").mkdir(parents=True)
    (package / "_bin").mkdir()
    fake_module = package / "runtime" / "native_index.py"
    fake_module.write_text("")
    binary = package / "_bin" / "aria-native"
    binary.write_bytes(b"#!/bin/sh\n")
    monkeypatch.delenv("ARIA_NATIVE_BINARY", raising=False)
    monkeypatch.setattr(native_index, "__file__", str(fake_module))
    monkeypatch.setattr(native_index.shutil, "which", lambda name: None)
    binary.chmod(0o644)
    assert native_index.native_binary() is None, "a copy that cannot run is not used"
    binary.chmod(0o755)
    assert native_index.native_binary() == str(binary.resolve())
    monkeypatch.setenv("ARIA_NATIVE_BINARY", str(tmp_path / "missing"))
    assert native_index.native_binary() is None, "an explicit setting still wins"
