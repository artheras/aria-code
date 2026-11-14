"""Isolated, non-executing source workspaces for remote Aria Code runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from aria_code.project_review import (
    MAX_ARCHIVE_BYTES,
    MAX_EXTRACTED_BYTES,
    MAX_FILES,
    ProjectReviewError,
    _archive_entries,
    _is_sensitive,
    _safe_member_path,
    _should_ignore,
)
from aria_code.workspace import WorkspaceFiles, WorkspaceSecurity


_WORKSPACE_ID = re.compile(r"^ws_[a-f0-9]{24}$")


class WorkspaceServiceError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _subject_hash(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorkspaceManifest:
    schema_version: str
    workspace_id: str
    owner_hash: str
    run_hash: str
    archive_name: str
    file_count: int
    extracted_bytes: int
    skipped_sensitive_files: int
    created_at: str
    expires_at: str
    execution_enabled: bool = False
    network_enabled: bool = False

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("owner_hash", None)
        data.pop("run_hash", None)
        return data


class WorkspaceService:
    """Owns bounded project trees; never executes their contents."""

    def __init__(self, root: str | Path | None = None, ttl_hours: int = 24) -> None:
        configured = root or os.getenv("ARIA_CODE_WORKSPACE_ROOT")
        self.root = Path(configured or (Path.home() / ".aria" / "runtime" / "workspaces")).expanduser().resolve()
        self.ttl_hours = max(1, min(int(ttl_hours), 168))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    def _workspace_dir(self, workspace_id: str) -> Path:
        if not _WORKSPACE_ID.fullmatch(workspace_id):
            raise WorkspaceServiceError("Invalid workspace id.")
        target = (self.root / workspace_id).resolve()
        if target.parent != self.root:
            raise WorkspaceServiceError("Invalid workspace path.")
        return target

    @staticmethod
    def _manifest_path(directory: Path) -> Path:
        return directory / ".aria-workspace.json"

    def create_from_archive(self, payload: bytes, filename: str, *, owner_id: str, run_id: str) -> WorkspaceManifest:
        if not owner_id.strip() or not run_id.strip():
            raise WorkspaceServiceError("owner_id and run_id are required.")
        if not payload or len(payload) > MAX_ARCHIVE_BYTES:
            raise WorkspaceServiceError("Project archive is empty or exceeds the upload limit.")
        self.cleanup_expired()

        workspace_id = f"ws_{secrets.token_hex(12)}"
        final_dir = self._workspace_dir(workspace_id)
        staging = Path(tempfile.mkdtemp(prefix=f".{workspace_id}-", dir=self.root))
        source_root = staging / "source"
        source_root.mkdir(mode=0o700)
        count = total = skipped_sensitive = 0
        try:
            for raw_path, data in _archive_entries(payload, filename):
                relative = _safe_member_path(raw_path)
                if _should_ignore(relative):
                    continue
                if _is_sensitive(relative):
                    skipped_sensitive += 1
                    continue
                count += 1
                total += len(data)
                if count > MAX_FILES or total > MAX_EXTRACTED_BYTES:
                    raise WorkspaceServiceError("Expanded project exceeds workspace limits.")
                target = (source_root / Path(*relative.parts)).resolve()
                if source_root != target and source_root not in target.parents:
                    raise WorkspaceServiceError("Archive path escapes the workspace.")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_bytes(data)
                try:
                    target.chmod(0o600)
                except OSError:
                    pass
            if count == 0:
                raise WorkspaceServiceError("No safe project files were found.")
            created = _now()
            manifest = WorkspaceManifest(
                schema_version="aria.workspace.v1",
                workspace_id=workspace_id,
                owner_hash=_subject_hash(owner_id),
                run_hash=_subject_hash(run_id),
                archive_name=PurePosixPath(filename).name[:180],
                file_count=count,
                extracted_bytes=total,
                skipped_sensitive_files=skipped_sensitive,
                created_at=created.isoformat(),
                expires_at=(created + timedelta(hours=self.ttl_hours)).isoformat(),
            )
            self._manifest_path(staging).write_text(json.dumps(asdict(manifest), indent=2), encoding="utf-8")
            staging.rename(final_dir)
            return manifest
        except (ProjectReviewError, OSError) as exc:
            shutil.rmtree(staging, ignore_errors=True)
            if isinstance(exc, ProjectReviewError):
                raise WorkspaceServiceError(str(exc)) from exc
            raise WorkspaceServiceError("Could not create isolated workspace.") from exc
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def get(self, workspace_id: str, *, owner_id: str) -> WorkspaceManifest:
        directory = self._workspace_dir(workspace_id)
        try:
            raw = json.loads(self._manifest_path(directory).read_text(encoding="utf-8"))
            manifest = WorkspaceManifest(**raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("Workspace not found.") from exc
        if not secrets.compare_digest(manifest.owner_hash, _subject_hash(owner_id)):
            # Do not reveal whether another tenant owns the identifier.
            raise WorkspaceServiceError("Workspace not found.")
        try:
            expires = datetime.fromisoformat(str(manifest.expires_at))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= _now():
                shutil.rmtree(directory, ignore_errors=True)
                raise WorkspaceServiceError("Workspace not found.")
        except ValueError as exc:
            raise WorkspaceServiceError("Workspace not found.") from exc
        return manifest

    def files(self, workspace_id: str, *, owner_id: str) -> WorkspaceFiles:
        self.get(workspace_id, owner_id=owner_id)
        source_root = self._workspace_dir(workspace_id) / "source"
        return WorkspaceFiles(WorkspaceSecurity(cwd=source_root, allow_home=False))

    def source_path(self, workspace_id: str, relative_path: str, *, owner_id: str) -> Path:
        self.get(workspace_id, owner_id=owner_id)
        source_root = (self._workspace_dir(workspace_id) / "source").resolve()
        relative = PurePosixPath(str(relative_path or ".").replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise WorkspaceServiceError("Path must stay inside the workspace.")
        target = (source_root / Path(*relative.parts)).resolve()
        if target != source_root and source_root not in target.parents:
            raise WorkspaceServiceError("Path must stay inside the workspace.")
        return target

    def delete(self, workspace_id: str, *, owner_id: str) -> None:
        self.get(workspace_id, owner_id=owner_id)
        shutil.rmtree(self._workspace_dir(workspace_id))

    def cleanup_expired(self, now: datetime | None = None) -> int:
        threshold = now or _now()
        removed = 0
        for directory in self.root.glob("ws_*"):
            try:
                raw = json.loads(self._manifest_path(directory).read_text(encoding="utf-8"))
                expires = datetime.fromisoformat(str(raw["expires_at"]))
                if expires <= threshold:
                    shutil.rmtree(directory)
                    removed += 1
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
        return removed
