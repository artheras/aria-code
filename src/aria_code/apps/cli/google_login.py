"""Google sign-in for the CLI, following RFC 8252's native-app flow.

The desktop app receives its OAuth callback through the arthera:// URL scheme it
registers with the OS. A CLI cannot own a scheme, so it does what RFC 8252
prescribes instead: bind an ephemeral port on loopback, hand that port to the
gateway, and let the browser deliver the callback there.

Three properties are worth keeping in mind when editing this:

* Only a port number is sent to the gateway, never a redirect URL. The callback
  builds the loopback address itself, so a caller cannot point the credential
  delivery anywhere else.
* What arrives on loopback is a single-use handoff code, not a token. The code is
  redeemed over POST, so the browser history never holds a credential.
* The gateway returns a *Google* ID token. The Arthera API authenticates with
  *Firebase* ID tokens, so the credential is exchanged with Identity Toolkit
  before it is stored — the same exchange the desktop app performs through the
  Firebase SDK.
"""

from __future__ import annotations

import http.server
import json
import secrets
import socket
import threading
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Dict, Optional

# The Firebase Web API key is public by design — it identifies the project and
# authorises nothing on its own. It is the same value the web and desktop
# clients ship in their bundles.
FIREBASE_WEB_API_KEY = "AIzaSyC9iYiroXgxTu2ANUPIjeGbITVzWEUjXAI"
IDENTITY_TOOLKIT_SIGN_IN_WITH_IDP = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp"
)

_LOGIN_TIMEOUT_SECONDS = 300


class _CallbackResult:
    def __init__(self) -> None:
        self.params: Dict[str, str] = {}
        self.received = threading.Event()


def _make_handler(result: _CallbackResult, expected_path: str):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != expected_path:
                self.send_response(404)
                self.end_headers()
                return

            result.params = {
                k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()
            }
            ok = "handoff_code" in result.params
            body = (
                "<html><body style='font-family:system-ui;padding:3rem'>"
                f"<h2>{'Signed in.' if ok else 'Sign-in failed.'}</h2>"
                "<p>You can close this tab and return to the terminal.</p>"
                "</body></html>"
            ).encode("utf-8")

            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            result.received.set()

        def log_message(self, *args):  # keep the terminal clean
            return

    return Handler


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_json(url: str, payload: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def exchange_google_token_for_firebase(google_id_token: str) -> Dict[str, Any]:
    """Trade a verified Google ID token for a Firebase session.

    requestUri is required by the endpoint but only has to be a well-formed URL
    the project would accept; nothing is fetched from it.
    """
    body = {
        "postBody": urllib.parse.urlencode(
            {"id_token": google_id_token, "providerId": "google.com"}
        ),
        "requestUri": "http://127.0.0.1",
        "returnIdpCredential": True,
        "returnSecureToken": True,
    }
    return _post_json(
        f"{IDENTITY_TOOLKIT_SIGN_IN_WITH_IDP}?key={FIREBASE_WEB_API_KEY}", body
    )


def login_api_url(configured: str) -> str:
    """Which server serves Google sign-in for this install.

    Sign-in is a route of the Arthera gateway, and the credential it returns is
    used against that gateway. The saved ``api_url`` is something else: a
    fresh install writes ``http://localhost:8000`` there, a developer backend
    that has no such route, so /login opened a browser on a page reading
    ``{"detail":"Not Found"}`` (or on a refused connection when nothing listens).

    ``ARIA_API_URL`` wins, as it does for the gateway provider. Otherwise a
    loopback ``api_url`` means "no real server was ever configured" and the
    production gateway is used. Any other configured server is respected.
    """
    import os

    from ...providers.llm.arthera_gateway import DEFAULT_API_URL

    override = os.getenv("ARIA_API_URL", "").strip()
    if override:
        return override.rstrip("/")
    host = (urllib.parse.urlparse(configured or "").hostname or "").lower()
    if not configured or host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return DEFAULT_API_URL
    return configured.rstrip("/")


def run_google_login(api_url: str, region: str = "GLOBAL") -> Dict[str, Any]:
    """Drive the whole flow and return the stored-credential fields.

    Raises RuntimeError with a message suitable for display on any failure.
    """
    api_url = api_url.rstrip("/")
    port = _free_port()
    callback_path = "/callback"
    result = _CallbackResult()

    server = http.server.HTTPServer(
        ("127.0.0.1", port), _make_handler(result, callback_path)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        login_url = (
            f"{api_url}/api/v2/auth/google/login?"
            + urllib.parse.urlencode(
                {"platform": "cli", "region": region, "port": port}
            )
        )
        opened = webbrowser.open(login_url)
        if not opened:
            raise RuntimeError(
                "Could not open a browser. Visit this URL manually:\n  " + login_url
            )

        if not result.received.wait(timeout=_LOGIN_TIMEOUT_SECONDS):
            raise RuntimeError(
                f"Timed out after {_LOGIN_TIMEOUT_SECONDS}s waiting for the browser."
            )
    finally:
        server.shutdown()
        server.server_close()

    if "message" in result.params and "handoff_code" not in result.params:
        raise RuntimeError(result.params["message"])

    handoff_code = result.params.get("handoff_code")
    if not handoff_code:
        raise RuntimeError("The sign-in callback did not carry a handoff code.")

    try:
        exchanged = _post_json(
            f"{api_url}/api/v2/auth/handoff/exchange", {"code": handoff_code}
        )
    except Exception as exc:
        raise RuntimeError(f"Could not redeem the sign-in handoff: {exc}") from exc

    payload = exchanged.get("data") or {}
    google_id_token = payload.get("token")
    if not google_id_token:
        raise RuntimeError("The sign-in handoff did not contain a credential.")

    try:
        session = exchange_google_token_for_firebase(google_id_token)
    except Exception as exc:
        raise RuntimeError(f"Firebase rejected the Google credential: {exc}") from exc

    id_token = session.get("idToken")
    if not id_token:
        raise RuntimeError("Firebase did not return a session token.")

    return {
        "auth_token": id_token,
        "refresh_token": session.get("refreshToken"),
        # expiresIn is seconds-as-string; the caller decides how to persist it.
        "expires_in": session.get("expiresIn"),
        "user_id": session.get("localId") or payload.get("uid"),
        "email": session.get("email") or payload.get("email"),
        "display_name": session.get("displayName") or payload.get("display_name"),
        "provider": "google.com",
    }


__all__ = ["run_google_login", "exchange_google_token_for_firebase", "login_api_url"]
