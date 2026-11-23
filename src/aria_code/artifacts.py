"""Local artifact paths for reports, charts, projects, and strategy output."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


def slugify_topic(topic: Optional[str], fallback: str = "general") -> str:
    raw = str(topic or "").strip()
    if not raw:
        raw = fallback
    raw = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "-", raw)
    raw = raw.strip("-._")
    return raw[:80] or fallback


@dataclass(frozen=True)
class ArtifactRecord:
    """Resolved paths for one generated artifact bundle."""

    category: str
    topic: str
    directory: Path
    path: Path
    metadata_path: Path
    raw_data_path: Path


@dataclass(frozen=True)
class ArtifactEntry:
    """Parsed artifact metadata with resolved paths and file state."""

    metadata_path: Path
    path: Path
    raw_data_path: Path
    kind: str
    status: str
    topic: str
    created_at: str
    mtime: float
    size_bytes: int


def _safe_stat(path: Path) -> Optional[os.stat_result]:
    try:
        return path.stat()
    except Exception:
        return None


def _artifact_entry_from_metadata(path: Path) -> Optional[ArtifactEntry]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        artifact = data.get("artifact") or {}
        output_path = Path(str(artifact.get("path") or ""))
        metadata_path = Path(str(artifact.get("metadata_path") or path))
        raw_path = Path(str(artifact.get("raw_data_path") or path.with_suffix(".raw_data.json")))
        output_stat = _safe_stat(output_path)
        meta_stat = _safe_stat(metadata_path)
        raw_stat = _safe_stat(raw_path)
        mtime = 0.0
        for stat in (output_stat, meta_stat, raw_stat):
            if stat is not None:
                mtime = max(mtime, float(stat.st_mtime))
        size_bytes = 0
        for stat in (output_stat, meta_stat, raw_stat):
            if stat is not None:
                size_bytes += int(stat.st_size)
        return ArtifactEntry(
            metadata_path=metadata_path,
            path=output_path,
            raw_data_path=raw_path,
            kind=str(data.get("kind") or artifact.get("category") or "artifact"),
            status=str(data.get("status") or "unknown"),
            topic=str(artifact.get("topic") or data.get("symbol") or ""),
            created_at=str(data.get("created_at") or ""),
            mtime=mtime or float(meta_stat.st_mtime if meta_stat else 0),
            size_bytes=size_bytes,
        )
    except Exception:
        return None


def _cleanup_empty_dirs(start: Path, stop: Path) -> None:
    current = start
    try:
        stop = stop.resolve()
    except Exception:
        return
    while True:
        try:
            current = current.resolve()
        except Exception:
            break
        if current == stop or stop not in current.parents:
            break
        try:
            current.rmdir()
        except Exception:
            break
        current = current.parent


def _project_artifact_root() -> Optional[Path]:
    """Return project-level artifact root from .ariarc when configured."""
    try:
        from aria_code.ariarc import AriaRC

        rc = AriaRC.load()
        source = rc.source_path
        if not source:
            return None
        data = getattr(rc, "_data", None)
        if not isinstance(data, dict):
            text = source.read_text(encoding="utf-8")
            import json as _json
            import re as _re
            text = _re.sub(r"/\*.*?\*/", "", text, flags=_re.DOTALL)
            text = _re.sub(r'(?<!:)(?<!https)//[^\n]*', "", text)
            data = _json.loads(text)
        configured = data.get("artifact_root") or data.get("output_dir")
        if configured:
            path = Path(str(configured)).expanduser()
            if not path.is_absolute():
                path = source.parent / path
            return path
        return source.parent / "aria-output"
    except Exception:
        return None
    return None


def artifact_root() -> Path:
    """Return the per-user local artifact root.

    Override with ARIA_ARTIFACT_ROOT when a user wants reports/projects under a
    specific workspace. Defaults to a product-owned folder under that user's
    Documents directory, not the developer's Arthera repo.
    """
    configured = os.getenv("ARIA_ARTIFACT_ROOT")
    if configured:
        return Path(configured).expanduser()
    project_root = _project_artifact_root()
    if project_root:
        return project_root
    return Path.home() / "Documents" / "Aria Code"


def user_output_root() -> Path:
    """Return a user-owned output root that never falls back to project cwd.

    Use this for generated code, strategies, and project scaffolds. Reports and
    charts may still honor project `.ariarc` via `artifact_root()`, but user
    code should not silently land in the Aria source checkout.
    """
    configured = os.getenv("ARIA_USER_OUTPUT_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Documents" / "Aria Code"


def user_projects_dir(create: bool = True) -> Path:
    path = user_output_root() / "projects"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def user_generated_dir(create: bool = True) -> Path:
    path = user_output_root() / "generated"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def artifact_roots(*, include_user_generated: bool = True) -> list[Path]:
    """Return unique artifact roots that should be visible to users."""
    roots: list[Path] = []
    for root in (artifact_root(), user_generated_dir(create=False) if include_user_generated else None):
        if root is None:
            continue
        try:
            resolved = root.expanduser().resolve()
        except Exception:
            resolved = root.expanduser()
        if not any(existing == resolved for existing in roots):
            roots.append(resolved)
    return roots


def artifact_dir(category: str, topic: Optional[str] = None, create: bool = True) -> Path:
    parts = [slugify_topic(part) for part in str(category or "artifacts").split("/") if part]
    base = artifact_root().joinpath(*parts)
    if topic:
        base = base / slugify_topic(topic)
    if create:
        base.mkdir(parents=True, exist_ok=True)
    return base


def create_artifact(
    category: str,
    topic: Optional[str],
    stem: str,
    suffix: str,
    *,
    timestamp: Optional[datetime] = None,
    create: bool = True,
) -> ArtifactRecord:
    """Create a dated artifact bundle path and its sidecar metadata paths.

    Output layout:

        <root>/<category>/<topic>/YYYY-MM-DD/<HHMMSS>_<stem><suffix>
        <root>/<category>/<topic>/YYYY-MM-DD/<HHMMSS>_<stem>.metadata.json
        <root>/<category>/<topic>/YYYY-MM-DD/<HHMMSS>_<stem>.raw_data.json
    """

    ts = timestamp or datetime.now()
    topic_slug = slugify_topic(topic)
    stem_slug = slugify_topic(stem, fallback="artifact")
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    directory = artifact_dir(category, topic_slug, create=False) / ts.strftime("%Y-%m-%d")
    prefix = ts.strftime("%H%M%S")
    base_name = f"{prefix}_{stem_slug}"
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return ArtifactRecord(
        category=category,
        topic=topic_slug,
        directory=directory,
        path=directory / f"{base_name}{suffix}",
        metadata_path=directory / f"{base_name}.metadata.json",
        raw_data_path=directory / f"{base_name}.raw_data.json",
    )


def create_user_artifact(
    category: str,
    topic: Optional[str],
    stem: str,
    suffix: str,
    *,
    timestamp: Optional[datetime] = None,
    create: bool = True,
) -> ArtifactRecord:
    """Create an artifact under the user-owned output root.

    Unlike `create_artifact`, this intentionally ignores project `.ariarc`
    output settings. Use it for charts, scripts, and generated assets the user
    expects to find in their local Aria output folder rather than the source
    checkout.
    """
    ts = timestamp or datetime.now()
    parts = [slugify_topic(part) for part in str(category or "generated").split("/") if part]
    topic_slug = slugify_topic(topic)
    stem_slug = slugify_topic(stem, fallback="artifact")
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    directory = user_generated_dir(create=False).joinpath(*parts, topic_slug, ts.strftime("%Y-%m-%d"))
    prefix = ts.strftime("%H%M%S")
    base_name = f"{prefix}_{stem_slug}"
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return ArtifactRecord(
        category=f"generated/{'/'.join(parts)}" if parts else "generated",
        topic=topic_slug,
        directory=directory,
        path=directory / f"{base_name}{suffix}",
        metadata_path=directory / f"{base_name}.metadata.json",
        raw_data_path=directory / f"{base_name}.raw_data.json",
    )


def write_artifact_metadata(record: ArtifactRecord, metadata: Dict[str, Any]) -> Path:
    payload = {
        "artifact": {
            "category": record.category,
            "topic": record.topic,
            "path": str(record.path),
            "metadata_path": str(record.metadata_path),
            "raw_data_path": str(record.raw_data_path),
        },
        **metadata,
    }
    record.metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return record.metadata_path


def write_artifact_raw_data(record: ArtifactRecord, data: Any) -> Path:
    record.raw_data_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return record.raw_data_path


_ARTIFACT_KINDS_BY_SUFFIX = {
    ".md": "markdown_document",
    ".markdown": "markdown_document",
    ".html": "html_document",
    ".htm": "html_document",
    ".pdf": "pdf_document",
    ".docx": "word_document",
    ".xlsx": "spreadsheet",
    ".xls": "spreadsheet",
    ".csv": "data_file",
    ".json": "data_file",
    ".py": "code_file",
    ".ipynb": "notebook",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".svg": "image",
}


def register_existing_artifact(
    path: Path | str,
    *,
    kind: str = "",
    topic: str = "",
    status: str = "complete",
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[ArtifactRecord]:
    """Register a generated file so it appears in the unified artifact list.

    Only files under :func:`user_generated_dir` are registered.  This keeps
    ordinary source-code edits out of the user's generated-output inventory.
    Registration is best-effort and never changes the generated file itself.
    """
    try:
        output_path = Path(path).expanduser().resolve()
        generated_root = user_generated_dir(create=False).expanduser().resolve()
        output_path.relative_to(generated_root)
    except (OSError, RuntimeError, ValueError):
        return None

    lowered_name = output_path.name.lower()
    if lowered_name.endswith((".metadata.json", ".raw_data.json")):
        return None

    record = ArtifactRecord(
        category="generated/files",
        topic=slugify_topic(topic or output_path.stem),
        directory=output_path.parent,
        path=output_path,
        # Keep the output extension in sidecar names so report.md and
        # report.html can coexist without overwriting each other's metadata.
        metadata_path=output_path.with_name(f"{output_path.name}.metadata.json"),
        raw_data_path=output_path.with_name(f"{output_path.name}.raw_data.json"),
    )
    payload: Dict[str, Any] = {
        "kind": kind or _ARTIFACT_KINDS_BY_SUFFIX.get(output_path.suffix.lower(), "generated_file"),
        "status": status,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if metadata:
        payload.update(metadata)
    write_artifact_metadata(record, payload)
    return record


def recent_artifacts(limit: int = 20, root: Optional[Path] = None) -> list[Dict[str, Any]]:
    """Return recent artifact metadata records, newest first."""
    base = root or artifact_root()
    if not base.exists():
        return []
    items: list[Dict[str, Any]] = []
    for path in base.rglob("*.metadata.json"):
        entry = _artifact_entry_from_metadata(path)
        if entry is None:
            continue
        items.append({
            "kind": entry.kind,
            "status": entry.status,
            "topic": entry.topic,
            "path": str(entry.path) if str(entry.path) else "",
            "metadata_path": str(entry.metadata_path),
            "raw_data_path": str(entry.raw_data_path),
            "created_at": entry.created_at,
            "mtime": entry.mtime,
            "size_bytes": entry.size_bytes,
        })
    items.sort(key=lambda item: item.get("mtime") or 0, reverse=True)
    return items[: max(0, int(limit))]


def recent_artifacts_all(limit: int = 20) -> list[Dict[str, Any]]:
    """Return recent artifacts across project and user-generated roots."""
    items: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for root in artifact_roots(include_user_generated=True):
        for item in recent_artifacts(limit=max(limit, 20), root=root):
            marker = str(item.get("metadata_path") or item.get("path") or "")
            if marker and marker in seen:
                continue
            if marker:
                seen.add(marker)
            item = dict(item)
            item["root"] = str(root)
            items.append(item)
    items.sort(key=lambda item: item.get("mtime") or 0, reverse=True)
    return items[: max(0, int(limit))]


def artifact_summary(root: Optional[Path] = None) -> Dict[str, Any]:
    """Return a lightweight inventory of artifacts under root."""
    base = root or artifact_root()
    if not base.exists():
        return {
            "root": str(base),
            "total": 0,
            "total_size_bytes": 0,
            "by_kind": {},
            "newest_mtime": 0.0,
            "oldest_mtime": 0.0,
        }
    entries: list[ArtifactEntry] = []
    for path in base.rglob("*.metadata.json"):
        entry = _artifact_entry_from_metadata(path)
        if entry is not None:
            entries.append(entry)
    by_kind: Dict[str, int] = {}
    newest = 0.0
    oldest = 0.0
    total_size = 0
    for entry in entries:
        by_kind[entry.kind] = by_kind.get(entry.kind, 0) + 1
        total_size += entry.size_bytes
        if entry.mtime:
            newest = max(newest, entry.mtime)
            oldest = entry.mtime if not oldest else min(oldest, entry.mtime)
    return {
        "root": str(base),
        "total": len(entries),
        "total_size_bytes": total_size,
        "by_kind": dict(sorted(by_kind.items(), key=lambda item: (-item[1], item[0]))),
        "newest_mtime": newest,
        "oldest_mtime": oldest,
    }


def artifact_summary_all() -> Dict[str, Any]:
    """Return artifact inventory across project and user-generated roots."""
    roots = artifact_roots(include_user_generated=True)
    summaries = [artifact_summary(root) for root in roots]
    by_kind: Dict[str, int] = {}
    total = 0
    total_size = 0
    newest = 0.0
    oldest = 0.0
    for summary in summaries:
        total += int(summary.get("total") or 0)
        total_size += int(summary.get("total_size_bytes") or 0)
        newest = max(newest, float(summary.get("newest_mtime") or 0))
        old = float(summary.get("oldest_mtime") or 0)
        if old:
            oldest = old if not oldest else min(oldest, old)
        for kind, count in (summary.get("by_kind") or {}).items():
            by_kind[str(kind)] = by_kind.get(str(kind), 0) + int(count)
    return {
        "roots": [str(root) for root in roots],
        "total": total,
        "total_size_bytes": total_size,
        "by_kind": dict(sorted(by_kind.items(), key=lambda item: (-item[1], item[0]))),
        "newest_mtime": newest,
        "oldest_mtime": oldest,
        "summaries": summaries,
    }


def prune_artifacts(keep: int = 20, root: Optional[Path] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Delete artifact bundles older than the newest `keep` entries."""
    base = root or artifact_root()
    keep = max(0, int(keep))
    if not base.exists():
        return {
            "root": str(base),
            "keep": keep,
            "scanned": 0,
            "removed": 0,
            "dry_run": dry_run,
            "deleted": [],
        }
    entries: list[ArtifactEntry] = []
    for path in base.rglob("*.metadata.json"):
        entry = _artifact_entry_from_metadata(path)
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda entry: entry.mtime, reverse=True)
    to_remove = entries[keep:]
    deleted: list[Dict[str, Any]] = []
    for entry in to_remove:
        targets = [entry.path, entry.metadata_path, entry.raw_data_path]
        removed_files: list[str] = []
        if not dry_run:
            for target in targets:
                try:
                    if target.exists():
                        target.unlink()
                        removed_files.append(str(target))
                except Exception:
                    continue
            for target in targets:
                if target.exists():
                    continue
                _cleanup_empty_dirs(target.parent, base)
        else:
            removed_files = [str(target) for target in targets if target.exists()]
        deleted.append(
            {
                "kind": entry.kind,
                "status": entry.status,
                "topic": entry.topic,
                "metadata_path": str(entry.metadata_path),
                "path": str(entry.path) if str(entry.path) else "",
                "removed_files": removed_files,
            }
        )
    return {
        "root": str(base),
        "keep": keep,
        "scanned": len(entries),
        "removed": len(deleted),
        "dry_run": dry_run,
        "deleted": deleted,
    }


def prune_artifacts_all(keep: int = 20, dry_run: bool = False) -> Dict[str, Any]:
    """Prune artifact bundles in every visible artifact root."""
    results = [prune_artifacts(keep=keep, root=root, dry_run=dry_run) for root in artifact_roots(include_user_generated=True)]
    deleted: list[Dict[str, Any]] = []
    for result in results:
        root = result.get("root") or ""
        for item in result.get("deleted") or []:
            item = dict(item)
            item["root"] = root
            deleted.append(item)
    return {
        "roots": [result.get("root") for result in results],
        "keep": max(0, int(keep)),
        "scanned": sum(int(result.get("scanned") or 0) for result in results),
        "removed": sum(int(result.get("removed") or 0) for result in results),
        "dry_run": dry_run,
        "deleted": deleted,
        "results": results,
    }
