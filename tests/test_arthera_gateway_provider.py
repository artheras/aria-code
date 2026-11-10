"""Google models for a signed-in user, with nothing to configure.

`/login` used to give an identity and no model access: reaching a Google
model still needed a Gemini API key from AI Studio, or gcloud and a billed
Google Cloud project. This provider spends the session instead, so the
question worth holding still is where it gets that session and what it does
when there is not one.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from aria_code.providers.llm import arthera_gateway as gw
from aria_code.providers.llm.base import ProviderConfig


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write_config(self, data: dict):
        (self.dir / "config.json").write_text(json.dumps(data), encoding="utf-8")

    def test_the_token_login_stored_is_what_authorises_calls(self):
        self.write_config({"auth_token": "firebase-token"})
        with mock.patch.dict(os.environ, {"ARIA_HOME": str(self.dir)}):
            self.assertEqual(gw.session_token(), "firebase-token")

    def test_signed_out_is_no_token_rather_than_an_empty_one(self):
        # An empty credential reaches the server and comes back 401, which
        # tells the user nothing they can act on.
        self.write_config({"auth_token": None})
        with mock.patch.dict(os.environ, {"ARIA_HOME": str(self.dir)}):
            self.assertIsNone(gw.session_token())
            self.assertFalse(gw.ArtheraGatewayProvider.available())

    def test_a_missing_config_is_not_an_error(self):
        with mock.patch.dict(os.environ, {"ARIA_HOME": str(self.dir / "absent")}):
            self.assertIsNone(gw.session_token())

    def test_a_corrupt_config_is_not_an_error(self):
        (self.dir / "config.json").write_text("{ not json", encoding="utf-8")
        with mock.patch.dict(os.environ, {"ARIA_HOME": str(self.dir)}):
            self.assertIsNone(gw.session_token())

    def test_a_new_install_reads_the_session_where_login_wrote_it(self):
        # With no legacy ~/.arthera, /login saves under ~/.aria-code. The
        # provider used to look only in ~/.arthera, so on a new install signing
        # in never gave the user a model.
        home = self.dir / "home"
        (home / ".aria-code").mkdir(parents=True)
        (home / ".aria-code" / "config.json").write_text(
            json.dumps({"auth_token": "fresh-install"}), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "ARIA_HOME"}
        env["HOME"] = str(home)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(gw.session_token(), "fresh-install")

    def test_an_existing_install_keeps_reading_its_legacy_folder(self):
        home = self.dir / "home"
        (home / ".arthera").mkdir(parents=True)
        (home / ".arthera" / "config.json").write_text(
            json.dumps({"auth_token": "legacy"}), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "ARIA_HOME"}
        env["HOME"] = str(home)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(gw.session_token(), "legacy")

    def test_a_signed_in_user_is_available(self):
        self.write_config({"auth_token": "t"})
        with mock.patch.dict(os.environ, {"ARIA_HOME": str(self.dir)}):
            self.assertTrue(gw.ArtheraGatewayProvider.available())


class EndpointTest(unittest.TestCase):
    def test_the_gateway_is_the_openai_path_on_the_api(self):
        with mock.patch.dict(os.environ, {"ARIA_API_URL": ""}):
            self.assertEqual(gw.gateway_base_url(), "https://api.arthera.xyz/api/v2/llm/v1")

    def test_another_deployment_is_configuration_not_a_code_change(self):
        with mock.patch.dict(os.environ, {"ARIA_API_URL": "https://api-staging.example"}):
            self.assertEqual(gw.gateway_base_url(), "https://api-staging.example/api/v2/llm/v1")

    def test_a_trailing_slash_does_not_double_up(self):
        with mock.patch.dict(os.environ, {"ARIA_API_URL": "https://api.example/"}):
            self.assertEqual(gw.gateway_base_url(), "https://api.example/api/v2/llm/v1")


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        Path(self.tmp.name, "config.json").write_text(json.dumps({"auth_token": "t"}), encoding="utf-8")

    def test_it_builds_itself_from_the_session_with_nothing_supplied(self):
        with mock.patch.dict(os.environ, {"ARIA_HOME": self.tmp.name, "ARIA_API_URL": ""}):
            provider = gw.ArtheraGatewayProvider(ProviderConfig(name="arthera", model=""))
        self.assertEqual(provider.config.api_key, "t")
        self.assertEqual(provider.config.base_url, "https://api.arthera.xyz/api/v2/llm/v1")

    def test_an_explicit_base_url_is_respected(self):
        with mock.patch.dict(os.environ, {"ARIA_HOME": self.tmp.name}):
            provider = gw.ArtheraGatewayProvider(
                ProviderConfig(name="arthera", model="", base_url="https://elsewhere.example/v1"))
        self.assertEqual(provider.config.base_url, "https://elsewhere.example/v1")


class RegistryTest(unittest.TestCase):
    def test_the_provider_is_reachable_by_name(self):
        from aria_code.providers.llm import registry

        self.assertIn("arthera", registry._PROVIDER_CLASSES)

    def test_it_comes_before_the_providers_that_need_a_key(self):
        # A signed-in user should not be asked for a key to reach a model
        # they already have.
        from aria_code.providers.llm import registry

        names = [entry[0] for entry in registry._DEFAULT_FALLBACK_CHAIN]
        keyed = [entry[0] for entry in registry._DEFAULT_FALLBACK_CHAIN if entry[1]]
        self.assertLess(names.index("arthera"), min(names.index(n) for n in keyed))


if __name__ == "__main__":
    unittest.main()
