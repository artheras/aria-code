"""Retired Gemini models are replaced, not left to fail on every request.

Vertex AI retires gemini-2.5-pro, -flash and -flash-lite on 20 October 2026
(release notes: 16 October); 2.0 shut down on 1 June 2026. A config that
still names one would send every turn to a model that no longer answers.
"""

from __future__ import annotations

import json

import pytest

from aria_code.apps.cli.bootstrap import DEFAULT_MODEL
from aria_code.apps.cli.config_paths import resolve_paths
from aria_code.apps.cli.config_store import load_cli_config
from aria_code.apps.cli.model_catalog import MODELS
from aria_code.apps.cli.model_retirement import migrate_config, retired_replacement


@pytest.mark.parametrize("old,new", [
    ("google/gemini-2.5-pro", "google/gemini-3.8-flash"),
    ("google/gemini-2.5-flash", "google/gemini-3.8-flash"),
    ("gemini-2.5-flash", "gemini-3.8-flash"),
    ("vertex/gemini-2.5-flash-lite", "vertex/gemini-3.1-flash-lite"),
    ("google/gemini-2.5-pro-preview-06-05", "google/gemini-3.8-flash"),
    ("gemini-2.0-flash-001", "gemini-3.8-flash"),
    ("gemini-2.0-flash-lite", "gemini-3.1-flash-lite"),
    ("gemini/gemini-1.5-pro-latest", "gemini/gemini-3.8-flash"),
    ("GOOGLE/GEMINI-2.5-PRO", "GOOGLE/gemini-3.8-flash"),
    # The previous default: Google deprecated it on 2026-10-08, so it moves with the default.
    ("google/gemini-3.5-flash", "google/gemini-3.8-flash"),
    ("gemini-3.5-flash", "gemini-3.8-flash"),
])
def test_retired_ids_are_replaced(old, new):
    assert retired_replacement(old) == new


@pytest.mark.parametrize("model", [
    "google/gemini-3.8-flash", "google/gemini-4-argon", "gemini-3.1-flash-lite",
    "google/gemma-4-31b-it", "qwen2.5:7b", "qwen2.5-coder:7b", "gpt-4o-mini", "", None,
])
def test_current_models_are_left_alone(model):
    assert retired_replacement(model) is None


def test_the_default_and_the_catalogue_name_no_retired_model():
    assert retired_replacement(DEFAULT_MODEL) is None
    retired = [m["id"] for m in MODELS.values() if retired_replacement(m["id"])]
    assert retired == []


def test_migrate_config_reports_what_changed():
    cfg = {"model": "google/gemini-2.5-pro", "ui_lang": "en"}
    assert migrate_config(cfg) == [("model", "google/gemini-2.5-pro", "google/gemini-3.8-flash")]
    assert cfg["model"] == "google/gemini-3.8-flash"
    assert migrate_config(cfg) == []


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ARIA_HOME", str(tmp_path / ".aria-code"))
    monkeypatch.chdir(tmp_path)
    from aria_code import ariarc
    from aria_code.apps.cli import config_store
    config_store._ANNOUNCED.clear()
    monkeypatch.setattr(ariarc, "_current_rc", None)   # .ariarc is cached per process
    return tmp_path


def _paths(home):
    return resolve_paths(home / ".aria-code")


def test_a_saved_retired_model_is_replaced_and_written_back_once(home, capsys):
    paths = _paths(home)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(json.dumps({"model": "google/gemini-2.5-pro", "ui_lang": "en"}))
    cfg = load_cli_config(paths, {"model": DEFAULT_MODEL})
    assert cfg["model"] == "google/gemini-3.8-flash"
    assert json.loads(paths.config_file.read_text())["model"] == "google/gemini-3.8-flash"
    err = capsys.readouterr().err
    assert "gemini-2.5-pro" in err and "gemini-3.8-flash" in err
    load_cli_config(paths, {"model": DEFAULT_MODEL})
    assert capsys.readouterr().err == ""


def test_a_project_pin_is_replaced_in_memory_only(home, capsys):
    paths = _paths(home)
    (home / ".ariarc").write_text('{"model": "google/gemini-2.5-flash"}')
    before = (home / ".ariarc").read_text()
    cfg = load_cli_config(paths, {"model": DEFAULT_MODEL, "ui_lang": "en"})
    assert cfg["model"] == "google/gemini-3.8-flash"
    assert (home / ".ariarc").read_text() == before
    assert ".ariarc" in capsys.readouterr().err


def test_a_current_model_is_untouched(home, capsys):
    paths = _paths(home)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(json.dumps({"model": "google/gemini-3.8-flash", "ui_lang": "en"}))
    assert load_cli_config(paths, {"model": DEFAULT_MODEL})["model"] == "google/gemini-3.8-flash"
    assert capsys.readouterr().err == ""
