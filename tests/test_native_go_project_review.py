"""Parity test between Python review_project_archive and Go go/projectreview."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from aria_code.project_review import review_project_archive

REPO_ROOT = Path(__file__).resolve().parent.parent
GO_PROJECT_REVIEW_DIR = REPO_ROOT / "go" / "projectreview"


@pytest.fixture(scope="module")
def go_project_review_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    env_bin = os.environ.get("ARIA_GO_PROJECT_REVIEW_BINARY")
    if env_bin:
        return Path(env_bin)
    if shutil.which("go") is None:
        pytest.skip("go toolchain not installed")
    out = tmp_path_factory.mktemp("projectreview") / "aria-project-review"
    res = subprocess.run(
        ["go", "build", "-o", str(out), "."],
        cwd=str(GO_PROJECT_REVIEW_DIR),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    return out


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_go_project_review_archive_matches_python(
    go_project_review_binary: Path,
    tmp_path: Path,
) -> None:
    payload = _make_zip(
        {
            "demo/README.md": b"# Demo App\nFastAPI + React web app.\n",
            "demo/package.json": b'{"name": "demo", "dependencies": {"react": "^18.0.0"}}\n',
            "demo/Dockerfile": b"FROM python:3.12-slim\n",
            "demo/.github/workflows/ci.yml": b"name: CI\non: [push]\n",
            "demo/.env": b"SECRET_KEY=should-be-skipped\n",
            "demo/src/app.py": (
                b"from fastapi import FastAPI\n"
                b"import subprocess, yaml\n"
                b"app = FastAPI()\n"
                b"API_KEY = 'sk-1234567890abcdefghijklmnop'\n"
                b"def run(cmd):\n"
                b"    try:\n"
                b"        subprocess.Popen(cmd, shell=True)\n"
                b"        yaml.load('a: 1')\n"
                b"    except:\n"
                b"        pass\n"
            ),
            "demo/src/View.tsx": b"export const View = () => <div>Hello</div>;\n",
            "demo/tests/test_app.py": b"def test_ok():\n    assert 1 == 1\n",
        }
    )
    archive_file = tmp_path / "demo.zip"
    archive_file.write_bytes(payload)

    py_report = review_project_archive(payload, "demo.zip")
    res = subprocess.run(
        [str(go_project_review_binary), "--review-archive", str(archive_file), "demo.zip"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert res.returncode == 0, res.stderr
    go_report = json.loads(res.stdout)

    assert go_report["schema_version"] == py_report["schema_version"]
    assert go_report["status"] == py_report["status"]
    assert go_report["summary"] == py_report["summary"]
    assert go_report["product_assessment"] == py_report["product_assessment"]
    assert go_report["recommendations"] == py_report["recommendations"]
    assert go_report["limitations"] == py_report["limitations"]
    assert go_report["findings"] == py_report["findings"]
