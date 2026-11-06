#!/usr/bin/env python3
"""Verify or deliberately update the finance helpers owned by financial-service.

The checked-in copies keep Python 3.10 consumers working without an MCP SDK
install. They have one upstream owner, an immutable source commit and hashes;
normal builds/CI verify offline and never execute code from another checkout.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/artheras/financial-service.git"
MANAGED = {
    "src/aria_code/domain/performance_stats.py": "src/arthera_finance/performance_stats.py",
    "src/aria_code/datasources/sec_statements.py": "src/arthera_finance/sec_statements.py",
}
LOCK = ROOT / "finance-core.lock.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check() -> dict:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    if (
        lock.get("schema_version") != 1
        or lock.get("repository") != REPOSITORY
        or not re.fullmatch(r"[0-9a-f]{40}", str(lock.get("commit", "")))
        or set(lock.get("files", {})) != set(MANAGED)
    ):
        raise ValueError("Invalid finance source lock")
    for local, source in MANAGED.items():
        entry = lock["files"][local]
        target = ROOT / local
        if entry.get("source") != source or target.is_symlink():
            raise ValueError(f"Invalid managed finance path: {local}")
        if digest(target.read_bytes()) != entry.get("sha256"):
            raise ValueError(f"Managed finance file changed: {local}; change financial-service and update the pin")
    return lock


def source_bytes(commit: str, source: str, checkout: Path | None) -> bytes:
    if checkout is not None:
        # Read the committed tree, not ignored/unstaged files or editable installs.
        resolved = subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", f"{commit}^{{commit}}"], text=True
        ).strip()
        if resolved != commit:
            raise ValueError("Source commit did not resolve exactly")
        data = subprocess.check_output(["git", "-C", str(checkout), "show", f"{commit}:{source}"])
    else:
        with urlopen(
            f"https://raw.githubusercontent.com/artheras/financial-service/{commit}/{source}", timeout=30
        ) as response:
            data = response.read(512 * 1024 + 1)
    if len(data) > 512 * 1024:
        raise ValueError("Finance source exceeds size limit")
    ast.parse(data.decode("utf-8"), filename=source)
    return data


def update(commit: str, checkout: Path | None) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Use a full immutable 40-character commit SHA")
    existing = check() if LOCK.exists() else None
    payloads = {local: source_bytes(commit, remote, checkout) for local, remote in MANAGED.items()}
    if existing is None:
        for local, data in payloads.items():
            if (ROOT / local).read_bytes() != data:
                raise ValueError(f"Initial import differs at {local}; review before establishing ownership")
    lock = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "commit": commit,
        "files": {local: {"source": MANAGED[local], "sha256": digest(data)} for local, data in payloads.items()},
    }
    outputs = {
        **{ROOT / local: data for local, data in payloads.items()},
        LOCK: (json.dumps(lock, indent=2) + "\n").encode(),
    }
    # Validate all inputs first, then stage in the repository's filesystem.
    with tempfile.TemporaryDirectory(prefix=".finance-source-", dir=ROOT) as temporary:
        staged = []
        for index, (target, data) in enumerate(outputs.items()):
            if target.is_symlink():
                raise ValueError(f"Refusing managed symlink: {target.name}")
            pending = Path(temporary) / str(index)
            pending.write_bytes(data)
            staged.append((pending, target))
        for pending, target in staged:
            os.replace(pending, target)
    check()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Verify managed files offline")
    mode.add_argument("--update", metavar="COMMIT", help="Import an explicitly reviewed financial-service commit")
    parser.add_argument("--source", type=Path, help="Read the committed files from an authenticated source clone")
    args = parser.parse_args()
    if args.source and not args.update:
        parser.error("--source requires --update")
    try:
        if args.update:
            update(args.update, args.source)
        lock = check()
        print(f"Finance source verified: {lock['commit']} ({len(MANAGED)} files)")
        return 0
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Finance source check failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
