"""Keep a Firebase sign-in alive the way Claude Code and Codex keep theirs.

A Firebase ID token lasts an hour. /login stores it with a refresh token, but
nothing used the refresh token, so an hour after signing in every gateway call
failed and the user was told to /login again. Claude Code and Codex do not ask:
they exchange the refresh token for a new access token shortly before the old
one expires and carry on.

This does the same against Firebase's Secure Token API, which accepts the
project's public web API key (the one the web and desktop clients ship; see
google_login.FIREBASE_WEB_API_KEY) and the user's refresh token. The result is
written back to config.json with owner-only permissions, so every process that
reads the session, the gateway provider and the Commerce plugin among them,
sees the fresh token.

Failure is deliberately quiet: if the refresh cannot happen (offline, token
revoked) the stored token is returned unchanged, the request fails with the
gateway's own 401, and /login remains the way back in.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

SECURE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"

#: Refresh when the token has this long or less left, so a request that starts
#: just before expiry is not sent with a token that dies in flight.
REFRESH_WITHIN_SECONDS = 300


def _parse_expiry(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def needs_refresh(config: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """True when the stored token is expired or about to be, and can be renewed."""
    if not config.get("auth_token") or not config.get("refresh_token"):
        return False
    expires = _parse_expiry(config.get("token_expires_at"))
    if expires is None:
        # A login from before expiry was recorded: nothing to compare against,
        # so leave it alone rather than refresh on every call.
        return False
    now = now or datetime.now(timezone.utc)
    return expires - now <= timedelta(seconds=REFRESH_WITHIN_SECONDS)


def exchange_refresh_token(refresh_token: str, api_key: str, *, timeout: int = 15,
                           opener: Callable = urllib.request.urlopen) -> Dict[str, Any]:
    """Trade a refresh token for a new ID token. Raises on any failure."""
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token}).encode()
    request = urllib.request.Request(
        f"{SECURE_TOKEN_URL}?key={urllib.parse.quote(api_key)}", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
    )
    with opener(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def refresh_config_file(path: Path, *, now: Optional[datetime] = None,
                        exchange: Optional[Callable[[str, str], Dict[str, Any]]] = None) -> Optional[str]:
    """Renew the session stored in `path` if it is due; return the token to use.

    Returns the current (possibly just refreshed) ID token, or None when there
    is no session. Never raises: a failed refresh returns the stored token.
    """
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = config.get("auth_token")
    token = str(token) if token else None
    if not token or not needs_refresh(config, now):
        return token

    from .google_login import FIREBASE_WEB_API_KEY

    try:
        result = (exchange or exchange_refresh_token)(str(config["refresh_token"]), FIREBASE_WEB_API_KEY)
        new_token = result.get("id_token") or result.get("idToken")
        if not new_token:
            return token
        config["auth_token"] = new_token
        if result.get("refresh_token") or result.get("refreshToken"):
            config["refresh_token"] = result.get("refresh_token") or result.get("refreshToken")
        seconds = int(result.get("expires_in") or result.get("expiresIn") or 3600)
        stamp = (now or datetime.now(timezone.utc)) + timedelta(seconds=seconds)
        config["token_expires_at"] = stamp.isoformat().replace("+00:00", "Z")
        from aria_code.packages.aria_core.secure_file import write_secret_json

        # Replace the file in one step: this is the whole config, and a crash
        # halfway through an in-place write would lose every setting in it.
        target = Path(path)
        scratch = target.with_name(target.name + ".refresh")
        write_secret_json(scratch, config)
        import os

        os.replace(scratch, target)
        return str(new_token)
    except Exception:
        return token


__all__ = ["needs_refresh", "refresh_config_file", "exchange_refresh_token", "REFRESH_WITHIN_SECONDS"]
