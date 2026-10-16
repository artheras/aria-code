"""The account-page login: bound to this process by state and a PKCE verifier."""
import base64
import hashlib
import json
import threading
import urllib.parse
import urllib.request

import pytest

from aria_code.apps.cli import web_login


def test_the_pkce_pair_is_a_valid_s256_pair():
    verifier, challenge = web_login.pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected and "=" not in challenge
    assert web_login.pkce_pair()[0] != verifier, "verifiers must not repeat"


def _drive(monkeypatch, callback, reply=None, posts=None):
    """Run run_web_login with a fake browser that performs `callback(url, port, state)`."""
    posts = posts if posts is not None else []

    def post(url, payload, timeout=20):
        posts.append((url, payload))
        return reply if reply is not None else {"success": True, "data": {
            "auth_token": "id-token", "refresh_token": "rt", "expires_in": 3600,
            "user_id": "u1", "email": "a@example.com", "display_name": "A"}}

    monkeypatch.setattr(web_login, "_post_json", post)
    seen = {}

    def browser(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        seen.update(url=url, port=int(q["port"][0]), state=q["state"][0], challenge=q["code_challenge"][0])
        threading.Thread(target=callback, args=(seen,), daemon=True).start()
        return True

    return web_login.run_web_login("https://api.example.com/", open_browser=browser, timeout=5), seen, posts


def _hit(port, **params):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?" + urllib.parse.urlencode(params), timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as err:
        return err.code


def test_a_matching_callback_is_redeemed_with_the_verifier_and_returns_a_session(monkeypatch):
    session, seen, posts = _drive(monkeypatch, lambda s: _hit(s["port"], handoff_code="c" * 40, state=s["state"]))
    assert seen["url"].startswith("https://arthera.finance/cli-login/?")
    url, payload = posts[0]
    assert url == "https://api.example.com/api/v2/auth/cli/exchange"
    assert payload["code"] == "c" * 40 and payload["state"] == seen["state"]
    # What went to the page is the hash of what is sent to the server.
    digest = hashlib.sha256(payload["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == seen["challenge"]
    assert session["auth_token"] == "id-token" and session["refresh_token"] == "rt"
    assert session["expires_in"] == "3600" and session["provider"] == "firebase"


def test_a_callback_with_the_wrong_state_is_ignored_and_the_real_one_still_works(monkeypatch):
    statuses = []

    def callback(s):
        statuses.append(_hit(s["port"], handoff_code="evil" * 10, state="x" * 32))
        statuses.append(_hit(s["port"], handoff_code="c" * 40, state=s["state"]))

    session, _seen, posts = _drive(monkeypatch, callback)
    assert statuses == [404, 200]
    assert posts[0][1]["code"] == "c" * 40, "the forged callback's code must never be redeemed"
    assert session["auth_token"] == "id-token"


def test_a_failed_redemption_is_reported_not_swallowed(monkeypatch):
    def post(url, payload, timeout=20):
        raise OSError("HTTP 400")

    monkeypatch.setattr(web_login, "_post_json", post)

    def browser(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        threading.Thread(target=_hit, args=(int(q["port"][0]),), kwargs={"handoff_code": "c" * 40, "state": q["state"][0]}, daemon=True).start()
        return True

    with pytest.raises(RuntimeError, match="Could not redeem"):
        web_login.run_web_login("https://api.example.com", open_browser=browser, timeout=5)


def test_no_browser_gives_the_url_to_open_by_hand(monkeypatch):
    with pytest.raises(RuntimeError, match="Visit this URL"):
        web_login.run_web_login("https://api.example.com", open_browser=lambda url: False, timeout=1)


def test_the_page_host_can_be_overridden_for_staging(monkeypatch):
    monkeypatch.setenv("ARIA_AUTH_URL", "https://staging-auth.example.com/")
    assert web_login.auth_origin() == "https://staging-auth.example.com"
