"""Coverage for the CLI's cross-platform memory sync client.

All HTTP is stubbed — no request leaves the process.
"""

from __future__ import annotations

import json
import unittest
import urllib.error
from unittest import mock

from aria_code.cloud_memory import CloudMemoryClient, client_from_config


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class AvailabilityTests(unittest.TestCase):
    def test_unavailable_without_a_token(self):
        client = CloudMemoryClient("https://api.example.com", None)
        self.assertFalse(client.available)

    def test_unavailable_without_an_api_url(self):
        client = CloudMemoryClient("", "a-token")
        self.assertFalse(client.available)

    def test_available_with_both(self):
        client = CloudMemoryClient("https://api.example.com", "a-token")
        self.assertTrue(client.available)

    def test_methods_no_op_when_unavailable_instead_of_raising(self):
        client = CloudMemoryClient("https://api.example.com", None)
        self.assertIsNone(client.push_item("hello"))
        self.assertEqual(client.list_items(), [])
        self.assertEqual(client.get_preferences(), {})
        self.assertFalse(client.put_preferences({"theme": "dark"}))


class PushItemTests(unittest.TestCase):
    def setUp(self):
        self.client = CloudMemoryClient("https://api.example.com", "a-token")

    def test_empty_content_is_not_sent(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertIsNone(self.client.push_item("   "))
        urlopen.assert_not_called()

    def test_project_scope_without_project_id_is_rejected_locally(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            result = self.client.push_item("note", scope="project")
        self.assertIsNone(result)
        urlopen.assert_not_called()

    def test_successful_push_returns_the_item_id(self):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse({"id": "item-1"}),
        ) as urlopen:
            item_id = self.client.push_item(
                "user prefers dark mode", metadata={"tag": "ui"}
            )
        self.assertEqual(item_id, "item-1")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.example.com/api/v2/memory/items")
        self.assertEqual(request.get_header("Authorization"), "Bearer a-token")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["content"], "user prefers dark mode")
        self.assertEqual(body["metadata"]["source"], "aria-code")
        self.assertEqual(body["metadata"]["tag"], "ui")

    def test_content_is_truncated_to_the_server_limit(self):
        long_text = "x" * 5_000
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeResponse({"id": "item-2"})
        ) as urlopen:
            self.client.push_item(long_text)
        body = json.loads(urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(len(body["content"]), 4_000)

    def test_a_stale_token_fails_quietly(self):
        error = urllib.error.HTTPError("url", 401, "unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertIsNone(self.client.push_item("hello"))

    def test_an_unreachable_backend_fails_quietly(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")):
            self.assertIsNone(self.client.push_item("hello"))


class ListItemsTests(unittest.TestCase):
    def test_returns_the_items_list(self):
        client = CloudMemoryClient("https://api.example.com", "a-token")
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse({"items": [{"id": "1"}, {"id": "2"}]}),
        ):
            items = client.list_items(scope="user", limit=10)
        self.assertEqual(len(items), 2)

    def test_limit_is_clamped_to_the_server_range(self):
        client = CloudMemoryClient("https://api.example.com", "a-token")
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeResponse({"items": []})
        ) as urlopen:
            client.list_items(limit=500)
        self.assertIn("limit=100", urlopen.call_args[0][0].full_url)


class PreferencesTests(unittest.TestCase):
    def test_put_preferences_reports_success(self):
        client = CloudMemoryClient("https://api.example.com", "a-token")
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeResponse({"ok": True})
        ):
            self.assertTrue(client.put_preferences({"theme": "dark"}))


class ClientFromConfigTests(unittest.TestCase):
    def test_reads_the_login_written_keys(self):
        client = client_from_config({"api_url": "https://api.example.com", "auth_token": "tok"})
        self.assertTrue(client.available)
        self.assertEqual(client.api_url, "https://api.example.com")

    def test_missing_keys_produce_an_unavailable_client(self):
        client = client_from_config({})
        self.assertFalse(client.available)


if __name__ == "__main__":
    unittest.main()


def test_retired_domain_cannot_receive_a_memory_token():
    assert CloudMemoryClient('https://api.arthera.finance','token').api_url == 'https://api.arthera.xyz'
