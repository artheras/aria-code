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
    from apps.cli.providers.chat_routing import normalize_provider_name
    return normalize_provider_name(name)


def _detect_lang() -> str:
    from apps.cli.i18n import detect_system_lang
    return detect_system_lang()


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
        on_loaded=sync_policy,
    )


def load_cli_config(
    paths: AriaConfigPaths,
    defaults: dict,
    *,
    sync_policy: Callable[[dict], None] | None = None,
) -> dict:
    """Load config.json, merge with defaults, then apply the project's .ariarc.

    The i18n/chat_routing imports below are bare on purpose: the tests
    patch those modules, and the packaged root is a different module
    object, so the packaged form left detect_system_lang unpatched.

    The .ariarc overlay goes last so a repo that pins a model gets it for
    everyone who opens it, without each developer editing their global config.
    It is best-effort: a malformed project file must not stop the CLI from
    starting, because the user would then have no way to run the tool that
    would fix it.
    """
    service = build_settings_service(paths, defaults, sync_policy=sync_policy)
    config = service.load()
    # The old localhost default is replaced once; see endpoint_migration.
    from aria_code.apps.cli.endpoint_migration import migrate_config as migrate_endpoint

    moved = migrate_endpoint(config)
    if moved:
        try:
            service.save(config)
        except Exception:
            pass
        _announce_endpoint(moved, config)
    # A model Google has retired fails on every request. The user's own saved
    # choice is replaced once and written back; see model_retirement.
    from aria_code.apps.cli.model_retirement import migrate_config

    saved = migrate_config(config)
    if saved:
        try:
            service.save(config)
        except Exception:
            pass
        _announce(saved, config, pinned=False)
    try:
        from aria_code.ariarc import apply_to_config

        apply_to_config(config)
    except Exception:
        pass
    # A project's .ariarc is the user's file: replace in memory, say so, never rewrite it.
    pinned = migrate_config(config)
    if pinned:
        _announce(pinned, config, pinned=True)
    return config


_ANNOUNCED: set[tuple[str, str]] = set()


def _announce_endpoint(changes, config: dict) -> None:
    """Say once per process that the old localhost default was replaced."""
    import sys

    zh = str(config.get("ui_lang", "")).lower().startswith("zh")
    for _key, old, new in changes:
        if (old, new) in _ANNOUNCED:
            continue
        _ANNOUNCED.add((old, new))
        if zh:
            msg = (f"ℹ 服务器地址 {old} 是旧版默认值，已改为 {new}。"
                   "如需使用本机后端，请设置环境变量 ARTHERA_API_URL，或在配置里加 api_url_pinned: true。")
        else:
            msg = (f"ℹ The server address {old} was the old default and is now {new}. "
                   "For a local backend set ARTHERA_API_URL, or add api_url_pinned: true to the config.")
        try:
            print(msg, file=sys.stderr)
        except Exception:
            pass


def _announce(changes, config: dict, *, pinned: bool) -> None:
    """One line on stderr per retired model, once per process."""
    import sys

    zh = str(config.get("ui_lang", "")).lower().startswith("zh")
    for _key, old, new in changes:
        if (old, new) in _ANNOUNCED:
            continue
        _ANNOUNCED.add((old, new))
        if zh:
            where = "项目 .ariarc 里固定的" if pinned else "你设置的"
            tail = "，请修改 .ariarc 里的 model" if pinned else "。可以用 /model 换成其他模型"
            msg = f"ℹ {where}模型 {old} 已被 Google 停用，本次改用 {new}{tail}。"
        else:
            where = "The model pinned in this project's .ariarc" if pinned else "Your model"
            tail = "; update the model in .ariarc" if pinned else "; use /model to pick another"
            msg = f"ℹ {where}, {old}, has been retired by Google. Using {new}{tail}."
        try:
            print(msg, file=sys.stderr)
        except Exception:
            pass


def save_cli_config(paths: AriaConfigPaths, cfg: dict) -> None:
    build_settings_service(paths, {}).save(cfg)
