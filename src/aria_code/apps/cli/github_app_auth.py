"""GitHub App authentication — generates installation tokens for Aria Code[bot].

Usage:
    from apps.cli.github_app_auth import get_installation_token, get_aria_git_url

    token = get_installation_token()          # raises if not configured
    url   = get_aria_git_url("artheras/aria-code", token)
    # git push with url as remote
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────
ARIA_APP_ID    = int(os.getenv("ARIA_GITHUB_APP_ID", "4114189"))
ARIA_APP_SLUG  = os.getenv("ARIA_GITHUB_APP_SLUG", "aria-code")

# Search order for private key
_PEM_SEARCH = [
    os.getenv("ARIA_GITHUB_APP_PEM", ""),
    os.path.expanduser("~/.config/aria-code/github-app.pem"),
    os.path.expanduser("~/.aria-code/github-app.pem"),
    "github-app.pem",
]

# noreply email GitHub uses for bot commits
# Format: {app_id}+{app_slug}[bot]@users.noreply.github.com
ARIA_BOT_EMAIL = f"{ARIA_APP_ID}+{ARIA_APP_SLUG}[bot]@users.noreply.github.com"
ARIA_BOT_NAME  = "Aria Code"

# GitHub user account for Aria — shows up in Contributors
ARIA_GITHUB_LOGIN = os.getenv("ARIA_GITHUB_LOGIN", "ariaaii")
ARIA_GITHUB_EMAIL = os.getenv("ARIA_GITHUB_EMAIL", "support@arthera.xyz")


def _find_pem() -> str:
    for path in _PEM_SEARCH:
        if path and os.path.isfile(path):
            return path
    raise FileNotFoundError(
        "GitHub App private key not found. "
        "Set ARIA_GITHUB_APP_PEM=/path/to/key.pem or place it at "
        "~/.config/aria-code/github-app.pem"
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _make_jwt(pem_path: str, app_id: int) -> str:
    """Create a signed JWT using RSA-SHA256 (pure stdlib + cryptography)."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend
    except ImportError:
        raise ImportError(
            "Install cryptography: pip install cryptography"
        )

    with open(pem_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

    now = int(time.time())
    header  = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = _b64url(private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))
    return f"{header}.{payload}.{signature}"


def _gh_api(path: str, token: str, *, method: str = "GET") -> dict:
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def get_installation_token(owner: str = "artheras") -> str:
    """Return a short-lived installation token (valid ~1 hour)."""
    pem   = _find_pem()
    jwt   = _make_jwt(pem, ARIA_APP_ID)

    # Find installation for owner
    installations = _gh_api("/app/installations", jwt)
    installation_id = None
    for inst in installations:
        if inst.get("account", {}).get("login", "").lower() == owner.lower():
            installation_id = inst["id"]
            break
    if installation_id is None:
        accts = [i.get("account", {}).get("login") for i in installations]
        raise RuntimeError(
            f"Aria Code GitHub App not installed for '{owner}'. "
            f"Installed accounts: {accts}. "
            f"Go to https://github.com/apps/aria-code and install it."
        )

    # Exchange for installation token
    result = _gh_api(f"/app/installations/{installation_id}/access_tokens", jwt, method="POST")
    token = result.get("token")
    if not token:
        raise RuntimeError(f"Failed to get installation token: {result}")
    return token


def get_aria_git_url(repo: str, token: str) -> str:
    """Return an authenticated HTTPS remote URL using the installation token."""
    return f"https://x-access-token:{token}@github.com/{repo}.git"


def aria_bot_env() -> dict[str, str]:
    """Env vars that make git commit as Aria Code[bot]."""
    return {
        "GIT_AUTHOR_NAME":     ARIA_BOT_NAME,
        "GIT_AUTHOR_EMAIL":    ARIA_BOT_EMAIL,
        "GIT_COMMITTER_NAME":  ARIA_BOT_NAME,
        "GIT_COMMITTER_EMAIL": ARIA_BOT_EMAIL,
    }
