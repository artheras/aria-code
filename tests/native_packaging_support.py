"""Independent archive fixtures and an actual Go packaging executable."""
import atexit
import functools
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]

@functools.lru_cache(maxsize=1)
def build_tool():
    directory = Path(tempfile.mkdtemp(prefix="aria-buildtools-test-"))
    atexit.register(shutil.rmtree, directory, True)
    binary = directory / ("aria-bundle.exe" if os.name == "nt" else "aria-bundle")
    subprocess.run(["go", "build", "-o", str(binary), "."],
                   cwd=ROOT / "go/buildtools", check=True, capture_output=True, timeout=120)
    return binary

def run_tool(*args):
    result = subprocess.run([str(build_tool()), *map(str, args)],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise SystemExit(result.stderr)
    return result.stdout

def fixture_archive(source, output, *, use_zip=False):
    output.mkdir(parents=True, exist_ok=True)
    target = output / (source.name + (".zip" if use_zip else ".tar.gz"))
    if use_zip:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for filename in sorted(source.rglob("*")):
                archive.write(filename, filename.relative_to(source.parent).as_posix())
    else:
        with tarfile.open(target, "w:gz") as archive:
            archive.add(source, arcname=source.name)
    return target
