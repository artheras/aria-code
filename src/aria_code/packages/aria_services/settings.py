"""SettingsService — single owner of persistent Aria configuration.

This is the concrete implementation behind the ``settings`` ServiceSpec in
``packages.aria_services.registry`` (capability ``settings.config``). It owns
the merge/normalize/persist mechanics that previously lived in
``apps/cli/config_store.py``; that module is now a thin CLI adapter which
constructs this service with CLI-specific hooks.

Layering: this package must stay importable by launcher, daemon, brokers and
MCP without dragging in the CLI. Anything CLI-flavored (provider-name
normalization, i18n language detection, model auto-selection, policy sync) is
injected as an optional callable rather than imported from ``apps.cli``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# Models that no longer exist / were renamed; a saved config pointing at one
# is silently reset to the caller's default so the CLI never boots into a
# permanently-broken model selection.
STALE_ARIA_MODEL_PREFIXES = ("aria-opus", "aria-prelude", "aria-sonata:3", "aria-sonata:4")

# Keys that are session state, not configuration — never persisted.
NEVER_PERSIST = frozenset({
    "conversation_history", "_session_workspace_root", "_session_write_roots",
    "_session_read_roots", "_workspace_transport",
})


@dataclass
class SettingsService:
    """Load, normalize, persist, and hand out the Aria config dict.

    ``config_dir``/``config_file``/``sessions_dir`` duck-type
    ``apps.cli.config_paths.AriaConfigPaths`` so the CLI can pass its existing
    paths object fields straight through.
    """

    config_dir: Path
    config_file: Path
    sessions_dir: Path
    defaults: dict = field(default_factory=dict)
    # Optional environment hooks (all safe to omit):
    normalize_provider: Optional[Callable[[str], str]] = None
    detect_lang: Optional[Callable[[], str]] = None
    # There was an auto_select_model hook here. It probed Ollama on first run
    # and replaced the caller's default model with whatever was installed; see
    # load(). Removed rather than left unwired, so it is not reconnected by
    # someone who finds an unused hook and assumes it should be doing something.
    on_loaded: Optional[Callable[[dict], None]] = None  # e.g. CLI policy sync
    stale_model_prefixes: tuple = STALE_ARIA_MODEL_PREFIXES

    _snapshot: Optional[dict] = field(default=None, repr=False)

    # ── core API ─────────────────────────────────────────────────────────────

    def load(self) -> dict:
        """Read config file, merge over defaults, normalize, fire on_loaded."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        if self.config_file.exists():
            try:
                saved = json.loads(self.config_file.read_text(encoding="utf-8"))
                merged = {**self.defaults, **saved}
                self._infer_local_provider(merged, saved)
                self._reset_stale_model(merged)
                self._ensure_ui_lang(merged)
                self._finish_load(merged)
                return merged
            except Exception:
                pass

        # First run: the defaults stand as written. This used to probe Ollama
        # here and overwrite cfg["model"] with whatever was installed, which
        # contradicted the comment above DEFAULT_MODEL in apps/cli/bootstrap.py
        # -- "provider configuration is the user's to make ... the code's job is
        # to have a sane default and then get out of the way". It also cost a
        # 3s HTTP timeout on every first start, and it was not needed for
        # discoverability: the banner already reports "Ollama online · N models"
        # (ui/banner.ollama_status_label), so a user with local models can see
        # them and run /model.
        cfg = dict(self.defaults)
        try:
            if self.detect_lang is not None:
                cfg["ui_lang"] = self.detect_lang()
        except Exception:
            cfg["ui_lang"] = "en"
        cfg.setdefault("ui_lang", "en")
        self._finish_load(cfg)
        return cfg

    def save(self, cfg: dict) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in cfg.items() if k not in NEVER_PERSIST}
        # config.json holds auth_token / refresh_token from /login, so it is
        # written owner-only like the other credential files.
        from aria_code.packages.aria_core.secure_file import write_secret_json

        write_secret_json(self.config_file, payload)
        self._snapshot = dict(cfg)

    def get(self, key: str, default: Any = None) -> Any:
        """Read one key from the last-loaded snapshot (loads on first use)."""
        if self._snapshot is None:
            self.load()
        return self._snapshot.get(key, default)

    def set(self, key: str, value: Any) -> dict:
        """Update one key and persist immediately; returns the new snapshot."""
        if self._snapshot is None:
            self.load()
        cfg = dict(self._snapshot)
        cfg[key] = value
        self.save(cfg)
        return cfg

    def snapshot(self) -> dict:
        """The last-loaded/saved config dict (loads on first use)."""
        if self._snapshot is None:
            self.load()
        return dict(self._snapshot)

    # ── normalization steps (extracted verbatim from the CLI loader) ─────────

    def _infer_local_provider(self, merged: dict, saved: dict) -> None:
        saved_model = merged.get("model", "")
        if "local_provider" not in saved:
            merged["local_provider"] = (
                saved_model.split("/", 1)[0].lower() if "/" in saved_model else "ollama"
            )
        if self.normalize_provider is not None:
            try:
                merged["local_provider"] = (
                    self.normalize_provider(merged.get("local_provider")) or "ollama"
                )
            except Exception:
                pass

    def _reset_stale_model(self, merged: dict) -> None:
        model = merged.get("model", "")
        if any(model.startswith(p) for p in self.stale_model_prefixes):
            merged["model"] = self.defaults.get("model", "")

    def _ensure_ui_lang(self, merged: dict) -> None:
        if not merged.get("ui_lang"):
            try:
                merged["ui_lang"] = self.detect_lang() if self.detect_lang else "en"
            except Exception:
                merged["ui_lang"] = "en"

    def _finish_load(self, cfg: dict) -> None:
        self._snapshot = dict(cfg)
        if self.on_loaded is not None:
            self.on_loaded(cfg)
