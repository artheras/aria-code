"""SQLite-backed run and event persistence for the Aria agent runtime."""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .run_state import (
    ACTIVE_RUN_STATUSES,
    RunStatus,
    is_terminal,
    normalize_run_status,
    require_transition,
)
from aria_code.packages.aria_core.paths import aria_home


# Compared with every non-alphanumeric character removed and lowercased, so
# apiKey, api-key and API_KEY are all the same field.
_SECRET_KEYS = {
    "apikey",
    "accesstoken",
    "authtoken",
    "refreshtoken",
    "authorization",
    "cookie",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "clientsecret",
    "token",
}

# Credentials inside free text: prompts, errors, tool output, commands. The
# shapes are those exec_events.py already masks on screen and in CI logs.
_SECRET_TEXT = (
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|client[_-]?secret"
                r"|token|password|passwd|secret)(\s*[=:]\s*)[\"']?[^\s'\",;]+"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]+"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@"), r"\1[REDACTED]@"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}|\bgh[pousr]_[A-Za-z0-9]{12,}"
                r"|\bAIza[0-9A-Za-z_-]{20,}|\bya29\.[0-9A-Za-z_-]+"), "[REDACTED]"),
)


def _redact_text(value: str) -> str:
    for pattern, replacement in _SECRET_TEXT:
        value = pattern.sub(replacement, value)
    return value


def _default_database_path() -> Path:
    configured = os.getenv("ARIA_HOME")
    if configured:
        root = Path(configured).expanduser()
    else:
        legacy = aria_home()
        root = legacy if legacy.exists() else Path.home() / ".aria-code"
    return root / "runtime" / "runs.sqlite3"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            result[key] = "[REDACTED]" if normalized in _SECRET_KEYS else _redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(_redact(value), ensure_ascii=False, default=str, separators=(",", ":"))


def _json_loads(value: str | None) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {"value": loaded}
    except (TypeError, json.JSONDecodeError):
        return {}


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    session_id: str
    status: RunStatus
    prompt: str
    workspace: str
    provider: str
    parent_run_id: Optional[str]
    owner_pid: int
    owner_host: str
    created_at: float
    updated_at: float
    started_at: Optional[float]
    finished_at: Optional[float]
    error: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunEventRecord:
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    timestamp: float
    data: Dict[str, Any] = field(default_factory=dict)


class RunNotFoundError(KeyError):
    """Raised when a run identifier is not present in the durable store."""


class RunStore:
    """Durable source of truth for runs and their append-only event streams."""

    def __init__(self, database_path: Path | str | None = None) -> None:
        self.database_path = Path(database_path or _default_database_path()).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    parent_run_id TEXT,
                    status TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    owner_pid INTEGER NOT NULL,
                    owner_host TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    error TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(parent_run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS run_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(run_id, sequence),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_runs_session_updated
                    ON runs(session_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_runs_status_updated
                    ON runs(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_run_events_run_sequence
                    ON run_events(run_id, sequence);
                """
            )

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            session_id=row["session_id"],
            status=RunStatus(row["status"]),
            prompt=row["prompt"],
            workspace=row["workspace"],
            provider=row["provider"],
            parent_run_id=row["parent_run_id"],
            owner_pid=int(row["owner_pid"]),
            owner_host=row["owner_host"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=float(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
            error=row["error"],
            metadata=_json_loads(row["metadata_json"]),
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> RunEventRecord:
        return RunEventRecord(
            event_id=row["event_id"],
            run_id=row["run_id"],
            sequence=int(row["sequence"]),
            event_type=row["event_type"],
            timestamp=float(row["timestamp"]),
            data=_json_loads(row["data_json"]),
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        data: Dict[str, Any] | None = None,
        *,
        event_id: str | None = None,
        timestamp: float | None = None,
    ) -> RunEventRecord:
        sequence = int(connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0])
        record = RunEventRecord(
            event_id=event_id or uuid.uuid4().hex,
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            timestamp=timestamp or time.time(),
            data=dict(data or {}),
        )
        connection.execute(
            """INSERT INTO run_events
               (event_id, run_id, sequence, event_type, timestamp, data_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                record.event_id,
                record.run_id,
                record.sequence,
                record.event_type,
                record.timestamp,
                _json_dumps(record.data),
            ),
        )
        connection.execute(
            "UPDATE runs SET updated_at = ? WHERE run_id = ?",
            (record.timestamp, run_id),
        )
        return record

    def create_run(
        self,
        *,
        session_id: str,
        prompt: str,
        workspace: str,
        provider: str = "",
        parent_run_id: str | None = None,
        metadata: Dict[str, Any] | None = None,
        run_id: str | None = None,
        owner_pid: int | None = None,
        owner_host: str | None = None,
    ) -> RunRecord:
        now = time.time()
        identifier = run_id or uuid.uuid4().hex[:16]
        pid = int(owner_pid if owner_pid is not None else os.getpid())
        host = owner_host or socket.gethostname()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO runs
                   (run_id, session_id, parent_run_id, status, prompt, workspace,
                    provider, owner_pid, owner_host, created_at, updated_at,
                    started_at, finished_at, error, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, '', ?)""",
                (
                    identifier,
                    session_id,
                    parent_run_id,
                    RunStatus.QUEUED.value,
                    _redact_text(prompt),
                    workspace,
                    provider,
                    pid,
                    host,
                    now,
                    now,
                    _json_dumps(metadata or {}),
                ),
            )
            self._append_event(connection, identifier, "run_created", {
                "status": RunStatus.QUEUED.value,
                "provider": provider,
                "workspace": workspace,
            }, timestamp=now)
        record = self.get_run(identifier)
        assert record is not None
        return record

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._row_to_run(row) if row is not None else None

    def transition(
        self,
        run_id: str,
        target: RunStatus | str,
        *,
        reason: str = "",
        error: str | None = None,
        provider: str | None = None,
        data: Dict[str, Any] | None = None,
    ) -> RunRecord:
        destination = normalize_run_status(target)
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(run_id)
            current = RunStatus(row["status"])
            require_transition(current, destination)
            if current == destination:
                return self._row_to_run(row)

            started_at = row["started_at"]
            if started_at is None and destination in {RunStatus.PLANNING, RunStatus.RUNNING}:
                started_at = now
            finished_at = now if is_terminal(destination) else None
            if error is None and current is RunStatus.INTERRUPTED and destination in {
                RunStatus.PLANNING,
                RunStatus.RUNNING,
            }:
                next_error = ""
            else:
                next_error = row["error"] if error is None else _redact_text(str(error))
            next_provider = row["provider"] if provider is None else str(provider)
            connection.execute(
                """UPDATE runs
                   SET status = ?, provider = ?, updated_at = ?, started_at = ?,
                       finished_at = ?, error = ?
                   WHERE run_id = ?""",
                (
                    destination.value,
                    next_provider,
                    now,
                    started_at,
                    finished_at,
                    next_error,
                    run_id,
                ),
            )
            event_data = {
                "from": current.value,
                "to": destination.value,
                "reason": reason,
            }
            if data:
                event_data.update(data)
            self._append_event(connection, run_id, "run_state_changed", event_data, timestamp=now)
            updated = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._row_to_run(updated)

    def append_event(
        self,
        run_id: str,
        event_type: str,
        data: Dict[str, Any] | None = None,
        *,
        event_id: str | None = None,
        timestamp: float | None = None,
    ) -> RunEventRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if exists is None:
                raise RunNotFoundError(run_id)
            return self._append_event(
                connection,
                run_id,
                event_type,
                data,
                event_id=event_id,
                timestamp=timestamp,
            )

    def events(self, run_id: str) -> list[RunEventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM run_events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def list_runs(
        self,
        *,
        session_id: str | None = None,
        statuses: Iterable[RunStatus | str] | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        clauses = []
        values: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            values.append(session_id)
        normalized = [normalize_run_status(status).value for status in (statuses or [])]
        if normalized:
            clauses.append(f"status IN ({','.join('?' for _ in normalized)})")
            values.extend(normalized)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM runs{where} ORDER BY updated_at DESC LIMIT ?",
                values,
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def recover_orphaned_runs(self) -> list[str]:
        """Mark active runs from dead local processes as interrupted."""
        host = socket.gethostname()
        active_values = [status.value for status in ACTIVE_RUN_STATUSES]
        placeholders = ",".join("?" for _ in active_values)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT run_id, owner_pid FROM runs "
                f"WHERE owner_host = ? AND status IN ({placeholders})",
                [host, *active_values],
            ).fetchall()
        recovered = []
        for row in rows:
            if not self._pid_is_alive(int(row["owner_pid"])):
                self.transition(
                    row["run_id"],
                    RunStatus.INTERRUPTED,
                    reason="owner_process_missing",
                    error="The process ended before the run reached a terminal state.",
                )
                recovered.append(row["run_id"])
        return recovered
