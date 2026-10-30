"""Host-bound task reads on the existing RunStore, not a network auth service."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .run_store import RunNotFoundError, RunStore


@dataclass(frozen=True)
class RunGrant:
    """Construct only after host authentication and project/session authorization.

    Never deserialize this grant from model arguments, HTTP bodies or headers.
    Direct access to the underlying RunStore remains a trusted-host capability.
    """
    organization: str
    project: str
    subject: str
    session: str
    workspace: str

    def __post_init__(self):
        for value in (self.organization, self.project, self.subject, self.session):
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError('Invalid host scope')
        path = Path(self.workspace).resolve(strict=True)
        if not path.is_dir():
            raise ValueError('Workspace must be a directory')
        object.__setattr__(self, 'workspace', str(path))

    def values(self):
        return (self.organization, self.project, self.subject, self.session, self.workspace)


class ScopedRuns:
    """A session-owner-only adapter; does not execute or resume queued records.

    Missing and out-of-scope IDs share one error. Bindings are immutable. Legacy
    runs with no binding are denied, even when their metadata claims ownership.
    """
    def __init__(self, store: RunStore, grant: RunGrant):
        self._store = store
        self._grant = grant
        with store._connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS run_access (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
                organization TEXT NOT NULL, project TEXT NOT NULL,
                subject TEXT NOT NULL, session TEXT NOT NULL, workspace TEXT NOT NULL
            )''')
            connection.execute('''CREATE INDEX IF NOT EXISTS idx_run_access_scope
                ON run_access(organization, project, subject, session, workspace)''')

    def create(self, prompt: str) -> dict:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 65536:
            raise ValueError('Prompt must be 1..65536 UTF-8 bytes')
        run = self._store.create_run(session_id=self._grant.session, prompt=prompt,
                                     workspace=self._grant.workspace)
        # A failure between these commits leaves an unbound record, which this
        # adapter denies. Never infer a binding from existing run metadata.
        with self._store._connect() as connection:
            connection.execute('INSERT INTO run_access VALUES (?, ?, ?, ?, ?, ?)',
                               (run.run_id, *self._grant.values()))
        return self.get(run.run_id)

    def _require(self, run_id):
        if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
            raise RunNotFoundError('Task not found')
        with self._store._connect() as connection:
            row = connection.execute('''SELECT run_id FROM run_access WHERE run_id=?
                AND organization=? AND project=? AND subject=? AND session=? AND workspace=?''',
                (run_id, *self._grant.values())).fetchone()
        if row is None:
            raise RunNotFoundError('Task not found')

    @staticmethod
    def _view(run):
        return {'version': 1, 'run_id': run.run_id, 'execution_state': run.status.value,
                'verification_state': 'unverified', 'created_at': run.created_at,
                'updated_at': run.updated_at, 'finished_at': run.finished_at}

    def get(self, run_id: str) -> dict:
        self._require(run_id)
        record = self._store.get_run(run_id)
        if record is None:
            raise RunNotFoundError('Task not found')
        return self._view(record)

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Limit must be an integer from 1 to 200')
        return limit

    def list(self, *, limit: int = 50) -> list[dict]:
        self._limit(limit)
        with self._store._connect() as connection:
            rows = connection.execute('''SELECT r.* FROM runs r JOIN run_access a ON r.run_id=a.run_id
                WHERE a.organization=? AND a.project=? AND a.subject=? AND a.session=? AND a.workspace=?
                ORDER BY r.updated_at DESC, r.run_id LIMIT ?''',
                (*self._grant.values(), limit)).fetchall()
        return [self._view(self._store._row_to_run(row)) for row in rows]

    def events(self, run_id: str, *, after: int = 0, limit: int = 50) -> list[dict]:
        self._require(run_id)
        self._limit(limit)
        if type(after) is not int or after < 0:
            raise ValueError('Cursor must be a nonnegative integer')
        with self._store._connect() as connection:
            rows = connection.execute('''SELECT sequence, event_type, timestamp FROM run_events
                WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?''',
                (run_id, after, limit)).fetchall()
        return [dict(row) for row in rows]
