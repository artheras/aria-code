"""Run the package alias finder inside a real PYZ archive, without source files."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(importlib.util.find_spec("PyInstaller") is None, reason="requires the native packaging test environment")
@pytest.mark.timeout(120)
def test_frozen_aliases_work_without_a_physical_package_directory(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "src"
    package = source / "aria_code"
    nested = package / "compat_pkg"
    nested.mkdir(parents=True)
    (package / "__init__.py").write_text((root / "src/aria_code/__init__.py").read_text(encoding="utf-8"), encoding="utf-8")
    (package / "helper.py").write_text("sentinel = object()\n", encoding="utf-8")
    (nested / "__init__.py").write_text("", encoding="utf-8")
    (nested / "state.py").write_text("items = []\n", encoding="utf-8")
    entry = tmp_path / "smoke.py"
    entry.write_text("""
import json
from pathlib import Path
import aria_code
import helper
import aria_code.helper
import compat_pkg.state
import aria_code.compat_pkg.state
assert helper is aria_code.helper
assert compat_pkg.state is aria_code.compat_pkg.state
compat_pkg.state.items.append('shared')
assert aria_code.compat_pkg.state.items == ['shared']
root = Path(aria_code.__file__).parent
print(json.dumps({'root': str(root), 'root_exists': root.is_dir(), 'aliases_shared': True}))
""", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(source)}
    build = subprocess.run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "--onedir",
                            "--name", "alias-smoke", "--distpath", str(tmp_path / "dist"),
                            "--workpath", str(tmp_path / "build"), "--specpath", str(tmp_path),
                            "--paths", str(source), "--hidden-import", "aria_code.helper",
                            "--hidden-import", "aria_code.compat_pkg.state", str(entry)],
                           cwd=tmp_path, env=env, capture_output=True, timeout=90)
    assert build.returncode == 0, build.stderr.decode(errors="replace")[-5000:]
    binary = tmp_path / "dist" / "alias-smoke" / ("alias-smoke.exe" if os.name == "nt" else "alias-smoke")
    for physical_directory in (False, True):
        run = subprocess.run([str(binary)], cwd=tmp_path, capture_output=True, timeout=10)
        assert run.returncode == 0, run.stderr.decode(errors="replace")
        result = json.loads(run.stdout)
        assert result["aliases_shared"]
        assert result["root_exists"] is physical_directory
        Path(result["root"]).mkdir(exist_ok=True)

