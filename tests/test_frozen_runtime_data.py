"""Resources must be present in real frozen imports, not only source installs."""
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def collector():
    spec = importlib.util.spec_from_file_location("frozen_resource_collector", ROOT / "scripts/pyinstaller_collect_args.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_required_schema_is_collected_under_both_import_roots(tmp_path):
    module = collector()
    hooks = module.write_hook(tmp_path)
    data = runpy.run_path(str(hooks / "hook-aria_code.py"))["datas"]
    assert {destination for _, destination in data} == {"aria_code/agents/warehouse", "agents/warehouse"}
    for source, _ in data:
        assert json.loads(Path(source).read_text())["type"] == "object"
    assert len(data) == 2  # no unrelated project/private JSON collection


def test_missing_runtime_schema_stops_packaging_early(tmp_path, monkeypatch):
    module = collector()
    monkeypatch.setattr(module, "PACKAGE", tmp_path)
    with pytest.raises(FileNotFoundError, match="runtime resource"):
        module.runtime_data_files()


@pytest.mark.slow_packaging
def test_real_pyinstaller_imports_schema_without_source_tree(tmp_path, monkeypatch):
    if importlib.util.find_spec("PyInstaller") is None:
        pytest.skip("PyInstaller is only required in native packaging jobs")
    module = collector()
    fixture = tmp_path / "fixture with spaces"
    package = fixture / "src/aria_code"
    warehouse = package / "agents/warehouse"
    warehouse.mkdir(parents=True)
    for folder in (package, package / "agents", warehouse):
        (folder / "__init__.py").write_text("")
    for name in ("task_agent.py", "navigation.schema.json"):
        shutil.copyfile(ROOT / "src/aria_code/agents/warehouse" / name, warehouse / name)
    monkeypatch.setattr(module, "PACKAGE", package)
    hooks = module.write_hook(fixture / "hooks")
    probe = fixture / "probe.py"
    probe.write_text(
        "from aria_code.agents.warehouse.task_agent import SCHEMA as packaged\n"
        "from agents.warehouse.task_agent import SCHEMA as bare\n"
        "assert packaged == bare and 'action' in packaged['properties']\n"
        "print('FROZEN_SCHEMA_OK')\n"
    )
    build_env = dict(os.environ)
    # Also supports an isolated --target test dependency install. Only the
    # PyInstaller dependency directory is inherited, never the project sources.
    build_env["PYTHONPATH"] = str(Path(importlib.util.find_spec("PyInstaller").origin).parents[1])
    build_env.pop("PYTHONHOME", None)
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--onedir", "--clean", "--noconfirm",
         "--name", "schema-probe", "--distpath", str(fixture / "dist"),
         "--workpath", str(fixture / "build"), "--specpath", str(fixture),
         "--paths", str(package.parent), "--paths", str(package),
         "--additional-hooks-dir", str(hooks), "--hidden-import", "aria_code", str(probe)],
        cwd=fixture, env=build_env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-5000:]
    # Remove the sources and run elsewhere, so PYTHONPATH/cwd cannot mask a
    # missing data file as it did in source-only tests.
    shutil.rmtree(fixture / "src")
    executable = fixture / "dist/schema-probe" / ("schema-probe.exe" if os.name == "nt" else "schema-probe")
    run_env = dict(build_env)
    run_env.pop("PYTHONPATH", None)
    result = subprocess.run([str(executable)], cwd=tmp_path, env=run_env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "FROZEN_SCHEMA_OK"
