"""Firebase account identity and refresh shared by the CLI and memory client.

JWT claims here are a local consistency check, never authorization: the API
still verifies Firebase's signature/project on every request.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from aria_code.packages.aria_core.paths import aria_home
from aria_code.packages.aria_core.secure_file import write_secret_json

PROJECT_ID = "arthera-66e5b"


def read_config() -> dict:
    try:
        value = json.loads((aria_home() / "config.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def claims_for(token: str) -> dict:
    try:
        encoded = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if claims.get("aud") != PROJECT_ID or claims.get("iss") != f"https://securetoken.google.com/{PROJECT_ID}":
            return {}
        return claims if isinstance(claims.get("sub"), str) and claims["sub"] else {}
    except (ValueError, IndexError, TypeError, AttributeError):
        return {}


def account_id(config: dict) -> str | None:
    if not config.get("auth_token"):
        return None
    uid = str(config.get("user_id") or "")
    claims = claims_for(str(config["auth_token"]))
    # Never infer identity from email or reuse a different account's token.
    return uid if uid and claims.get("sub") == uid else None


def account_root(config: dict | None = None) -> Path:
    config = read_config() if config is None else config
    uid = account_id(config)
    bucket = hashlib.sha256(uid.encode()).hexdigest() if uid else "anonymous"
    return aria_home() / "accounts" / bucket


def profile_path(config: dict | None = None) -> Path:
    return account_root(config) / "ARIA.md"


def persist_if_current(config: dict, original: tuple, updates: dict, save: Callable | None = None) -> bool:
    if (config.get("user_id"), config.get("auth_token"), config.get("refresh_token")) != original:
        return False
    config.update(updates)
    if save:
        save(config)
    else:
        disk = read_config()
        if (disk.get("user_id"), disk.get("auth_token"), disk.get("refresh_token")) != original:
            return False
        disk.update(updates)
        write_secret_json(aria_home() / "config.json", disk)
    return True


def get_account_token(config: dict, save: Callable | None = None, *, force=False) -> str | None:
    uid = account_id(config)
    if not uid:
        return None
    token = str(config["auth_token"])
    claims = claims_for(token)
    try:
        expires = float(claims.get("exp", 0))
    except (ValueError, TypeError):
        return None
    if not force and expires > time.time() + 120:
        return token
    refresh = config.get("refresh_token")
    if not refresh:
        return token if expires > time.time() else None
    original = (uid, token, refresh)
    from aria_code.apps.cli.google_login import FIREBASE_WEB_API_KEY

    request = urllib.request.Request(
        f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_WEB_API_KEY}",
        data=urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            result = json.loads(response.read())
        fresh = result.get("id_token", "")
        fresh_claims = claims_for(fresh)
        if (
            result.get("user_id") != uid
            or fresh_claims.get("sub") != uid
            or float(fresh_claims.get("exp", 0)) <= time.time()
        ):
            return None
        updates = {"auth_token": fresh, "refresh_token": result.get("refresh_token") or refresh}
        if isinstance(fresh_claims.get("email"), str):
            updates["email"] = fresh_claims["email"].strip().lower()
        from datetime import datetime, timezone

        updates["token_expires_at"] = datetime.fromtimestamp(float(fresh_claims["exp"]), timezone.utc).isoformat()
        if not persist_if_current(config, original, updates, save):
            return None
        return fresh
    except Exception:
        # Do not expose tokens, refresh tokens, HTTP bodies or URLs in errors.
        return token if not force and expires > time.time() else None


def password_session(email: str, password: str) -> dict:
    """Browserless fallback, using the exact same Firebase account provider."""
    from aria_code.apps.cli.google_login import FIREBASE_WEB_API_KEY

    request = urllib.request.Request(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_WEB_API_KEY}",
        data=json.dumps({"email": email, "password": password, "returnSecureToken": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read())
    token, uid = result.get("idToken"), result.get("localId")
    if not token or claims_for(token).get("sub") != uid:
        raise ValueError("Missing Firebase identity")
    return {
        "auth_token": token,
        "user_id": uid,
        "refresh_token": result.get("refreshToken"),
        "email": result.get("email"),
        "display_name": result.get("displayName"),
        "provider": "password",
    }


async def prepare_account(config: dict, save=None) -> str | None:
    """Refresh credentials, and give memory two seconds before using its cache.

    The fenced sync may finish in the background; a slow memory service must
    not hold up sending a chat turn. Its status/outbox remains inspectable.
    """
    import asyncio
    from aria_code.account_memory import AccountMemory

    token = await asyncio.to_thread(get_account_token, config, save)
    try:
        await asyncio.wait_for(asyncio.shield(asyncio.to_thread(AccountMemory(config, save=save).sync)), timeout=2)
    except asyncio.TimeoutError:
        pass
    return token
