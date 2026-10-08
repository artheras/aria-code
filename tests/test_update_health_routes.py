"""Release metadata and service diagnostics follow the selected installation and route."""

import io
import time
import unittest
from unittest.mock import patch

from aria_code.apps.cli import update_check
from aria_code.apps.cli.commands.diagnostic_cmds import _health_targets


class UpdateAndHealthTests(unittest.TestCase):
    def test_known_legacy_releases_migrate_without_breaking_future_versions(self):
        for version in ("4.1.3", "4.1.4", "4.1.7", "4.2.0", "4.3.0", "4.4.0", "4.4.1", "4.4.2"):
            with self.subTest(version=version):
                self.assertTrue(update_check._newer("v0.110.0", version))
                self.assertFalse(update_check._newer(version, "0.110.0"))
        self.assertTrue(update_check._newer("0.110.0", "0.79.0"))
        self.assertFalse(update_check._newer("0.110.0", "0.110.0"))
        self.assertFalse(update_check._newer("0.110.0", "4.5.0"))
        self.assertTrue(update_check._newer("5.0.0", "4.4.2"))

    def test_legacy_user_receives_the_current_release_notice(self):
        with patch.object(update_check, "_read_cache", return_value={
            "source": update_check._RELEASE_URL,
            "checked_at": time.time(), "latest": "v0.110.0",
        }):
            update_check._notice = None
            update_check._worker("4.4.2", "en", "pip")
        self.assertIn("v0.110.0", update_check._notice)

    def test_old_unscoped_registry_cache_is_ignored(self):
        with patch.object(update_check, "_read_cache", return_value={
            "checked_at": time.time(), "latest": "4.1.0",
        }), patch("urllib.request.urlopen", side_effect=OSError("offline")) as fetch:
            update_check._notice = None
            update_check._worker("0.56.0", "en")
        fetch.assert_called_once()
        self.assertIsNone(update_check._notice)

    def test_release_cache_and_version_are_validated(self):
        self.assertTrue(update_check._newer("v0.62.0", "0.56.0"))
        self.assertFalse(update_check._newer("v4.1.0-beta", "0.56.0"))
        self.assertFalse(update_check._newer("not-a-version", "0.56.0"))
        with patch.object(update_check, "_read_cache", return_value={
            "source": update_check._RELEASE_URL,
            "checked_at": time.time(), "latest": "v0.62.0",
        }):
            update_check._notice = None
            update_check._worker("0.56.0", "en")
        self.assertIn("v0.62.0", update_check._notice)
        self.assertNotIn("vv0.62.0", update_check._notice)
        self.assertIn("aria update", update_check._notice)

    def test_each_install_channel_has_its_own_source_and_command(self):
        self.assertEqual(update_check._update_command("npm"), "npm install -g @artheras/aria-code@latest")
        self.assertEqual(update_check._update_command("pip", "0.74.0"),
                         'python3 -m pip install --upgrade "aria-code==0.74.0"')
        self.assertTrue(update_check._update_command("source").startswith("git pull"))
        self.assertIn("%2Faria-code", update_check._NPM_URL)
        with patch.object(update_check, "_read_cache", return_value={}), \
                patch.object(update_check, "_write_cache") as save, \
                patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"version":"0.62.0"}')) as fetch:
            update_check._notice = None
            update_check._worker("0.56.0", "en", "npm")
        self.assertEqual(fetch.call_args.args[0].full_url, update_check._NPM_URL)
        self.assertEqual(save.call_args.args[0]["source"], update_check._NPM_URL)
        self.assertIn("aria update", update_check._notice)

    def test_pypi_s_old_4x_line_is_never_offered_as_an_update(self):
        """PyPI's latest is 4.4.2, an older numbering; v0.73.0 users were told to 'upgrade' to it."""
        self.assertFalse(hasattr(update_check, "_PYPI_URL"))
        for channel in ("pip", "source", "native"):
            with self.subTest(channel=channel), \
                    patch.object(update_check, "_read_cache", return_value={}), \
                    patch.object(update_check, "_write_cache"), \
                    patch("urllib.request.urlopen",
                          return_value=io.BytesIO(b'{"tag_name":"v0.73.0"}')) as fetch:
                update_check._notice = None
                update_check._worker("0.73.0", "en", channel)
            self.assertEqual(fetch.call_args.args[0].full_url, update_check._RELEASE_URL)
            self.assertIsNone(update_check._notice)
        self.assertNotIn("4.4.2", update_check._update_command("pip"))
        self.assertIn('"aria-code<4"', update_check._update_command("pip"))

    def test_health_checks_only_the_active_cloud_backend(self):
        targets, message = _health_targets({
            "model": "gemini-3.8-flash", "backend_chat": True,
        }, "https://api.arthera.finance")
        self.assertEqual(targets, [
            ("Google Cloud · Arthera API", "https://api.arthera.finance", "/health"),
        ])
        self.assertEqual(message, "")

    def test_direct_cloud_provider_is_not_misreported_offline(self):
        targets, message = _health_targets({
            "model": "google/gemini-2.5-pro", "backend_chat": False,
        }, "https://api.arthera.finance")
        self.assertEqual(targets, [])
        self.assertIn("configured (not probed)", message)

    def test_local_route_checks_ollama_only(self):
        targets, message = _health_targets({
            "model": "qwen2.5:7b", "backend_chat": False,
            "local_provider": "ollama",
        }, "https://api.arthera.finance")
        self.assertEqual(targets[0][0], "Ollama")
        self.assertEqual(message, "")


if __name__ == "__main__":
    unittest.main()
