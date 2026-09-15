"""Persistent CLI configuration loading and saving.

CLI adapter for :class:`packages.aria_services.settings.SettingsService` —
the service owns the merge/normalize/persist mechanics; this module supplies
the CLI-specific hooks (provider-name normalization, i18n detection, model
auto-selection) and keeps the historical ``load_cli_config``/``save_cli_config``
API so existing call sites are untouched.
"""

from __future__ import annotations

from collections.abc import Callable

from aria_code.apps.cli.config_paths import AriaConfigPaths
from aria_code.packages.aria_services.settings import (  # noqa: F401  (re-exported)
    STALE_ARIA_MODEL_PREFIXES,
    SettingsService,
)


def _normalize_provider(name: str) -> str:
    from aria_code.apps.cli.providers.chat_routing import normalize_provider_name
    return normalize_provider_name(name)


def _detect_lang() -> str:
    from aria_code.apps.cli.i18n import detect_system_lang
    return detect_system_lang()


def _auto_select_model(ollama_url: str, fallback: str) -> str:
    from aria_code.apps.cli.i18n import auto_select_model
    return auto_select_model(ollama_url, fallback=fallback)


def build_settings_service(
    paths: AriaConfigPaths,
    defaults: dict,
    *,
    sync_policy: Callable[[dict], None] | None = None,
) -> SettingsService:
    """Construct the SettingsService with the CLI's environment hooks."""
    return SettingsService(
        config_dir=paths.config_dir,
        config_file=paths.config_file,
        sessions_dir=paths.sessions_dir,
        defaults=defaults,
        normalize_provider=_normalize_provider,
        detect_lang=_detect_lang,
        auto_select_model=_auto_select_model,
        on_loaded=sync_policy,
    )


def load_cli_config(
    paths: AriaConfigPaths,
    defaults: dict,
    *,
    sync_policy: Callable[[dict], None] | None = None,
) -> dict:
    """Load config.json, merge with defaults, then apply the project's .ariarc.

    The .ariarc overlay goes last so a repo that pins a model gets it for
    everyone who opens it, without each developer editing their global config.
    It is best-effort: a malformed project file must not stop the CLI from
    starting, because the user would then have no way to run the tool that
    would fix it.
    """
    config = build_settings_service(paths, defaults, sync_policy=sync_policy).load()
    try:
        from aria_code.ariarc import apply_to_config

        apply_to_config(config)
    except Exception:
        pass
    # A model Google retired (a saved 2.5 Pro, say) runs as its replacement.
    from aria_code.apps.cli.model_catalog import current_gemini_model

    if isinstance(config.get("model"), str):
        config["model"] = current_gemini_model(config["model"])
    return config


def save_cli_config(paths: AriaConfigPaths, cfg: dict) -> None:
    build_settings_service(paths, {}).save(cfg)
