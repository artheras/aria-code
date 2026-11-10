"""Google sign-in must reach the Arthera gateway, not a developer backend on localhost."""
import pytest

from aria_code.apps.cli.google_login import login_api_url
from aria_code.providers.llm.arthera_gateway import DEFAULT_API_URL


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.delenv("ARIA_API_URL", raising=False)


@pytest.mark.parametrize("configured", [
    "http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000", "http://localhost", "", None,
])
def test_loopback_or_missing_api_url_means_the_production_gateway(configured):
    assert login_api_url(configured) == DEFAULT_API_URL


def test_a_real_configured_server_is_respected_without_a_trailing_slash():
    assert login_api_url("https://api.example.com/") == "https://api.example.com"


def test_the_environment_override_wins_over_everything(monkeypatch):
    monkeypatch.setenv("ARIA_API_URL", "https://staging.example.com/")
    assert login_api_url("http://localhost:8000") == "https://staging.example.com"
    assert login_api_url("https://api.example.com") == "https://staging.example.com"


def test_the_login_page_can_be_served_from_the_brand_auth_host(monkeypatch):
    from aria_code.apps.cli.google_login import login_page_origin

    monkeypatch.delenv("ARIA_AUTH_URL", raising=False)
    assert login_page_origin("https://api.arthera.xyz/") == "https://api.arthera.xyz"
    monkeypatch.setenv("ARIA_AUTH_URL", "https://auth.arthera.xyz/")
    assert login_page_origin("https://api.arthera.xyz") == "https://auth.arthera.xyz"
