"""Durable, conflict-aware file checkpoints for agent write operations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from .run_store import RunStore


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _json_loads(value: str | None) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {"value": loaded}
    except (TypeError, json.JSONDecodeError):
        return {}


@dataclass(frozen=True)
class CheckpointFile:
    checkpoint_id: str
    path: str
    existed_before: bool
    before_content: str
    after_content: str
    before_hash: str
    after_hash: str
    before_mode: Optional[int]


@dataclass(frozen=True)
class CheckpointRecord:
    checkpoint_id: str
    sequence: int
    run_id: Optional[str]
    session_id: str
    source: str
    label: str
    status: str
    created_at: float
    restored_at: Optional[float]
    metadata: Dict[str, Any] = field(default_factory=dict)
    files: tuple[CheckpointFile, ...] = ()


@dataclass(frozen=True)
class RestoreResult:
    checkpoint_ids: tuple[str, ...]
    run_id: Optional[str]
    restored_paths: tuple[str, ...]


class CheckpointNotFoundError(KeyError):
    """Raised when a checkpoint or checkpoint group cannot be found."""


class CheckpointConflictError(RuntimeError):
    """Raised when a file changed after the checkpoint was recorded."""


class CheckpointStore:
    """Store file pre-images and restore them only when hashes still match."""

    def __init__(self, database_path: Path | str | None = None) -> None:
        self.run_store = RunStore(database_path)
        self.database_path = self.run_store.database_path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    checkpoint_id TEXT NOT NULL UNIQUE,
                    run_id TEXT,
                    session_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    restored_at REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS checkpoint_files (
                    checkpoint_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    existed_before INTEGER NOT NULL,
                    before_content TEXT NOT NULL,
                    after_content TEXT NOT NULL,
                    before_hash TEXT NOT NULL,
                    after_hash TEXT NOT NULL,
                    before_mode INTEGER,
                    PRIMARY KEY(checkpoint_id, path),
                    FOREIGN KEY(checkpoint_id)
                        REFERENCES checkpoints(checkpoint_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_checkpoints_run_sequence
                    ON checkpoints(run_id, sequence DESC);
                CREATE INDEX IF NOT EXISTS idx_checkpoints_session_sequence
                    ON checkpoints(session_id, sequence DESC);
                CREATE INDEX IF NOT EXISTS idx_checkpoints_status_sequence
                    ON checkpoints(status, sequence DESC);
                """
            )

    @staticmethod
    def _row_to_file(row: sqlite3.Row) -> CheckpointFile:
        return CheckpointFile(
            checkpoint_id=row["checkpoint_id"],
            path=row["path"],
            existed_before=bool(row["existed_before"]),
            before_content=row["before_content"],
            after_content=row["after_content"],
            before_hash=row["before_hash"],
            after_hash=row["after_hash"],
            before_mode=int(row["before_mode"]) if row["before_mode"] is not None else None,
        )

    def _row_to_checkpoint(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> CheckpointRecord:
        file_rows = connection.execute(
            "SELECT * FROM checkpoint_files WHERE checkpoint_id = ? ORDER BY path",
            (row["checkpoint_id"],),
        ).fetchall()
        return CheckpointRecord(
            checkpoint_id=row["checkpoint_id"],
            sequence=int(row["sequence"]),
            run_id=row["run_id"],
            session_id=row["session_id"],
            source=row["source"],
            label=row["label"],
            status=row["status"],
            created_at=float(row["created_at"]),
            restored_at=float(row["restored_at"]) if row["restored_at"] is not None else None,
            metadata=_json_loads(row["metadata_json"]),
            files=tuple(self._row_to_file(file_row) for file_row in file_rows),
        )

    def record_change(
        self,
        *,
        path: Path | str,
        before_content: str,
        after_content: str,
        existed_before: bool,
        source: str,
        run_id: str | None = None,
        session_id: str = "",
        before_mode: int | None = None,
        label: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> CheckpointRecord:
        target = Path(path).expanduser().resolve()
        if not target.is_file():
            raise CheckpointConflictError(f"Written file is missing: {target}")
        current = target.read_text(encoding="utf-8", errors="replace")
        after_hash = _sha256(after_content)
        if _sha256(current) != after_hash:
            raise CheckpointConflictError(
                f"File changed before checkpoint could be recorded: {target}"
            )
        if run_id and self.run_store.get_run(run_id) is None:
            raise CheckpointNotFoundError(f"Unknown run id: {run_id}")

        checkpoint_id = uuid.uuid4().hex[:16]
        created_at = time.time()
        mode = before_mode
        if existed_before and mode is None:
            mode = stat.S_IMODE(target.stat().st_mode)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO checkpoints
                   (checkpoint_id, run_id, session_id, source, label, status,
                    created_at, restored_at, metadata_json)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, NULL, ?)""",
                (
                    checkpoint_id,
                    run_id,
                    session_id,
                    source,
                    label,
                    created_at,
                    _json_dumps(metadata or {}),
                ),
            )
            connection.execute(
                """INSERT INTO checkpoint_files
                   (checkpoint_id, path, existed_before, before_content,
                    after_content, before_hash, after_hash, before_mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint_id,
                    str(target),
                    int(existed_before),
                    before_content,
                    after_content,
                    _sha256(before_content),
                    after_hash,
                    mode,
                ),
            )
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()

        if run_id:
            self.run_store.append_event(run_id, "checkpoint_created", {
                "checkpoint_id": checkpoint_id,
                "path": str(target),
                "source": source,
            })
        assert row is not None
        with self._connect() as connection:
            return self._row_to_checkpoint(connection, row)

    def get(self, checkpoint_id: str) -> Optional[CheckpointRecord]:
        key = (checkpoint_id or "").strip()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ?",
                (key,),
            ).fetchone()
            if row is None and key:
                rows = connection.execute(
                    "SELECT * FROM checkpoints WHERE checkpoint_id LIKE ? ORDER BY sequence DESC LIMIT 2",
                    (f"{key}%",),
                ).fetchall()
                row = rows[0] if len(rows) == 1 else None
            return self._row_to_checkpoint(connection, row) if row is not None else None

    def list(
        self,
        *,
        run_id: str | None = None,
        session_id: str | None = None,
        status: str | None = "active",
        limit: int = 50,
    ) -> list[CheckpointRecord]:
        clauses = []
        values: list[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            values.append(run_id)
        if session_id:
            clauses.append("session_id = ?")
            values.append(session_id)
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM checkpoints{where} ORDER BY sequence DESC LIMIT ?",
                values,
            ).fetchall()
            return [self._row_to_checkpoint(connection, row) for row in rows]

    def latest_run_id(self, *, session_id: str | None = None) -> Optional[str]:
        clauses = ["status = 'active'", "run_id IS NOT NULL"]
        values: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            values.append(session_id)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT run_id FROM checkpoints WHERE {' AND '.join(clauses)} "
                "ORDER BY sequence DESC LIMIT 1",
                values,
            ).fetchone()
        return row["run_id"] if row is not None else None

    @staticmethod
    def _snapshot(path: Path) -> tuple[bool, str, Optional[int]]:
        if not path.exists():
            return False, "", None
        if not path.is_file():
            raise CheckpointConflictError(f"Checkpoint path is not a file: {path}")
        return (
            True,
            path.read_text(encoding="utf-8", errors="replace"),
            stat.S_IMODE(path.stat().st_mode),
        )

    @staticmethod
    def _write_atomic(path: Path, content: str, mode: int | None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.aria-rewind-{uuid.uuid4().hex[:8]}")
        try:
            temporary.write_text(content, encoding="utf-8")
            if mode is not None:
                temporary.chmod(mode)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _restore(self, checkpoints: Iterable[CheckpointRecord]) -> RestoreResult:
        records = list(checkpoints)
        if not records:
            raise CheckpointNotFoundError("No active checkpoints to restore")
        files = [file for record in records for file in record.files]
        if not files:
            raise CheckpointNotFoundError("Checkpoint contains no files")

        simulated: Dict[str, tuple[bool, str]] = {}
        backups: Dict[str, tuple[bool, str, Optional[int]]] = {}
        for file in files:
            path = Path(file.path)
            if file.path not in simulated:
                exists, content, mode = self._snapshot(path)
                backups[file.path] = (exists, content, mode)
                simulated[file.path] = (exists, _sha256(content))
            exists, current_hash = simulated[file.path]
            if not exists or current_hash != file.after_hash:
                raise CheckpointConflictError(
                    f"Refusing to overwrite a file changed after the checkpoint: {path}"
                )
            simulated[file.path] = (file.existed_before, file.before_hash)

        restored_paths: list[str] = []
        try:
            for file in files:
                path = Path(file.path)
                exists, current, _ = self._snapshot(path)
                if not exists or _sha256(current) != file.after_hash:
                    raise CheckpointConflictError(
                        f"File changed while rewind was running: {path}"
                    )
                if file.existed_before:
                    self._write_atomic(path, file.before_content, file.before_mode)
                else:
                    path.unlink()
                if file.path not in restored_paths:
                    restored_paths.append(file.path)
        except Exception:
            for path_text, (existed, content, mode) in backups.items():
                path = Path(path_text)
                if existed:
                    self._write_atomic(path, content, mode)
                elif path.exists() and path.is_file():
                    path.unlink()
            raise

        restored_at = time.time()
        checkpoint_ids = tuple(record.checkpoint_id for record in records)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "UPDATE checkpoints SET status = 'restored', restored_at = ? WHERE checkpoint_id = ?",
                [(restored_at, checkpoint_id) for checkpoint_id in checkpoint_ids],
            )

        run_id = records[0].run_id if all(
            record.run_id == records[0].run_id for record in records
        ) else None
        if run_id:
            self.run_store.append_event(run_id, "checkpoint_restored", {
                "checkpoint_ids": list(checkpoint_ids),
                "paths": restored_paths,
            })
        return RestoreResult(checkpoint_ids, run_id, tuple(restored_paths))

    def max_sequence(self) -> int:
        """The newest checkpoint's sequence; a later rewind undoes everything after it."""
        with self._connect() as connection:
            row = connection.execute("SELECT MAX(sequence) AS top FROM checkpoints").fetchone()
        return int(row["top"] or 0)

    def since(self, sequence: int, *, session_id: str | None = None) -> list[CheckpointRecord]:
        """Active checkpoints recorded after ``sequence``, newest first."""
        clauses = ["status = 'active'", "sequence > ?"]
        values: list[Any] = [int(sequence)]
        if session_id:
            clauses.append("session_id = ?")
            values.append(session_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM checkpoints WHERE {' AND '.join(clauses)} ORDER BY sequence DESC",
                values,
            ).fetchall()
            return [self._row_to_checkpoint(connection, row) for row in rows]

    def restore_since(
        self,
        sequence: int,
        *,
        session_id: str | None = None,
        skip: Callable[[str], bool] = lambda _path: False,
    ) -> RestoreResult:
        """Undo every change recorded after ``sequence``, newest first, all or nothing.

        ``skip`` drops files that no longer have a place to be restored to —
        an edit inside a task worktree that has since been removed, whose
        effect on the user's files has its own checkpoint. Nothing to undo is
        not an error: a point with no file changes after it rewinds to itself.
        """
        records = self.since(sequence, session_id=session_id)
        filtered = [replace(record, files=tuple(f for f in record.files if not skip(f.path)))
                    for record in records]
        if any(record.files for record in filtered):
            return self._restore(filtered)
        if records:
            restored_at = time.time()
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    "UPDATE checkpoints SET status = 'restored', restored_at = ? WHERE checkpoint_id = ?",
                    [(restored_at, record.checkpoint_id) for record in records],
                )
        return RestoreResult(tuple(record.checkpoint_id for record in records), None, ())

    def restore_checkpoint(self, checkpoint_id: str) -> RestoreResult:
        record = self.get(checkpoint_id)
        if record is None or record.status != "active":
            raise CheckpointNotFoundError(checkpoint_id)
        return self._restore([record])

    def restore_run(self, run_id: str) -> RestoreResult:
        records = self.list(run_id=run_id, status="active", limit=10_000)
        if not records:
            raise CheckpointNotFoundError(run_id)
        return self._restore(records)

    def restore_latest_run(self, *, session_id: str | None = None) -> RestoreResult:
        run_id = self.latest_run_id(session_id=session_id)
        if not run_id:
            raise CheckpointNotFoundError("No active run checkpoint found")
        return self.restore_run(run_id)

    def restore_latest(self, *, session_id: str | None = None) -> RestoreResult:
        clauses = ["status = 'active'"]
        values: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            values.append(session_id)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT checkpoint_id, run_id FROM checkpoints "
                f"WHERE {' AND '.join(clauses)} ORDER BY sequence DESC LIMIT 1",
                values,
            ).fetchone()
        if row is None:
            raise CheckpointNotFoundError("No active checkpoint found")
        if row["run_id"]:
            return self.restore_run(row["run_id"])
        return self.restore_checkpoint(row["checkpoint_id"])
