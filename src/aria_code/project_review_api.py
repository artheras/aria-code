"""Internal HTTP surface for Arthera to request Aria Code project reviews."""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from aria_code.workspace_export import export_workspace

from aria_code.project_review import MAX_ARCHIVE_BYTES, ProjectReviewError, review_project_archive
from aria_code.workspace_service import WorkspaceService, WorkspaceServiceError
from aria_code.workspace_changes import WorkspaceChangeError, WorkspaceChangeService
from aria_code.workspace_tools import build_workspace_read_tools

router = APIRouter(prefix="/v1", tags=["Project Review"])


def _workspaces() -> WorkspaceService:
    try:
        ttl_hours = int(os.getenv("ARIA_CODE_WORKSPACE_TTL_HOURS", "24"))
    except ValueError:
        ttl_hours = 24
    return WorkspaceService(ttl_hours=ttl_hours)


class ChangeProposalRequest(BaseModel):
    owner_id: str = Field(min_length=1, max_length=512)
    path: str = Field(min_length=1, max_length=1000)
    content: str = Field(max_length=1_048_576)
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class WorkspaceOwnerRequest(BaseModel):
    owner_id: str = Field(min_length=1, max_length=512)


class ExportFile(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    content: str = Field(max_length=1_048_576)


class WorkspaceExportRequest(WorkspaceOwnerRequest):
    overlays: list[ExportFile] = Field(default_factory=list, max_length=2000)


@router.post('/workspaces/{workspace_id}/export')
async def download_workspace(workspace_id: str, payload: WorkspaceExportRequest, authorization: str | None = Header(default=None)):
    _authorize(authorization)
    try:
        content = await run_in_threadpool(export_workspace, _workspaces(), workspace_id, payload.owner_id, [item.model_dump() for item in payload.overlays])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(content, media_type='application/zip', headers={'Content-Disposition': 'attachment; filename="aria-project.zip"'})


class WorkspaceReadRequest(WorkspaceOwnerRequest):
    path: str = Field(min_length=1, max_length=1000)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=400, ge=1, le=2000)


class WorkspaceListRequest(WorkspaceOwnerRequest):
    path: str = Field(default=".", min_length=1, max_length=1000)
    pattern: str = Field(default="*", min_length=1, max_length=200)


class WorkspaceSearchRequest(WorkspaceOwnerRequest):
    pattern: str = Field(min_length=1, max_length=500)
    path: str = Field(default=".", min_length=1, max_length=1000)
    file_glob: str = Field(default="**/*", min_length=1, max_length=200)


def _authorize(value: str | None) -> None:
    expected = os.getenv("ARIA_CODE_REVIEW_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Project review service authentication is not configured.")
    supplied = (value or "").removeprefix("Bearer ").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid project review service token.")


@router.post("/project-reviews")
async def create_project_review(
    project: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    payload = await project.read(MAX_ARCHIVE_BYTES + 1)
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail="Project archive exceeds the upload limit.")
    try:
        return review_project_archive(payload, project.filename or "project.zip")
    except ProjectReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/workspaces", status_code=201)
async def create_workspace(
    project: UploadFile = File(...),
    owner_id: str = Form(...),
    run_id: str = Form(...),
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    payload = await project.read(MAX_ARCHIVE_BYTES + 1)
    try:
        manifest = _workspaces().create_from_archive(
            payload,
            project.filename or "project.zip",
            owner_id=owner_id,
            run_id=run_id,
        )
        return manifest.public_dict()
    except WorkspaceServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}")
async def get_workspace(
    workspace_id: str,
    owner_id: str = Query(...),
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    try:
        return _workspaces().get(workspace_id, owner_id=owner_id).public_dict()
    except WorkspaceServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/workspaces/{workspace_id}", status_code=204)
async def delete_workspace(
    workspace_id: str,
    owner_id: str = Query(...),
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    try:
        _workspaces().delete(workspace_id, owner_id=owner_id)
    except WorkspaceServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _run_workspace_read_tool(workspace_id: str, owner_id: str, name: str, params: dict):
    tools, _ = build_workspace_read_tools(_workspaces(), workspace_id, owner_id)
    result = tools[name][0](params)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Workspace operation failed.")
    return result


@router.post("/workspaces/{workspace_id}/files/read")
async def read_workspace_file(
    workspace_id: str,
    payload: WorkspaceReadRequest,
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    return _run_workspace_read_tool(
        workspace_id, payload.owner_id, "workspace_read_file", payload.model_dump(exclude={"owner_id"})
    )


@router.post("/workspaces/{workspace_id}/files/list")
async def list_workspace_files(
    workspace_id: str,
    payload: WorkspaceListRequest,
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    return _run_workspace_read_tool(
        workspace_id, payload.owner_id, "workspace_list_files", payload.model_dump(exclude={"owner_id"})
    )


@router.post("/workspaces/{workspace_id}/search")
async def search_workspace_code(
    workspace_id: str,
    payload: WorkspaceSearchRequest,
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    return _run_workspace_read_tool(
        workspace_id, payload.owner_id, "workspace_search_code", payload.model_dump(exclude={"owner_id"})
    )


@router.post("/workspaces/{workspace_id}/changes", status_code=201)
async def propose_workspace_change(
    workspace_id: str,
    payload: ChangeProposalRequest,
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    try:
        service = WorkspaceChangeService(_workspaces())
        change = service.propose(
            workspace_id,
            owner_id=payload.owner_id,
            path=payload.path,
            content=payload.content,
            expected_sha256=payload.expected_sha256,
        )
        before_file = service._directory(workspace_id, change.change_id) / 'before.bin'
        before_available = before_file.stat().st_size <= 1_048_576
        return {**change.public_dict(), 'after_content': payload.content,
                'before_content': before_file.read_text(encoding='utf-8') if before_available else '',
                'before_content_available': before_available}
    except WorkspaceChangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/changes/{change_id}")
async def get_workspace_change(
    workspace_id: str,
    change_id: str,
    owner_id: str = Query(...),
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    try:
        return WorkspaceChangeService(_workspaces()).get(
            workspace_id, change_id, owner_id=owner_id
        ).public_dict()
    except WorkspaceChangeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/changes/{change_id}/apply")
async def apply_workspace_change(
    workspace_id: str,
    change_id: str,
    payload: WorkspaceOwnerRequest,
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    try:
        return WorkspaceChangeService(_workspaces()).apply(
            workspace_id, change_id, owner_id=payload.owner_id
        ).public_dict()
    except WorkspaceChangeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/changes/{change_id}/rollback")
async def rollback_workspace_change(
    workspace_id: str,
    change_id: str,
    payload: WorkspaceOwnerRequest,
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    try:
        return WorkspaceChangeService(_workspaces()).rollback(
            workspace_id, change_id, owner_id=payload.owner_id
        ).public_dict()
    except WorkspaceChangeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
