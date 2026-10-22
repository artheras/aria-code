import json

import pytest

from aria_code.apps.cli.bootstrap import default_config
from aria_code.apps.cli.endpoint_migration import LEGACY_DEFAULT_API_URL, PRODUCTION_API_URL, migrate_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ARTHERA_API_URL", raising=False)


def test_a_new_install_defaults_to_the_production_gateway_not_localhost():
    assert default_config()["api_url"] == PRODUCTION_API_URL
    # The local backend setting is a separate concept and is unchanged.
    assert default_config()["local_url"] == "http://localhost:8000"


@pytest.mark.parametrize("old", [LEGACY_DEFAULT_API_URL, LEGACY_DEFAULT_API_URL + "/", "HTTP://LOCALHOST:8000"])
def test_the_old_saved_default_is_replaced_once(old):
    cfg = {"api_url": old, "local_mode": True, "model": "qwen3:8b"}
    changes = migrate_config(cfg)
    assert cfg["api_url"] == PRODUCTION_API_URL
    assert changes == [("api_url", old, PRODUCTION_API_URL)]
    assert migrate_config(cfg) == []  # nothing left to move
    # Choosing a local model is a different decision and is not touched.
    assert cfg["local_mode"] is True and cfg["model"] == "qwen3:8b"


@pytest.mark.parametrize("chosen", ["https://staging.example.com", "http://localhost:9000", "http://127.0.0.1:8000"])
def test_any_other_address_is_a_choice_and_is_left_alone(chosen):
    cfg = {"api_url": chosen}
    assert migrate_config(cfg) == [] and cfg["api_url"] == chosen


def test_pinning_keeps_localhost():
    cfg = {"api_url": LEGACY_DEFAULT_API_URL, "api_url_pinned": True}
    assert migrate_config(cfg) == [] and cfg["api_url"] == LEGACY_DEFAULT_API_URL


def test_the_environment_overrides_a_saved_value_without_reporting_a_migration(monkeypatch):
    monkeypatch.setenv("ARTHERA_API_URL", "http://localhost:8000/")
    cfg = {"api_url": "https://api.arthera.finance"}
    assert migrate_config(cfg) == []
    assert cfg["api_url"] == "http://localhost:8000"
    # So a developer with a local backend keeps it by setting one variable.
    cfg = {"api_url": LEGACY_DEFAULT_API_URL}
    assert migrate_config(cfg) == [] and cfg["api_url"] == "http://localhost:8000"


def test_loading_a_real_config_file_migrates_and_writes_back(tmp_path, monkeypatch):
    from aria_code.apps.cli.bootstrap import runtime_paths
    from aria_code.apps.cli.config_store import load_cli_config

    monkeypatch.setenv("ARIA_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"api_url": LEGACY_DEFAULT_API_URL, "model": "qwen3:8b"}))
    cfg = load_cli_config(runtime_paths(), default_config())
    assert cfg["api_url"] == PRODUCTION_API_URL
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["api_url"] == PRODUCTION_API_URL


@pytest.mark.parametrize("old", ["https://api.arthera.finance", "https://api.arthera.finance/", "https://cn.arthera.finance"])
def test_expired_domain_moves_to_current_gateway(old):
    cfg = {"api_url":old}
    assert migrate_config(cfg) == [("api_url",old,PRODUCTION_API_URL)]
    assert cfg["api_url"] == "https://api.arthera.xyz"


def test_pinned_expired_domain_is_still_retired():
    cfg={'api_url':'https://api.arthera.finance','api_url_pinned':True}
    migrate_config(cfg)
    assert cfg['api_url']=='https://api.arthera.xyz'
