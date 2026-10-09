"""Durable, local task ledger for background agents.

The live scheduler intentionally remains in-memory, but an agent task must not
disappear merely because the CLI is restarted.  This small JSON ledger records
the user-visible task contract, state, handoff and evidence.  It contains no
credentials and is safe to inspect locally.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterable, Mapping


LEDGER_VERSION = 1
MAX_RESULT_CHARS = 32_000


def default_ledger_path() -> Path:
    override = os.getenv("ARIA_TASK_LEDGER_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    root = Path(os.getenv("ARIA_CONFIG_DIR") or (Path.home() / ".aria"))
    return root / "task_ledger.json"


class TaskLedger:
    """Atomic JSON persistence for task snapshots.

    The ledger stores a mapping rather than an append-only log because callers
    need fast status restoration after a restart.  ``updated_at`` remains an
    auditable ordering signal.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or default_ledger_path()).expanduser()

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            records = payload.get("tasks", {}) if isinstance(payload, Mapping) else {}
            return {
                str(task_id): dict(record)
                for task_id, record in records.items()
                if isinstance(record, Mapping)
            }
        except (OSError, ValueError, TypeError):
            return {}

    def save(self, records: Mapping[str, Mapping[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": LEDGER_VERSION,
            "updated_at": time.time(),
            "tasks": records,
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        fd, temp_name = tempfile.mkstemp(prefix=".task_ledger_", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def upsert(self, record: Mapping[str, Any]) -> None:
        self.upsert_many((record,))

    def upsert_many(self, records: Iterable[Mapping[str, Any]]) -> None:
        """Persist a batch with one read and one atomic replacement.

        Validate the entire batch before touching disk. Restoring interrupted
        tasks at startup must not rewrite the full ledger for every task.
        """
        snapshots = {}
        updated_at = time.time()
        for record in records:
            task_id = str(record.get("task_id") or "").strip()
            if not task_id:
                raise ValueError("Task record requires task_id")
            snapshot = dict(record)
            snapshot["updated_at"] = updated_at
            result = str(snapshot.get("result") or "")
            if len(result) > MAX_RESULT_CHARS:
                snapshot["result"] = result[:MAX_RESULT_CHARS] + "\n…[truncated in task ledger]"
                snapshot["result_truncated"] = True
            snapshots[task_id] = snapshot
        if not snapshots:
            return
        stored = self.load()
        stored.update(snapshots)
        self.save(stored)

    def remove_many(self, task_ids: Iterable[str]) -> None:
        """Drop records with one read and one atomic replacement."""
        doomed = {str(task_id) for task_id in task_ids}
        stored = self.load()
        if not doomed & stored.keys():
            return
        self.save({task_id: record for task_id, record in stored.items() if task_id not in doomed})

    def restore(self) -> Iterable[dict[str, Any]]:
        return self.load().values()
