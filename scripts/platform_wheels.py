#!/usr/bin/env python3
"""Platform wheels that carry the native frontend and the Go engine.

A plain ``pip install aria-code`` gets the pure wheel and runs the Python
engine. This script takes that wheel and the five native release archives
(``aria-code-{linux-x64,linux-arm64,macos-arm64,macos-x64,windows-x64}``)
and writes one wheel per platform with ``aria-native`` (the Rust terminal
frontend) and ``aria-runtime`` (the Go engine) in ``aria_code/_bin/``. pip
picks the wheel for its platform; ``aria`` then starts the Rust frontend,
which runs the Go engine beside it (``rust/cli/src/runtime.rs``).

Each wheel's tag is read from its binaries, not assumed: the newest glibc
symbol version on Linux, the minimum macOS version a Mach-O file declares.
The pure wheel stays as the fallback for every other platform.

    python scripts/platform_wheels.py --wheel dist/aria_code-X-py3-none-any.whl \\
        --archives native/ --out dist/
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import re
import struct
import sys
import tarfile
import zipfile
from pathlib import Path

PLATFORMS = {
    # archive slug: (wheel platform kind, executable suffix)
    "linux-x64": ("manylinux", "x86_64", ""),
    "linux-arm64": ("manylinux", "aarch64", ""),
    "macos-arm64": ("macosx", "arm64", ""),
    "macos-x64": ("macosx", "x86_64", ""),
    "windows-x64": ("win", "amd64", ".exe"),
}
BINARIES = ("aria-native", "aria-runtime")
MAX_BINARY = 256 << 20
GLIBC = re.compile(rb"GLIBC_2\.(\d+)(?:\.\d+)?\x00")


def glibc_minor(data: bytes) -> int:
    """The newest GLIBC_2.N symbol version an ELF file needs; 17 if none (static)."""
    if not data.startswith(b"\x7fELF"):
        raise ValueError("not an ELF executable")
    return max([17] + [int(m.group(1)) for m in GLIBC.finditer(data)])


def macos_minimum(data: bytes) -> tuple[int, int]:
    """The minimum macOS version a thin 64-bit Mach-O file declares."""
    if len(data) < 32 or struct.unpack_from("<I", data)[0] != 0xFEEDFACF:
        raise ValueError("not a thin 64-bit Mach-O executable")
    ncmds = struct.unpack_from("<I", data, 16)[0]
    offset = 32
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, offset)
        if cmd == 0x32:  # LC_BUILD_VERSION: platform, minos
            minos = struct.unpack_from("<I", data, offset + 12)[0]
            return minos >> 16, (minos >> 8) & 0xFF
        if cmd == 0x24:  # LC_VERSION_MIN_MACOSX: version
            version = struct.unpack_from("<I", data, offset + 8)[0]
            return version >> 16, (version >> 8) & 0xFF
        if size < 8:
            break
        offset += size
    raise ValueError("the Mach-O file declares no minimum macOS version")


def platform_tag(slug: str, binaries: dict[str, bytes]) -> str:
    kind, arch, _ = PLATFORMS[slug]
    if kind == "win":
        for name, data in binaries.items():
            if not data.startswith(b"MZ"):
                raise ValueError(f"{slug}: {name} is not a Windows executable")
        return "win_amd64"
    if kind == "manylinux":
        return f"manylinux_2_{max(glibc_minor(d) for d in binaries.values())}_{arch}"
    major, minor = max(macos_minimum(d) for d in binaries.values())
    # Wheel tags spell macOS 11 and later as N_0.
    return f"macosx_{major}_{minor if major < 11 else 0}_{arch}"


def read_binaries(archive: Path, suffix: str) -> dict[str, bytes]:
    """aria-native and aria-runtime from a release archive, as regular files only."""
    wanted = {name + suffix: name for name in BINARIES}
    found: dict[str, bytes] = {}

    def take(path: str, size: int, read) -> None:
        base = path.rsplit("/", 1)[-1]
        name = wanted.get(base)
        # The frontend's own copy is aria-code-bin; the indexer copy in
        # _internal/ is the same aria-native build.
        if name and name not in found:
            if size > MAX_BINARY:
                raise ValueError(f"{archive.name}: {path} is larger than {MAX_BINARY >> 20} MiB")
            found[name] = read()

    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if not info.is_dir():
                    take(info.filename, info.file_size, lambda i=info: z.read(i))
    else:
        with tarfile.open(archive, "r:gz") as t:
            for member in t:
                if member.isfile():
                    take(member.name, member.size, lambda m=member: t.extractfile(m).read())
    missing = [n for n in BINARIES if n not in found]
    if missing:
        raise ValueError(f"{archive.name} has no {', '.join(missing)}")
    return found


def record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def platform_wheel(pure: Path, binaries: dict[str, bytes], tag: str, suffix: str, out: Path) -> Path:
    name, version, *_ = pure.name[:-4].split("-")
    target = out / f"{name}-{version}-py3-none-{tag}.whl"
    if target.exists():
        raise FileExistsError(target)
    rows = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(pure) as src, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as dst:
        names = src.namelist()
        dist_info = next(n.split("/")[0] for n in names if n.endswith(".dist-info/WHEEL"))
        if not any(n.startswith("aria_code/") for n in names):
            raise ValueError(f"{pure.name} holds no aria_code package")
        if any(n.startswith("aria_code/_bin/") for n in names):
            raise ValueError(f"{pure.name} already carries binaries")
        for info in src.infolist():
            if info.filename in (f"{dist_info}/RECORD", f"{dist_info}/WHEEL"):
                continue
            data = src.read(info)
            dst.writestr(info, data)
            rows.append((info.filename, record_hash(data), str(len(data))))
        for base, data in sorted(binaries.items()):
            info = zipfile.ZipInfo(f"aria_code/_bin/{base}{suffix}", date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = (0o100755 << 16)
            info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(info, data)
            rows.append((info.filename, record_hash(data), str(len(data))))
        wheel = src.read(f"{dist_info}/WHEEL").decode()
        lines = [l for l in wheel.splitlines() if not l.startswith(("Tag:", "Root-Is-Purelib:"))]
        lines.insert(1, "Root-Is-Purelib: false")
        lines.append(f"Tag: py3-none-{tag}")
        wheel = ("\n".join(lines) + "\n").encode()
        dst.writestr(zipfile.ZipInfo(f"{dist_info}/WHEEL", date_time=(2020, 1, 1, 0, 0, 0)), wheel)
        rows.append((f"{dist_info}/WHEEL", record_hash(wheel), str(len(wheel))))
        record = io.StringIO()
        writer = csv.writer(record, lineterminator="\n")
        writer.writerows(rows)
        writer.writerow((f"{dist_info}/RECORD", "", ""))
        dst.writestr(zipfile.ZipInfo(f"{dist_info}/RECORD", date_time=(2020, 1, 1, 0, 0, 0)), record.getvalue())
    target.write_bytes(buffer.getvalue())
    return target


def build(pure: Path, archives: Path, out: Path) -> list[Path]:
    if not pure.name.endswith("-py3-none-any.whl"):
        raise ValueError(f"{pure.name} is not a pure py3-none-any wheel")
    found = {}
    for slug in PLATFORMS:
        candidates = [archives / f"aria-code-{slug}.tar.gz", archives / f"aria-code-{slug}.zip"]
        present = [c for c in candidates if c.is_file()]
        if not present:
            raise FileNotFoundError(f"no release archive for {slug} in {archives}")
        found[slug] = present[0]
    prepared = []
    # Read and tag everything before writing anything.
    for slug, archive in found.items():
        suffix = PLATFORMS[slug][2]
        binaries = read_binaries(archive, suffix)
        prepared.append((binaries, platform_tag(slug, binaries), suffix))
    out.mkdir(parents=True, exist_ok=True)
    return [platform_wheel(pure, binaries, tag, suffix, out) for binaries, tag, suffix in prepared]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--wheel", required=True, type=Path, help="the pure py3-none-any wheel")
    parser.add_argument("--archives", required=True, type=Path, help="directory of the native release archives")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        for wheel in build(args.wheel, args.archives, args.out):
            print(wheel.name)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"platform wheels: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
