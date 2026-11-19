"""Regression tests for the Google Programmable Search backend in _web_search.

GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_ENGINE_ID were documented in
.env.example but no code path ever read them, so configuring them changed
nothing and search fell through to rate-limited DuckDuckGo.

Hermetic — urlopen is stubbed; no request leaves the process.
"""

import io
import json
import os
import unittest
from contextlib import contextmanager
from unittest import mock

from aria_code.tools.web_search import _web_search

GOOGLE_PAYLOAD = {
    "items": [
        {"title": "First", "link": "https://example.com/1", "snippet": "first snippet"},
        {"title": "Second", "link": "https://example.com/2", "snippet": "second snippet"},
    ]
}

# Keys for every provider that is tried before Google, so the chain reaches it.
_EARLIER_PROVIDER_KEYS = ("BRAVE_SEARCH_API_KEY", "TAVILY_API_KEY")


@contextmanager
def _google_only_env(**overrides):
    """Configure Google credentials and clear the providers tried before it."""
    keys = _EARLIER_PROVIDER_KEYS + ("GOOGLE_SEARCH_API_KEY", "GOOGLE_SEARCH_ENGINE_ID")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for key in _EARLIER_PROVIDER_KEYS:
            os.environ.pop(key, None)
        os.environ["GOOGLE_SEARCH_API_KEY"] = "test-key"
        os.environ["GOOGLE_SEARCH_ENGINE_ID"] = "test-cx"
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _fake_urlopen(recorder):
    @contextmanager
    def _open(url, timeout=None):
        recorder.append(url)
        yield io.BytesIO(json.dumps(GOOGLE_PAYLOAD).encode())

    return _open


class GoogleSearchBackendTests(unittest.TestCase):
    def setUp(self):
        # _resolve_search_key also consults ~/.arthera/providers.json; pin it to
        # the env vars so a developer's real keys cannot influence the result.
        patcher = mock.patch(
            "aria_code.tools.web_search._resolve_search_key",
            side_effect=lambda env_var, provider: os.getenv(env_var, ""),
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_google_results_are_returned(self):
        urls = []
        with _google_only_env(), mock.patch(
            "urllib.request.urlopen", _fake_urlopen(urls)
        ):
            result = _web_search({"query": "nvidia earnings"})

        self.assertTrue(result["success"])
        self.assertEqual(result["provider"], "google")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["url"], "https://example.com/1")
        self.assertEqual(result["results"][0]["snippet"], "first snippet")

    def test_request_targets_the_custom_search_endpoint_with_credentials(self):
        urls = []
        with _google_only_env(), mock.patch(
            "urllib.request.urlopen", _fake_urlopen(urls)
        ):
            _web_search({"query": "nvidia earnings"})

        self.assertEqual(len(urls), 1)
        self.assertIn("customsearch/v1", urls[0])
        self.assertIn("key=test-key", urls[0])
        self.assertIn("cx=test-cx", urls[0])

    def test_chinese_query_requests_chinese_results(self):
        urls = []
        with _google_only_env(), mock.patch(
            "urllib.request.urlopen", _fake_urlopen(urls)
        ):
            _web_search({"query": "英伟达 财报"})

        self.assertIn("lr=lang_zh-CN", urls[0])

    def test_english_query_does_not_pin_a_language(self):
        urls = []
        with _google_only_env(), mock.patch(
            "urllib.request.urlopen", _fake_urlopen(urls)
        ):
            _web_search({"query": "nvidia earnings"})

        self.assertNotIn("lr=", urls[0])

    def test_google_is_skipped_without_an_engine_id(self):
        # A key alone cannot query Programmable Search; the chain must move on
        # rather than raise.
        urls = []
        with _google_only_env(GOOGLE_SEARCH_ENGINE_ID=None), mock.patch(
            "urllib.request.urlopen", _fake_urlopen(urls)
        ):
            result = _web_search({"query": "nvidia earnings"})

        self.assertEqual(urls, [])
        self.assertNotEqual(result.get("provider"), "google")

    def test_num_results_is_forwarded_and_capped(self):
        urls = []
        with _google_only_env(), mock.patch(
            "urllib.request.urlopen", _fake_urlopen(urls)
        ):
            _web_search({"query": "nvidia", "num_results": 50})

        self.assertIn("num=10", urls[0])


if __name__ == "__main__":
    unittest.main()
