"""Sign in through the account page, the way Claude Code and Codex do.

``/login`` opens a page on Arthera's own auth host. The user chooses how to sign
in there (Google, Apple or email, and email can be a new account) and the page
hands the session back to this process on a loopback port. Nothing is typed in
the terminal and no provider is chosen for the user.

The hand-back is bound to this process, not merely to "whoever is listening":

* ``state`` is random and checked on the way back, so a callback this run did
  not start is ignored;
* a PKCE ``code_verifier`` stays here; only its SHA-256 goes into the page. The
  one-time code in the redirect is useless without it, so a program that sees
  the redirect cannot redeem it;
* the credential never appears in a URL. The redirect carries a short code and
  the session is fetched over POST.

Page: the /cli-login route of apps/web. Server side: apps/api/src/routes/auth_v2_routes.py
(/cli/handoff, /cli/exchange).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import os
import secrets
import threading
import urllib.parse
import webbrowser
from typing import Any, Dict

from .google_login import _CallbackResult, _free_port, _post_json

# The apex is Firebase Hosting and the Firebase auth domain, so the page and the
# sign-in popup are same-origin there.
AUTH_ORIGIN = "https://arthera.xyz"
LOGIN_PATH = "/cli-login"
_TIMEOUT_SECONDS = 300


def auth_origin() -> str:
    """The host that serves the sign-in page. ARIA_AUTH_URL overrides it for staging."""
    return (os.getenv("ARIA_AUTH_URL", "").strip() or AUTH_ORIGIN).rstrip("/")


def pkce_pair() -> tuple[str, str]:
    """A code verifier and its S256 challenge (RFC 7636)."""
    verifier = secrets.token_urlsafe(48)  # 64 characters, inside the 43-128 the spec allows
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _handler(result: _CallbackResult, state: str):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            parsed = urllib.parse.urlparse(self.path)
            params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            if parsed.path != "/callback" or not secrets.compare_digest(params.get("state", ""), state):
                # Not this run's callback: ignore it and keep waiting for ours.
                self.send_response(404)
                self.end_headers()
                return
            ok = "handoff_code" in params
            body = (
                "<html><body style='font-family:system-ui;padding:3rem'>"
                f"<h2>{'Signed in.' if ok else 'Sign-in failed.'}</h2>"
                "<p>You can close this tab and return to the terminal.</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            result.params = params
            result.received.set()

        def log_message(self, *args):  # keep the terminal clean
            return

    return Handler


def run_web_login(api_url: str, *, open_browser=webbrowser.open, timeout: int = _TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Drive the whole flow; return the stored-credential fields.

    Raises RuntimeError with a message suitable for display on any failure.
    """
    api_url = api_url.rstrip("/")
    port = _free_port()
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    result = _CallbackResult()
    server = http.server.HTTPServer(("127.0.0.1", port), _handler(result, state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"{auth_origin()}{LOGIN_PATH}?" + urllib.parse.urlencode(
            {"port": port, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
        )
        if not open_browser(url):
            raise RuntimeError("Could not open a browser. Visit this URL manually:\n  " + url)
        if not result.received.wait(timeout=timeout):
            raise RuntimeError(f"Timed out after {timeout}s waiting for the browser.")
    finally:
        server.shutdown()
        server.server_close()

    code = result.params.get("handoff_code")
    if not code:
        raise RuntimeError(result.params.get("message") or "The sign-in did not carry a handoff code.")
    try:
        reply = _post_json(f"{api_url}/api/v2/auth/cli/exchange",
                           {"code": code, "code_verifier": verifier, "state": state})
    except Exception as exc:
        raise RuntimeError(f"Could not redeem the sign-in: {exc}") from exc
    data = reply.get("data") or {}
    if not data.get("auth_token"):
        raise RuntimeError("The sign-in did not return a session.")
    return {
        "auth_token": data["auth_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_in": str(data["expires_in"]) if data.get("expires_in") is not None else None,
        "user_id": data.get("user_id"),
        "email": data.get("email"),
        "display_name": data.get("display_name"),
        "provider": "firebase",
    }


__all__ = ["run_web_login", "pkce_pair", "auth_origin", "AUTH_ORIGIN"]
