"""Read-only project snapshots with proposed text overlays."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

from aria_code.project_review import MAX_EXTRACTED_BYTES, MAX_FILES, _is_sensitive, _safe_member_path, _should_ignore
from aria_code.workspace_service import WorkspaceService, WorkspaceServiceError


def export_workspace(service: WorkspaceService, workspace_id: str, owner_id: str, overlays: list[dict[str, str]]) -> bytes:
    manifest = service.get(workspace_id, owner_id=owner_id)
    root = service.source_path(workspace_id, '.', owner_id=owner_id)
    proposed: dict[str, bytes] = {}
    proposed_bytes = 0
    for item in overlays:
        relative = _safe_member_path(item['path'])
        if ':' in relative.parts[0] or any(ord(char) < 32 for char in item['path']):
            raise WorkspaceServiceError('Invalid export path.')
        if _is_sensitive(relative) or _should_ignore(relative):
            raise WorkspaceServiceError('Excluded files cannot be exported as proposals.')
        name = str(relative)
        if name in proposed:
            raise WorkspaceServiceError('Duplicate export path.')
        proposed[name] = item['content'].encode('utf-8')
        proposed_bytes += len(proposed[name])
        if proposed_bytes > 16 * 1024 * 1024:
            raise WorkspaceServiceError('Proposed export exceeds the 16 MB limit.')
        if len(proposed[name]) > 1_048_576:
            raise WorkspaceServiceError('Exported proposal exceeds the text file limit.')
    output = io.BytesIO()
    count = total = 0
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        def add(name: str, data: bytes) -> None:
            nonlocal count, total
            count += 1
            total += len(data)
            if count > MAX_FILES or total > MAX_EXTRACTED_BYTES:
                raise WorkspaceServiceError('Project snapshot exceeds export limits.')
            archive.writestr(name, data)

        for path in sorted(root.rglob('*')):
            if path.is_symlink():
                raise WorkspaceServiceError('Symbolic links cannot be exported.')
            if not path.is_file():
                continue
            name = path.relative_to(root).as_posix()
            relative = _safe_member_path(name)
            if _is_sensitive(relative) or _should_ignore(relative):
                continue
            safe_path = service.source_path(workspace_id, name, owner_id=owner_id)
            if safe_path.stat().st_size > MAX_EXTRACTED_BYTES - total:
                raise WorkspaceServiceError('Project snapshot exceeds export limits.')
            add(name, proposed.pop(name) if name in proposed else safe_path.read_bytes())
        for name, data in proposed.items():
            add(name, data)
        archive.comment = json.dumps({
            'schemaVersion': 1, 'workspaceId': workspace_id,
            'exportedAt': datetime.now(timezone.utc).isoformat(),
            'originalFilesWritten': False, 'verified': False,
            'excludedSensitiveFiles': manifest.skipped_sensitive_files,
            'scope': 'imported-source-with-proposed-overlays',
        }).encode('utf-8')
    return output.getvalue()
