"""Cross-platform memory sync for the CLI.

MemoryManager keeps facts in ~/.arthera/memory/ so the CLI works offline and
without an account. This module carries the durable ones to the same store the
web, desktop and iOS clients read (`/api/v2/memory/items`), so a preference
stated in the terminal is not invisible everywhere else.

Local stays the working copy on purpose. Sync is best-effort: an expired token,
an offline laptop or a backend outage must never cost the user a memory or block
the turn they are in the middle of. Every call here returns a value instead of
raising, and the caller treats failure as "not synced yet".

Identity comes from `aria login` (see apps/cli/google_login.py). Without a token
this module does nothing at all — memory is per-user on the server, and there is
no sensible anonymous bucket to write into.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 8
_MAX_CONTENT_CHARS = 4_000  # matches MemoryItemCreate.content on the server


class CloudMemoryClient:
    """Thin client over the cross-platform memory API.

    Instantiate with the API base URL and the Firebase ID token stored by
    `/login`. `available` is False when there is no token, which callers should
    check rather than relying on every method quietly no-opping.
    """

    def __init__(self, api_url: str, token: Optional[str]) -> None:
        from aria_code.service_endpoints import current_url
        self.api_url = current_url(api_url)
        self.token = (token or "").strip()
        self.last_status = None

    @property
    def available(self) -> bool:
        return bool(self.api_url and self.token)

    # ── transport ────────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.available:
            return None
        self.last_status = None

        url = f"{self.api_url}{path}"
        if query:
            pairs = {k: v for k, v in query.items() if v is not None}
            if pairs:
                url = f"{url}?{urllib.parse.urlencode(pairs)}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
            self.last_status = 200
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            self.last_status = exc.code
            # 401 is the ordinary case of an ID token older than an hour, not an
            # error worth interrupting the user over. Say so at debug level and
            # let the next /login refresh it.
            level = logging.DEBUG if exc.code == 401 else logging.WARNING
            logger.log(level, "Cloud memory %s %s failed: HTTP %s", method, path, exc.code)
            return None
        except Exception as exc:
            logger.debug("Cloud memory %s %s unreachable: %s", method, path, exc)
            return None

    # ── items ────────────────────────────────────────────────────────────────

    def push_item(
        self,
        content: str,
        *,
        kind: str = "durable",
        scope: str = "user",
        project_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Store one memory server-side. Returns its id, or None if not synced.

        `classification` is left at the server default. Anything the server
        considers sensitive, financial or connected requires explicit consent
        there, and a CLI turn is not a place to imply that consent was given.
        """
        text = (content or "").strip()
        if not text:
            return None
        if scope == "project" and not project_id:
            # The server rejects this combination; catching it here keeps a
            # pointless round trip and a confusing 422 out of the logs.
            logger.debug("Cloud memory: project scope needs a project_id")
            return None

        payload: Dict[str, Any] = {
            "content": text[:_MAX_CONTENT_CHARS],
            "kind": kind,
            "scope": scope,
            "metadata": {"source": "aria-code", **(metadata or {})},
        }
        if project_id:
            payload["project_id"] = project_id

        result = self._request("POST", "/api/v2/memory/items", body=payload)
        if not isinstance(result, dict):
            return None
        item_id = result.get("id") or (result.get("item") or {}).get("id")
        return str(item_id) if item_id else None

    def list_items(
        self,
        *,
        scope: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return stored memories, newest first. Empty list when unavailable."""
        result = self._request(
            "GET",
            "/api/v2/memory/items",
            query={
                "scope": scope,
                "project_id": project_id,
                "limit": max(1, min(int(limit), 100)),
            },
        )
        if not isinstance(result, dict):
            return []
        items = result.get("items")
        return items if isinstance(items, list) else []

    # ── preferences ──────────────────────────────────────────────────────────

    def delete_item(self, item_id: str) -> bool:
        result = self._request("DELETE", f"/api/v2/memory/items/{urllib.parse.quote(item_id, safe='')}")
        return result is not None or self.last_status == 404

    def get_preferences(self) -> Dict[str, Any]:
        result = self._request("GET", "/api/v2/memory/preferences")
        return result if isinstance(result, dict) else {}

    def put_preferences(self, preferences: Dict[str, str]) -> bool:
        result = self._request(
            "PUT", "/api/v2/memory/preferences", body=dict(preferences)
        )
        return result is not None


def client_from_config(config: Dict[str, Any]) -> CloudMemoryClient:
    """Build a client from the CLI config written by /login.

    Kept separate from CloudMemoryClient so the class stays testable without a
    config dict, and so the key names live in one place if they change.
    """
    return CloudMemoryClient(
        api_url=str(config.get("api_url") or ""),
        token=config.get("auth_token"),
    )


__all__ = ["CloudMemoryClient", "client_from_config"]
