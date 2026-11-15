from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

import pytest

from aria_code.workspace_service import WorkspaceService, WorkspaceServiceError
from aria_code.workspace_tools import build_workspace_proposal_tools, build_workspace_read_tools


def _zip(entries: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return output.getvalue()


def test_workspace_import_is_isolated_and_skips_secrets(tmp_path):
    service = WorkspaceService(tmp_path, ttl_hours=2)
    manifest = service.create_from_archive(
        _zip({"app/main.py": "print('ok')", ".env": "TOKEN=secret"}),
        "project.zip",
        owner_id="tenant:user-a",
        run_id="run-a",
    )

    assert manifest.execution_enabled is False
    assert manifest.network_enabled is False
    assert manifest.skipped_sensitive_files == 1
    assert (tmp_path / manifest.workspace_id / "source" / "app" / "main.py").exists()
    assert not (tmp_path / manifest.workspace_id / "source" / ".env").exists()
    assert "owner_hash" not in manifest.public_dict()
    assert "run_hash" not in manifest.public_dict()


def test_workspace_owner_isolation_and_confined_file_access(tmp_path):
    service = WorkspaceService(tmp_path)
    manifest = service.create_from_archive(
        _zip({"src/app.py": "value = 1"}),
        "project.zip",
        owner_id="owner-a",
        run_id="run-a",
    )
    files = service.files(manifest.workspace_id, owner_id="owner-a")
    result = files.read_file(str(tmp_path / manifest.workspace_id / "source" / "src" / "app.py"))
    assert "value = 1" in result.content

    with pytest.raises(WorkspaceServiceError, match="not found"):
        service.get(manifest.workspace_id, owner_id="owner-b")
    with pytest.raises(PermissionError):
        files.read_file(str(tmp_path / "outside.txt"))


def test_workspace_rejects_path_traversal_and_cleans_staging(tmp_path):
    service = WorkspaceService(tmp_path)
    with pytest.raises(WorkspaceServiceError, match="unsafe path"):
        service.create_from_archive(
            _zip({"../escape.py": "bad"}),
            "project.zip",
            owner_id="owner-a",
            run_id="run-a",
        )
    assert list(tmp_path.iterdir()) == []


def test_cleanup_expired_workspace(tmp_path):
    service = WorkspaceService(tmp_path)
    manifest = service.create_from_archive(
        _zip({"README.md": "hello"}), "project.zip", owner_id="owner-a", run_id="run-a"
    )
    path = tmp_path / manifest.workspace_id / ".aria-workspace.json"
    data = json.loads(path.read_text())
    data["expires_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data))
    assert service.cleanup_expired(datetime.now(timezone.utc)) == 1
    assert not (tmp_path / manifest.workspace_id).exists()


def test_get_rejects_and_removes_expired_workspace(tmp_path):
    service = WorkspaceService(tmp_path)
    manifest = service.create_from_archive(
        _zip({"README.md": "hello"}), "project.zip", owner_id="owner-a", run_id="run-a"
    )
    path = tmp_path / manifest.workspace_id / ".aria-workspace.json"
    data = json.loads(path.read_text())
    data["expires_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data))
    with pytest.raises(WorkspaceServiceError, match="not found"):
        service.get(manifest.workspace_id, owner_id="owner-a")
    assert not (tmp_path / manifest.workspace_id).exists()


def test_bound_read_tools_use_relative_paths_and_hide_host_root(tmp_path):
    service = WorkspaceService(tmp_path)
    manifest = service.create_from_archive(
        _zip({"src/app.py": "def hello():\n    return 'world'\n"}),
        "project.zip",
        owner_id="owner-a",
        run_id="run-a",
    )
    tools, schemas = build_workspace_read_tools(service, manifest.workspace_id, "owner-a")
    read = tools["workspace_read_file"][0]({"path": "src/app.py"})
    search = tools["workspace_search_code"][0]({"pattern": "hello", "file_glob": "**/*.py"})

    assert read["success"] is True
    assert read["path"] == "src/app.py"
    assert str(tmp_path) not in str(read)
    assert search["count"] == 1
    assert {schema["function"]["name"] for schema in schemas} == {
        "workspace_read_file", "workspace_list_files", "workspace_search_code"
    }


def test_bound_read_tools_reject_parent_escape(tmp_path):
    service = WorkspaceService(tmp_path)
    manifest = service.create_from_archive(
        _zip({"app.py": "print('ok')"}), "project.zip", owner_id="owner-a", run_id="run-a"
    )
    tools, _ = build_workspace_read_tools(service, manifest.workspace_id, "owner-a")
    result = tools["workspace_read_file"][0]({"path": "../.aria-workspace.json"})
    assert result["success"] is False
    assert "inside the workspace" in result["error"]


def test_proposal_tool_returns_diff_without_modifying_source(tmp_path):
    service = WorkspaceService(tmp_path)
    manifest = service.create_from_archive(
        _zip({"app.py": "value = 1\n"}), "project.zip", owner_id="owner-a", run_id="run-a"
    )
    tools, _ = build_workspace_proposal_tools(service, manifest.workspace_id, "owner-a")
    result = tools["workspace_propose_change"][0]({"path": "app.py", "content": "value = 2\n"})
    target = tmp_path / manifest.workspace_id / "source" / "app.py"
    assert result["success"] is True
    assert result["requires_approval"] is True
    assert "+value = 2" in result["diff"]
    assert target.read_text() == "value = 1\n"
