"""Prevent independent edits or path tampering in the managed finance copies."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def fixture_repo(tmp_path):
    script = tmp_path / "scripts/sync_finance_core.py"
    script.parent.mkdir()
    shutil.copy2(ROOT / "scripts/sync_finance_core.py", script)
    lock = json.loads((ROOT / "finance-core.lock.json").read_text())
    (tmp_path / "finance-core.lock.json").write_text(json.dumps(lock))
    for local in lock["files"]:
        target = tmp_path / local
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / local, target)
    return script, lock


def run(script, *args):
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True)


def test_source_lock_verifies_in_a_fresh_directory_without_network(tmp_path):
    script, _ = fixture_repo(tmp_path)
    result = run(script, "--check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_dirty_copy_blocks_both_check_and_update_before_fetch(tmp_path):
    script, lock = fixture_repo(tmp_path)
    target = tmp_path / next(iter(lock["files"]))
    target.write_text("# a separate implementation\n")
    for args in [("--check",), ("--update", lock["commit"], "--source", str(tmp_path / "missing-source"))]:
        result = run(script, *args)
        assert result.returncode == 1
        assert "Managed finance file changed" in result.stdout
    assert target.read_text() == "# a separate implementation\n"


def test_lock_cannot_add_an_arbitrary_managed_path(tmp_path):
    script, lock = fixture_repo(tmp_path)
    lock["files"]["../outside.py"] = {"source": "x", "sha256": "0" * 64}
    (tmp_path / "finance-core.lock.json").write_text(json.dumps(lock))
    result = run(script, "--check")
    assert result.returncode == 1
    assert "Invalid finance source lock" in result.stdout


def test_updates_require_an_immutable_commit(tmp_path):
    script, _ = fixture_repo(tmp_path)
    result = run(script, "--update", "main")
    assert result.returncode == 1
    assert "full immutable" in result.stdout
