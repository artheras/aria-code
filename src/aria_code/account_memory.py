"""Account-scoped durable memory: a cloud snapshot plus a retryable outbox.

Only newly captured account facts are queued. Legacy files and repository
instructions are never uploaded. A failed request never becomes a success.
"""

from __future__ import annotations

import uuid
import os
from contextlib import contextmanager
import json
import threading
import time
from typing import Callable

from aria_code.account_session import account_id, account_root, get_account_token, read_config
from aria_code.cloud_memory import CloudMemoryClient
from aria_code.packages.aria_core.secure_file import write_secret_json

_LOCKS: dict[str, threading.RLock] = {}
_SYNC_LOCKS: dict[str, threading.Lock] = {}
_LOCK = threading.Lock()


def _lock_for(table, key, factory):
    with _LOCK:
        return table.setdefault(str(key), factory())


@contextmanager
def _state_lock(path):
    """Protect read-modify-write across CLI windows as well as threads."""
    with _lock_for(_LOCKS, path, threading.RLock):
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "r+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def _write_state(path, data):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        write_secret_json(temporary, data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AccountMemory:
    def __init__(self, config: dict | Callable[[], dict] | None = None, save=None):
        self._config = config if config is not None else read_config
        self.save = save

    def config(self):
        return self._config() if callable(self._config) else self._config

    def _owner(self):
        config = self.config()
        uid = account_id(config)
        return config, uid, account_root(config) / "cloud-memory.json"

    @staticmethod
    def _read(path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def status(self):
        config, uid, path = self._owner()
        with _state_lock(path):
            data = self._read(path)
        return {
            "account_id": uid,
            "email": config.get("email") or "",
            "enabled": data.get("preferences", {}).get("enabled", True),
            "pending": len(data.get("outbox", [])),
            "last_synced": data.get("last_synced"),
            "error": data.get("error"),
        }

    def facts(self, *, include_disabled=False):
        _, uid, path = self._owner()
        if not uid:
            return []
        with _state_lock(path):
            data = self._read(path)
        if not include_disabled and data.get("preferences", {}).get("enabled") is False:
            return []
        outbox = data.get("outbox", [])
        hidden = {op["id"] for op in outbox if op.get("op") == "delete"}
        cancelled = set(data.get("cancelled_adds", []))
        facts = [
            f
            for f in data.get("facts", [])
            if f.get("id") not in hidden and (f.get("metadata") or {}).get("client_id") not in cancelled
        ]
        seen = {f.get("content") for f in facts}
        for op in outbox:
            if op.get("op") == "add" and op.get("content") not in seen:
                facts.append({"id": op["id"], "content": op["content"], "pending": True})
                seen.add(op["content"])
        return facts

    def context(self, max_chars=800):
        _, uid, path = self._owner()
        if not uid:
            return ""
        with _state_lock(path):
            preferences = self._read(path).get("preferences", {})
        if preferences.get("enabled") is False:
            return ""
        lines = [str(preferences[key]).strip() for key in ("nickname", "occupation", "about") if preferences.get(key)]
        lines.extend(str(f.get("content", "")) for f in self.facts())
        text = "\n".join(dict.fromkeys(lines))[:max_chars]
        return f"## Account Memory\n{text}\n" if text else ""

    def add(self, content: str):
        config, uid, path = self._owner()
        content = content.strip()[:4000]
        if not uid or not content:
            return False
        # Stable per queued operation, so a retry is idempotent but an explicit
        # re-add after deletion can create a new fact.
        item_id = uuid.uuid4().hex
        with _state_lock(path):
            data = self._read(path)
            if data.get("preferences", {}).get("enabled") is False or account_id(self.config()) != uid:
                return False
            if any(f.get("content") == content for f in data.get("facts", [])):
                return True
            outbox = data.setdefault("outbox", [])
            if not any(op.get("op") == "add" and op.get("content") == content for op in outbox):
                outbox.append({"op": "add", "id": item_id, "content": content})
            _write_state(path, data)
        return True

    def forget(self, item_id):
        _, uid, path = self._owner()
        if not uid:
            return False
        with _state_lock(path):
            data = self._read(path)
            pending = data.setdefault("outbox", [])
            local = any(op.get("id") == item_id and op.get("op") == "add" for op in pending)
            if local:
                data["cancelled_adds"] = list(set(data.get("cancelled_adds", [])) | {item_id})
            data["outbox"] = [op for op in pending if op.get("id") != item_id]
            if not local and any(f.get("id") == item_id for f in data.get("facts", [])):
                data["outbox"].append({"op": "delete", "id": item_id})
            _write_state(path, data)
        return True

    def sync(self, *, force=False):
        config, uid, path = self._owner()
        if not uid:
            return {"ok": False, "reason": "sign-in-required"}
        lock = _lock_for(_SYNC_LOCKS, path, threading.Lock)
        if not lock.acquire(blocking=False):
            return {"ok": False, "reason": "in-progress"}
        state_lock = lambda: _state_lock(path)
        original = config.get("auth_token")

        def current():
            now = self.config()
            return account_id(now) == uid and now.get("auth_token") == original

        def update(**fields):
            if not current():
                return
            with state_lock():
                data = self._read(path)
                data.update(fields)
                _write_state(path, data)

        try:
            with state_lock():
                data = self._read(path)
            if not force and time.time() - data.get("last_attempt", 0) < 60:
                return {"ok": not data.get("error"), "cached": True}
            update(last_attempt=time.time())
            token = get_account_token(config, self.save)
            if token:
                original = token
            if not token or not current():
                update(error="sign-in-required")
                return {"ok": False, "reason": "sign-in-required"}
            from aria_code.apps.cli.endpoint_migration import PRODUCTION_API_URL

            client = CloudMemoryClient(str(config.get("api_url") or PRODUCTION_API_URL), token)
            preferences = client.get_preferences()
            if not preferences or not current():
                reason = "sign-in-required" if client.last_status == 401 else "service-unavailable"
                update(error=reason)
                return {"ok": False, "reason": reason}
            # A newly disabled switch takes effect even if the next read fails.
            update(preferences=preferences)
            summary = client._request("GET", "/api/v2/memory/summary")
            if summary is None:
                update(error="service-unavailable")
                return {"ok": False, "reason": "service-unavailable"}
            if not current():
                return {"ok": False, "reason": "account-changed"}
            facts = summary.get("facts", [])
            facts = [f for f in facts if isinstance(f, dict) and f.get("id") and isinstance(f.get("content"), str)]
            update(preferences=preferences, facts=facts)
            enabled = preferences.get("enabled") is not False
            with state_lock():
                latest = self._read(path)
                cancelled = set(latest.get("cancelled_adds", []))
                outbox = latest.setdefault("outbox", [])
                for fact in facts:
                    if (fact.get("metadata") or {}).get("client_id") in cancelled:
                        deletion = {"op": "delete", "id": fact["id"]}
                        if deletion not in outbox:
                            outbox.append(deletion)
                _write_state(path, latest)
                pending = list(outbox)
            failed = False
            for op in pending[:10]:
                if not enabled and op["op"] == "add":
                    continue
                if not current():
                    return {"ok": False, "reason": "account-changed"}
                if op["op"] == "add":
                    existing = next(
                        (
                            f
                            for f in facts
                            if (f.get("metadata") or {}).get("client_id") == op["id"]
                            or f.get("content") == op["content"]
                        ),
                        None,
                    )
                    item = existing or client._request(
                        "POST",
                        "/api/v2/memory/items",
                        body={
                            "content": op["content"],
                            "client_id": op["id"],
                            "scope": "user",
                            "kind": "durable",
                            "metadata": {"source": "aria-code", "client_id": op["id"]},
                        },
                    )
                    success = isinstance(item, dict) and bool(item.get("id"))
                    if success and not existing and not item.get("deleted_at"):
                        facts.append(item)
                else:
                    success = client.delete_item(op["id"])
                    if success:
                        facts = [f for f in facts if f.get("id") != op["id"]]
                if not current():
                    return {"ok": False, "reason": "account-changed"}
                if not success:
                    failed = True
                    break
                with state_lock():
                    latest = self._read(path)
                    latest["outbox"] = [item for item in latest.get("outbox", []) if item != op]
                    if op["op"] == "add" and op["id"] in latest.get("cancelled_adds", []) and item.get("id"):
                        deletion = {"op": "delete", "id": item["id"]}
                        if deletion not in latest["outbox"]:
                            latest["outbox"].append(deletion)
                    latest["facts"] = facts
                    _write_state(path, latest)
            update(last_synced=time.time(), error="pending-retry" if failed else None)
            return {"ok": not failed, "pending": self.status()["pending"], "enabled": enabled}
        except Exception:
            update(error="service-unavailable")
            return {"ok": False, "reason": "service-unavailable"}
        finally:
            lock.release()
