"""JSON-backed session persistence for Aria Code."""

from __future__ import annotations

import os
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from aria_code.apps.cli.config_paths import resolve_config_dir


class SessionManager:
    """Manage chat sessions with local file persistence."""

    def __init__(self, sessions_dir: Optional[Path] = None):
        self.root = sessions_dir or (resolve_config_dir() / "sessions")
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def save_session(self, session_id: str, conversation: list, metadata: dict = None):
        meta = dict(metadata or {})
        if not meta.get("created_at"):
            meta["created_at"] = datetime.now().isoformat()
        for msg in conversation:
            if msg.get("role") == "user":
                meta.setdefault("title", str(msg.get("content", ""))[:60])
                break
        data = {
            "id": session_id,
            "messages": conversation,
            "metadata": meta,
            "updated_at": datetime.now().isoformat(),
        }
        path = self._path(session_id)
        # A killed terminal must leave either the prior session or the new
        # one, never a half-written JSON file that cannot be resumed.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root,
                                             prefix=".session-", suffix=".tmp", delete=False) as f:
                temporary = f.name
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def load_session(self, session_id: str) -> Optional[dict]:
        path = self._path(session_id)
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return None

    def list_sessions(self, limit: int = 20) -> list:
        sessions = []
        for path in sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                sessions.append({
                    "id": data.get("id", path.stem),
                    "title": data.get("metadata", {}).get("title", "Untitled"),
                    "messages": len(data.get("messages", [])),
                    "updated": data.get("updated_at", ""),
                })
            except Exception:
                continue
            if len(sessions) >= limit:
                break
        return sessions

    def search_sessions(self, query: str, limit: int = 20) -> list:
        """Full-text search through session message content."""
        q = query.lower()
        results = []
        for path in sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                messages = data.get("messages", [])
                hits = []
                for msg in messages:
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        if q in content.lower():
                            idx = content.lower().index(q)
                            start = max(0, idx - 20)
                            end = min(len(content), idx + len(q) + 80)
                            hits.append(content[start:end])
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                text = block.get("text", "")
                                if text and q in text.lower():
                                    idx = text.lower().index(q)
                                    start = max(0, idx - 20)
                                    end = min(len(text), idx + len(q) + 80)
                                    hits.append(text[start:end])
                if hits:
                    results.append({
                        "id": data.get("id", path.stem),
                        "title": data.get("metadata", {}).get("title", "Untitled"),
                        "updated": data.get("updated_at", ""),
                        "match_count": len(hits),
                        "preview": hits[0],
                    })
            except Exception:
                continue
            if len(results) >= limit:
                break
        return sorted(results, key=lambda r: r["match_count"], reverse=True)

    def delete_session(self, session_id: str) -> bool:
        path = self._path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False
