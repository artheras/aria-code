"""Finding the Google Cloud project without a shell.

Google Cloud video generation failed in the desktop app with "Set
VERTEX_PROJECT (or GOOGLE_CLOUD_PROJECT)" even though `gcloud auth
application-default login` had been run and the credentials worked. The cause
was not the env var: `google.auth.default()` resolves the project for a user
credential by *running gcloud*, and an app launched from Finder has no gcloud
on PATH, so it returned None and the workflow reported that as "set the env
var" — which sends you off to fix something that was never wrong.

The project is written on disk by that same login, so it is read from there.
"""
from __future__ import annotations

import json

import pytest

from aria_code.video_workflow import google_models


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """No env var and no real gcloud home, so each test states its own world."""
    for name in ("VERTEX_PROJECT", "GOOGLE_CLOUD_PROJECT", "CLOUDSDK_CONFIG"):
        monkeypatch.delenv(name, raising=False)


def _gcloud_home(monkeypatch, tmp_path, *, adc=None, config=None):
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    if adc is not None:
        (tmp_path / "application_default_credentials.json").write_text(json.dumps(adc))
    if config is not None:
        (tmp_path / "configurations").mkdir(parents=True, exist_ok=True)
        (tmp_path / "configurations" / "config_default").write_text(config)


def test_env_var_still_wins(monkeypatch):
    monkeypatch.setenv("VERTEX_PROJECT", "from-env")
    assert google_models.vertex_project() == "from-env"


def test_falls_back_to_the_project_adc_was_logged_in_with(monkeypatch, tmp_path):
    _gcloud_home(monkeypatch, tmp_path, adc={"type": "authorized_user", "quota_project_id": "example-project"})
    assert google_models._adc_config_project() == "example-project"


def test_falls_back_to_the_gcloud_default_project(monkeypatch, tmp_path):
    _gcloud_home(monkeypatch, tmp_path, config="[core]\naccount = a@b.c\nproject = example-project\n")
    assert google_models._adc_config_project() == "example-project"


def test_a_machine_that_never_logged_in_reports_nothing(monkeypatch, tmp_path):
    _gcloud_home(monkeypatch, tmp_path)
    assert google_models._adc_config_project() == ""


def test_unreadable_files_do_not_raise(monkeypatch, tmp_path):
    # A half-written or foreign-format file must not take the workflow down.
    _gcloud_home(monkeypatch, tmp_path, adc={"type": "authorized_user"}, config="not ini at all")
    assert google_models._adc_config_project() == ""


def test_the_error_names_the_thing_that_actually_fixes_it(monkeypatch):
    monkeypatch.setattr(google_models, "vertex_project", lambda: "")
    with pytest.raises(google_models.GoogleModelError) as excinfo:
        google_models._post("https://example.invalid/x:generateContent", {}, 1.0)
    assert "application-default login" in str(excinfo.value)


def test_no_gcloud_on_path_still_finds_the_project(monkeypatch, tmp_path):
    """The failing case in full: google.auth answers None, the disk answers."""
    monkeypatch.setattr(google_models, "_adc_config_project", lambda: "example-project")
    monkeypatch.setitem(
        __import__("sys").modules, "google.auth", None
    )  # importing it raises, as in a venv without google-auth
    assert google_models.vertex_project() == "example-project"


class _Response:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _capture_posts(monkeypatch, responses):
    """Answer each POST from `responses`; return the list of calls made."""
    import requests

    calls = []
    monkeypatch.setattr(google_models, "vertex_project", lambda: "example-project")
    monkeypatch.setattr(google_models, "_access_token", lambda: "token")
    monkeypatch.setattr(google_models.time, "sleep", lambda _s: None)
    monkeypatch.setattr(requests, "post", lambda *a, **k: (calls.append(k), responses.pop(0))[1])
    return calls


def test_a_busy_endpoint_is_retried_rather_than_failing_the_plan(monkeypatch):
    # Vertex's shared endpoints return 429 under load; the first run of a plan
    # that had already done a minute of work died on one.
    calls = _capture_posts(monkeypatch, [_Response(429), _Response(429), _Response(200, {"ok": True})])
    assert google_models._post("https://x/models/gemini:generateContent", {}, 1.0) == {"ok": True}
    assert len(calls) == 3


def test_giving_up_on_busy_says_so_in_words_the_user_can_act_on(monkeypatch):
    _capture_posts(monkeypatch, [_Response(429) for _ in range(4)])
    with pytest.raises(google_models.GoogleModelError, match="busy right now"):
        google_models._post("https://x/models/gemini:generateContent", {}, 1.0)


def test_a_real_error_is_not_retried(monkeypatch):
    # A bad request will never become good; retrying it only wastes a minute.
    calls = _capture_posts(monkeypatch, [_Response(400, text="bad argument")])
    with pytest.raises(google_models.GoogleModelError, match="bad argument"):
        google_models._post("https://x/models/gemini:generateContent", {}, 1.0)
    assert len(calls) == 1


def test_a_suspended_billing_account_is_named_as_such(monkeypatch):
    # Vertex refuses a project whose billing is suspended with "Lightning
    # dunning decision is deny for project: projects/<number>" — wording that
    # reads like a fault in the request, and sends you through the code and
    # the credentials looking for something that is not there.
    _capture_posts(monkeypatch, [_Response(403, text='{"error":{"message":"Lightning dunning decision is deny for project: projects/123456789012"}}')])
    with pytest.raises(google_models.GoogleModelError, match="billing account"):
        google_models._post("https://x/models/gemini:generateContent", {}, 1.0)


def test_an_ordinary_refusal_still_reports_what_google_said(monkeypatch):
    _capture_posts(monkeypatch, [_Response(403, text="caller lacks aiplatform.endpoints.predict")])
    with pytest.raises(google_models.GoogleModelError, match="aiplatform.endpoints.predict"):
        google_models._post("https://x/models/gemini:generateContent", {}, 1.0)


def test_a_model_this_project_cannot_reach_says_so(monkeypatch):
    _capture_posts(monkeypatch, [_Response(404, text="<!DOCTYPE html>")])
    with pytest.raises(google_models.GoogleModelError, match="not available to this project"):
        google_models._post("https://x/models/veo-9:generateContent", {}, 1.0)
