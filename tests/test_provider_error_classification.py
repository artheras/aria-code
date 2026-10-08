from types import SimpleNamespace

import pytest

from aria_code.packages.aria_services.provider_health import classify_provider_error


@pytest.mark.parametrize("message,category,retryable", [
    ("403 API key invalid for generateContent", "auth", False),
    ("404 model not found for generateContent", "model_unavailable", False),
    ("429 too many requests", "rate_limited", True),
    ("HTTP 401: request not found", "auth", False),
    ("generateContent failed", "error", True),
    ("temperature parameter invalid", "error", True),
    ("output limit reached", "error", True),
    ("request rate-limited", "rate_limited", True),
    ("Connection timed out", "timeout", True),
    ("404 no market data found", "no_data", True),
])
def test_actionable_error_categories(message, category, retryable):
    issue = classify_provider_error("google", message)
    assert (issue.category, issue.retryable) == (category, retryable)
    if not retryable:
        assert issue.cooldown_seconds == 0


def test_structured_http_status_wins_over_message():
    issue = classify_provider_error("google", SimpleNamespace(status_code=403, message="rate limit"))
    assert issue.category == "auth"
    assert not issue.retryable


def test_sdk_grpc_status_is_recognized():
    error = SimpleNamespace(code=lambda: SimpleNamespace(name="PERMISSION_DENIED"))
    assert classify_provider_error("google", error).category == "auth"


def test_structured_error_still_hides_sensitive_details():
    error = SimpleNamespace(status_code=429, message="https://example.test/?token=secret-value")
    issue = classify_provider_error("google", error)
    assert issue.category == "rate_limited"
    assert "secret-value" not in issue.message
