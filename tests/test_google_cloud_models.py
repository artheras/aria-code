"""Gemini on Google Cloud must work with what a user actually has, through their proxy.

Found by running Gemini 2.5 Pro on Vertex AI from a machine that reaches
Google only through a system proxy, logged in with `gcloud auth login`:

- Google models went to the google-genai SDK route, which accepts only
  application-default credentials, so a gcloud login alone failed with
  "default credentials were not found".
- The gcloud fallback read the project from `gcloud config get-value project`
  only (empty → ".../projects//locations/..."), fixed the region to
  us-central1, and ran both gcloud calls with no timeout on the event loop.
- None of the 23 aiohttp sessions honoured HTTP(S)_PROXY, unlike urllib, httpx
  and curl, so behind a proxy each model call hung until the 300-second
  timeout and then reported an empty error.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import re

import pytest

from aria_code.apps.cli import bootstrap
from aria_code.apps.cli.providers import base

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "aria_code"


@pytest.fixture
def no_google_env(monkeypatch, tmp_path):
    for name in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_APPLICATION_CREDENTIALS", "K_SERVICE",
                 "GCE_METADATA_HOST", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "gcloud"))
    monkeypatch.setattr("aria_code.providers.llm.registry._load_provider_cfg_from_file", lambda name: {})


def fake_gcloud(monkeypatch, *, project="", token="tok"):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gcloud" if name == "gcloud" else None)
    answers = {("config", "get-value", "project"): project, ("auth", "print-access-token"): token}
    monkeypatch.setattr(base, "_gcloud", lambda *args, timeout=20: answers[args])


class TestVertexEndpoint:
    def test_no_gcloud_means_no_fallback(self, no_google_env, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert base.vertex_openai_endpoint({}) == {}

    def test_project_from_the_environment_first(self, no_google_env, monkeypatch):
        fake_gcloud(monkeypatch, project="from-gcloud")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "example-project")
        got = base.vertex_openai_endpoint({})
        assert got["base_url"] == ("https://aiplatform.googleapis.com/v1/projects/example-project/"
                                   "locations/global/endpoints/openapi")

    def test_a_regional_endpoint(self, no_google_env, monkeypatch):
        fake_gcloud(monkeypatch, project="p")
        got = base.vertex_openai_endpoint({"gcp_location": "europe-west2"})
        assert got["base_url"].startswith("https://europe-west2-aiplatform.googleapis.com/v1/projects/p/")

    @pytest.mark.parametrize("project", ["", "(unset)"])
    def test_no_project_is_an_error_not_an_empty_url(self, no_google_env, monkeypatch, project):
        fake_gcloud(monkeypatch, project=project)
        assert base.vertex_openai_endpoint({})["error"].startswith("vertex_needs_project")

    def test_not_logged_in(self, no_google_env, monkeypatch):
        fake_gcloud(monkeypatch, project="p", token="")
        assert base.vertex_openai_endpoint({})["error"].startswith("vertex_not_logged_in")


class TestWhichRoute:
    def test_gcloud_login_only(self, no_google_env, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gcloud")
        assert base._gcloud_login_only({})

    def test_adc_file_keeps_the_sdk_route(self, no_google_env, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gcloud")
        (tmp_path / "gcloud").mkdir()
        (tmp_path / "gcloud" / "application_default_credentials.json").write_text("{}")
        assert not base._gcloud_login_only({})

    def test_an_api_key_keeps_the_sdk_route(self, no_google_env, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gcloud")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        assert not base._gcloud_login_only({})

    def test_a_gcloud_login_reaches_the_openai_compatible_endpoint(self, no_google_env, monkeypatch):
        fake_gcloud(monkeypatch, project="p")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
        seen = {}

        class Local:
            @classmethod
            def from_config(cls, cfg):
                seen.update(cfg)
                return cls()

            async def stream(self, messages, tools, cancel_event=None):
                yield {"type": "done", "response": "ready"}

        monkeypatch.setattr("aria_code.local_llm_provider.LocalLLMProvider", Local)

        async def run():
            return [e async for e in base.ConfiguredProvider({}, "google/gemini-2.5-pro").stream([], tools=[])]

        asyncio.run(run())
        assert seen["custom_endpoint"].endswith("/projects/p/locations/global/endpoints/openapi")
        assert seen["custom_model"] == "google/gemini-2.5-pro"


class TestProxy:
    def test_every_aiohttp_session_honours_the_system_proxy(self):
        offending = []
        for path in sorted(SRC.rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"aiohttp\.ClientSession\(", line) and "trust_env=True" not in line:
                    offending.append(f"{path.relative_to(SRC)}:{number}")
        assert not offending, offending

    def test_local_services_bypass_the_proxy(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:15236")
        monkeypatch.setenv("NO_PROXY", "corp.internal,localhost")
        monkeypatch.delenv("no_proxy", raising=False)
        bootstrap.ensure_loopback_bypasses_proxy()
        assert os.environ["NO_PROXY"] == "corp.internal,localhost,127.0.0.1,::1"

    def test_no_proxy_means_nothing_changes(self, monkeypatch):
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy",
                     "NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        bootstrap.ensure_loopback_bypasses_proxy()
        assert "NO_PROXY" not in os.environ

