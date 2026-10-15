import io
import json
import zipfile

import pytest
from aria_code.workspace_export import export_workspace
from aria_code.workspace_service import WorkspaceService


def setup_workspace(tmp_path):
    source = io.BytesIO()
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('site/index.html', 'old')
        archive.writestr('site/photo.png', b'\x89PNG\x00\xff')
        archive.writestr('.env', 'SECRET=never-export')
    service = WorkspaceService(tmp_path)
    workspace = service.create_from_archive(source.getvalue(), 'site.zip', owner_id='alice', run_id='run')
    return service, workspace.workspace_id


def test_export_contains_binary_source_and_proposed_version_without_writing(tmp_path):
    service, workspace = setup_workspace(tmp_path)
    result = export_workspace(service, workspace, 'alice', [{'path': 'site/index.html', 'content': 'new'}, {'path': 'site/new.css', 'content': 'body{}'}])
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        assert archive.read('site/index.html') == b'new'
        assert archive.read('site/photo.png') == b'\x89PNG\x00\xff'
        assert archive.read('site/new.css') == b'body{}'
        assert '.env' not in archive.namelist()
        assert json.loads(archive.comment)['originalFilesWritten'] is False
    assert service.source_path(workspace, 'site/index.html', owner_id='alice').read_text() == 'old'


@pytest.mark.parametrize('path', ['../escape', '/outside', '.env', 'C:/outside'])
def test_export_rejects_unsafe_overlay(tmp_path, path):
    service, workspace = setup_workspace(tmp_path)
    with pytest.raises(ValueError):
        export_workspace(service, workspace, 'alice', [{'path': path, 'content': 'x'}])


def test_export_rejects_other_owner(tmp_path):
    service, workspace = setup_workspace(tmp_path)
    with pytest.raises(ValueError):
        export_workspace(service, workspace, 'bob', [])


def test_export_http_route_returns_an_authenticated_archive(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aria_code.project_review_api import router
    service, workspace = setup_workspace(tmp_path)
    monkeypatch.setenv('ARIA_CODE_WORKSPACE_ROOT', str(tmp_path))
    monkeypatch.setenv('ARIA_CODE_REVIEW_TOKEN', 'test-token')
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.post(f'/v1/workspaces/{workspace}/export', json={'owner_id': 'alice'}).status_code == 401
        result = client.post(f'/v1/workspaces/{workspace}/export', json={'owner_id': 'alice', 'overlays': [{'path': 'site/index.html', 'content': 'version two'}]}, headers={'Authorization': 'Bearer test-token'})
        assert result.status_code == 200
        assert result.headers['content-type'] == 'application/zip'
        with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
            assert archive.read('site/index.html') == b'version two'
        proposal = client.post(f'/v1/workspaces/{workspace}/changes', json={'owner_id': 'alice', 'path': 'site/index.html', 'content': 'new proposal'}, headers={'Authorization': 'Bearer test-token'})
        assert proposal.status_code == 201
        assert proposal.json()['before_content'] == 'old'
        assert proposal.json()['after_content'] == 'new proposal'
