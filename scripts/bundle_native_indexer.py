#!/usr/bin/env python3
"""Build ``aria-native`` and put it inside a PyInstaller --onedir build.

    python scripts/bundle_native_indexer.py dist-native/dist/aria-code-bin

The native project-graph indexer (``aria-native index imports|symbols``, see
docs/rust-cli-migration.md) only helps users who have the binary. The native
release is a PyInstaller --onedir directory per platform; this builds the Rust
workspace for the runner's platform and copies the executable into the
directory's contents folder (``_internal`` on PyInstaller 6), which is
``sys._MEIPASS`` at run time — where ``runtime/native_index.py`` looks when
frozen. On macOS that folder is also what scripts/build_native_binary.sh signs
Mach-O by Mach-O, so the binary is signed and notarized with the rest.

It then runs the copied binary: ``--version`` and an empty ``index imports``
request must answer, or the build fails rather than shipping a broken file.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = "aria-native.exe" if sys.platform == "win32" else "aria-native"


def contents_dir(onedir: Path) -> Path:
    """PyInstaller 6 keeps everything but the launcher in _internal/."""
    internal = onedir / "_internal"
    return internal if internal.is_dir() else onedir


def main(argv: list[str]) -> int:
    frontend = "--frontend" in argv
    argv = [a for a in argv if a != "--frontend"]
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    onedir = Path(argv[0]).resolve()
    if not onedir.is_dir():
        print(f"not a directory: {onedir}", file=sys.stderr)
        return 2

    subprocess.run(["cargo", "build", "--release", "--locked", "-p", "aria-native"],
                   cwd=ROOT / "rust", check=True)
    build_dir = Path(os.environ.get("CARGO_TARGET_DIR", str(ROOT / "rust" / "target")))
    if not build_dir.is_absolute():
        build_dir = ROOT / "rust" / build_dir
    built = build_dir / "release" / EXE
    target = contents_dir(onedir) / EXE
    shutil.copy2(built, target)
    target.chmod(0o755)

    version = subprocess.run([str(target), "--version"], capture_output=True, text=True,
                             timeout=30, check=True)
    probe = subprocess.run([str(target), "index", "imports"], capture_output=True, text=True, timeout=30,
                           input=json.dumps({"root": str(onedir), "files": [], "parse": []}), check=True)
    if json.loads(probe.stdout) != {"imports": {}, "fallback": []}:
        print(f"unexpected index answer: {probe.stdout!r}", file=sys.stderr)
        return 1
    print(f"bundled {version.stdout.strip()} at {target} ({target.stat().st_size // 1024} KiB)")
    if frontend:
        launcher = onedir / ("aria-code-bin.exe" if sys.platform == "win32" else "aria-code-bin")
        worker = onedir / ("aria-code-worker.exe" if sys.platform == "win32" else "aria-code-worker")
        if not launcher.is_file() or worker.exists():
            raise RuntimeError("Expected a fresh aria-code-bin PyInstaller directory")
        # Preserve PyInstaller's sibling _internal layout. Rust is the formal
        # launcher; it discovers this worker without needing system Python.
        launcher.rename(worker)
        shutil.copy2(built, launcher)
        launcher.chmod(0o755)
        subprocess.run([str(launcher), "--version"], check=True, timeout=30)
        print(f"native frontend installed; Python application worker at {worker}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
