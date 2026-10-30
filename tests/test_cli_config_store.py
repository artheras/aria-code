from aria_code.apps.cli.config_paths import resolve_paths
from aria_code.apps.cli.config_store import load_cli_config, save_cli_config


def _no_project_ariarc(monkeypatch):
    """Neutralise any .ariarc above the test's cwd.

    load_cli_config now overlays the project file, so a test asserting the
    merge of defaults and saved values would otherwise pass or fail depending
    on whether the developer running it happens to have one.
    """
    import aria_code.ariarc as ariarc

    monkeypatch.setattr(ariarc, "find_ariarc", lambda *a, **k: None)
    monkeypatch.setattr(ariarc, "_CACHED_RC", None, raising=False)
    # Isolate the overlay itself too: other CLI tests can load the legacy
    # bare import alias before this module is collected.
    monkeypatch.setattr(ariarc, "apply_to_config", lambda cfg: cfg)


def test_load_cli_config_merges_saved_values_and_syncs(monkeypatch, tmp_path):
    _no_project_ariarc(monkeypatch)
    paths = resolve_paths(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(
        '{"model": "aria-opus-old", "ui_lang": "", "permission_mode": "read-only"}',
        encoding="utf-8",
    )
    # config_store imports `from apps.cli.i18n import detect_system_lang`, so
    # the bare root is the module object it actually calls. Patching the
    # packaged one left the real detector in place and ui_lang came back "en".
    monkeypatch.setattr("apps.cli.i18n.detect_system_lang", lambda: "zh")
    synced = []

    cfg = load_cli_config(
        paths,
        {"model": "qwen2.5-coder:1.5b", "ui_lang": "", "permission_mode": "workspace-write"},
        sync_policy=synced.append,
    )

    assert cfg["model"] == "qwen2.5-coder:1.5b"
    assert cfg["ui_lang"] == "zh"
    assert cfg["permission_mode"] == "read-only"
    assert synced == [cfg]


def test_save_cli_config_excludes_conversation_history(tmp_path):
    paths = resolve_paths(tmp_path)

    save_cli_config(paths, {"model": "test-model", "conversation_history": [{"role": "user"}]})

    text = paths.config_file.read_text(encoding="utf-8")
    assert "test-model" in text
    assert "conversation_history" not in text
