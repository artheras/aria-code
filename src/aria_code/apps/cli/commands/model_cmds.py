"""
ModelCommandsMixin — Model/config commands: model, apikey, providers, cloud, config, tools, skills.

Extracted from aria_cli.py. Methods' __globals__ are rebound to aria_cli's namespace
by _rebind_mixin_globals() called at module load time.
"""
from __future__ import annotations

from aria_code.apps.cli.i18n import ui_text
from aria_code.packages.aria_core.paths import aria_home


import logging
import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional

# Import path → the name you actually type after `pip install`. Namespace
# packages make these differ, and guessing from the module name produces
# advice that fails: `google.genai` is shipped as `google-genai`, so the
# obvious `pip install google` installs an unrelated stub.
_PIP_NAMES = {
    "google.genai": "google-genai",
    "google.generativeai": "google-generativeai",
    "google.cloud": "google-cloud",
    "anthropic": "anthropic",
    "openai": "openai",
    "mistralai": "mistralai",
    "cohere": "cohere",
    "yfinance": "yfinance",
    "akshare": "akshare",
    "ccxt": "ccxt",
}


def _pip_name(module: str) -> str:
    """Best installable name for *module*, longest known prefix first."""
    if module in _PIP_NAMES:
        return _PIP_NAMES[module]
    for known, package in sorted(_PIP_NAMES.items(), key=lambda kv: -len(kv[0])):
        if module.startswith(known + "."):
            return package
    # Unknown: the top-level name is the best available guess, and it is right
    # for the ordinary single-package case.
    return module.split(".")[0].replace("_", "-")


def _actionable(provider: str, error: str) -> str:
    """Turn a probe failure into something the user can act on.

    The picker used to show ``ollama_err[:40]``, which for the most common
    failure produced ``[ollama: <urlopen error [Errno 61] Connection ref]`` —
    a raw Python exception, truncated mid-word, naming neither the cause nor
    the remedy. A connection refused on the local runtime port has exactly one
    meaning and one fix, so say them.
    """
    text = str(error or "")
    low = text.lower()
    if "errno 61" in low or "connection refused" in low:
        return "未运行 · 先执行 ollama serve" if provider == "ollama" else f"{provider} 未运行"
    if "timed out" in low or "timeout" in low:
        return "连接超时 · 检查服务地址与网络"
    if "name or service not known" in low or "nodename nor servname" in low:
        return "地址无法解析 · 检查 ollama_url 配置"
    if "no module named" in low:
        module = text.split("'")[1] if "'" in text else text.strip()
        return f"缺少 {module} · pip install {_pip_name(module)}"
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= 48 else collapsed[:47] + "…"


def detect_ollama_models_rich(*args, **kwargs):
    from aria_code.apps.cli.helpers import detect_ollama_models_rich as fn
    return fn(*args, **kwargs)
def _sync_write_policy(*args, **kwargs):
    from aria_code.aria_cli import _sync_write_policy as fn
    return fn(*args, **kwargs)
def _load_providers_json(*args, **kwargs):
    from aria_code.aria_cli import _load_providers_json as fn
    return fn(*args, **kwargs)
def _get__PROVIDER_KEY_MAP():
    from aria_code.aria_cli import _PROVIDER_KEY_MAP as val
    return val
def _get__LLM_SIGNUP_URLS():
    from aria_code.aria_cli import _LLM_SIGNUP_URLS as val
    return val
def _get__PROVIDER_BASE_URLS():
    from aria_code.aria_cli import _PROVIDER_BASE_URLS as val
    return val
def _get_ARIA_TOOLS():
    # Owned by apps/cli/tool_registry.py; aria_cli fills it in place.
    from ..tool_registry import ARIA_TOOLS as val
    return val
def _get_THINKING_MODES():
    from aria_code.aria_cli import THINKING_MODES as val
    return val
def _get__PROVIDER_GUIDE():
    from aria_code.aria_cli import _PROVIDER_GUIDE as val
    return val
def _get__DATA_KEY_MAP():
    from aria_code.aria_cli import _DATA_KEY_MAP as val
    return val
def _run_picker_in_thread(*args, **kwargs):
    from aria_code.aria_cli import _run_picker_in_thread as fn
    return fn(*args, **kwargs)
import pathlib
def _get_MODEL_ALIASES():
    from aria_code.aria_cli import MODEL_ALIASES as val
    return val
def _save_providers_json(*args, **kwargs):
    from aria_code.aria_cli import _save_providers_json as fn
    return fn(*args, **kwargs)
def _test_api_key(*args, **kwargs):
    from aria_code.aria_cli import _test_api_key as fn
    return fn(*args, **kwargs)
def _get_provider_key(*args, **kwargs):
    from aria_code.apps.cli.provider_keys import get_provider_key as fn
    return fn(*args, **kwargs)
def _get__DATA_SIGNUP_URLS():
    from aria_code.aria_cli import _DATA_SIGNUP_URLS as val
    return val
def _arrow_select(*args, **kwargs):
    # Re-exported by aria_cli; taken from its own module here.
    from aria_code.ui.picker import arrow_select as fn
    return fn(*args, **kwargs)
def _get_SKILLS():
    from aria_code.apps.cli.skills_catalog import SKILLS as val
    return val
def _get_LOCAL_TOOLS():
    # Owned by apps/cli/tool_registry.py; aria_cli fills it in place.
    from ..tool_registry import LOCAL_TOOLS as val
    return val
def get_model_capability(*args, **kwargs):
    from aria_code.apps.cli._optional import get_model_capability as fn
    return fn(*args, **kwargs)
def _save_data_key(*args, **kwargs):
    from aria_code.aria_cli import _save_data_key as fn
    return fn(*args, **kwargs)
def _get_MODELS():
    from aria_code.apps.cli.model_catalog import MODELS as val
    return val
def load_config(*args, **kwargs):
    from aria_code.aria_cli import load_config as fn
    return fn(*args, **kwargs)
def _null_ctx(*args, **kwargs):
    from ._ui import null_ctx as fn
    return fn(*args, **kwargs)
def _get__PROVIDER_DESC():
    from aria_code.aria_cli import _PROVIDER_DESC as val
    return val
def _get_PROVIDERS_FILE():
    from aria_code.aria_cli import PROVIDERS_FILE as val
    return val
def _get__HAS_MODEL_CAP():
    from aria_code.apps.cli._optional import HAS_MODEL_CAP as val
    return val
def _load_data_keys(*args, **kwargs):
    from aria_code.apps.cli.provider_keys import load_data_keys as fn
    return fn(*args, **kwargs)

import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional

# A real logger. This was a `def logger(*args, **kwargs)` forwarding to
# aria_cli, so every `logger.debug(...)` raised AttributeError — and all but
# one of them sit in an except block, where the failure replaced whatever
# error was being reported.
logger = logging.getLogger(__name__)


class ModelCommandsMixin:
    """Mixin: Model/config commands: model, apikey, providers, cloud, config, tools, skills."""

    async def cmd_model(self, args: str):
        name = args.strip()

        # A model Google has retired would fail on the next request; switch
        # to its replacement and say so (model_retirement.py).
        from aria_code.apps.cli.model_retirement import retired_replacement

        _replacement = retired_replacement(name)
        if _replacement:
            msg = ui_text(self, f"ℹ {name} 已被 Google 停用，改用 {_replacement}。",
                          f"ℹ {name} has been retired by Google; using {_replacement}.")
            self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
            name = _replacement

        # ── "provider/model" format (Open Interpreter style) ─────────────────
        # Examples: /model deepseek/deepseek-chat  /model ollama/qwen2.5:7b
        #           /model openai/gpt-4.5          /model openai/o3  /model openai/o4-mini
        if "/" in name and not name.startswith("http"):
            _prov, _mod = name.split("/", 1)
            from aria_code.apps.cli.providers.chat_routing import normalize_provider_name

            _prov = normalize_provider_name(_prov)
            _mod  = _mod.strip()
            _local_backends = {"ollama", "lmstudio", "vllm", "llamacpp", "jan", "custom"}
            if _prov not in _local_backends:
                # Cloud provider — check API key. Google models also run on
                # Vertex AI with Google Cloud credentials and no key; this
                # refused every `/model google/…` without a Gemini API key,
                # so a team running on Google Cloud could not pick Gemini 3
                # or Gemma at all.
                from aria_code.apps.cli.providers.base import google_cloud_available
                _key = _get_provider_key(_prov)
                _vertex_ok = _prov in ("google", "gemini", "vertexai") and google_cloud_available()
                if not _key and not _vertex_ok:
                    if _prov in ("google", "gemini", "vertexai"):
                        msg = ui_text(self,
                                      f"⚠ 需要 Google Cloud 凭据或 API key：运行 gcloud auth application-default login，"
                                      f"或 /apikey set {_prov} <key>",
                                      f"⚠ Needs Google Cloud credentials or an API key: run "
                                      f"gcloud auth application-default login, or /apikey set {_prov} <key>")
                    else:
                        msg = (f"⚠ {_prov} API key 未配置。"
                               f"运行: /apikey set {_prov} <key>")
                    self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
                    return
            self.terminal.config["local_provider"] = _prov
            self.terminal.config["model"] = _mod
            self.terminal.config["local_mode"] = _prov in _local_backends
            # /model is an explicit provider selection. A legacy
            # backend_chat=true value must not silently override it and route
            # the next turn through Aria SSE instead of the selected runtime.
            self.terminal.config["backend_chat"] = False
            self.context.save_config(self.terminal.config)
            msg = f"✓ 已切换到 {_prov}/{_mod}"
            self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
            return

        # Direct selection by number: /model 1 /model 2 … (Codex style)
        if name.isdigit():
            idx = int(name) - 1
            keys = list(_get_MODELS().keys())
            if 0 <= idx < len(keys):
                self._set_model(keys[idx])
            else:
                self.context.console.print(f"[dim]No model #{name}[/dim]" if self.context.has_rich else f"No model #{name}")
            return

        # Direct selection by key (case-insensitive): /model qwen7b
        if name.lower() in _get_MODELS():
            self._set_model(name.lower())
            return

        # Direct selection by alias: /model st / s / p / coder
        if name.lower() in _get_MODEL_ALIASES():
            self._set_model(_get_MODEL_ALIASES()[name.lower()])
            return

        # Direct selection by full Ollama model ID: /model qwen2.5-coder:1.5b
        if name and ":" in name:
            self._set_model_by_id(name, provider="ollama")
            return

        # ── Interactive picker (Codex style: numbered list + descriptions) ────
        ollama_url  = self.terminal.config.get("ollama_url", "http://localhost:11434")
        current_provider = str(
            self.terminal.config.get("local_provider") or "ollama"
        ).lower()
        current_id  = self.terminal.config.get("model", "qwen2.5:7b")
        try:
            from aria_code.apps.cli.i18n import t as _i18nt
            _lang = self.terminal.config.get("ui_lang", "en") or "en"
            _i18n = lambda k: _i18nt(k, lang=_lang)
        except Exception:
            _lang = "en"
            _i18n = lambda k: k

        _local_backends = {"ollama", "lmstudio", "vllm", "llamacpp", "jan", "custom"}
        if current_provider == "ollama":
            rich_models, ollama_err = detect_ollama_models_rich(ollama_url)
        elif current_provider in _local_backends:
            try:
                from local_llm_provider import LocalLLMProvider

                _runtime = LocalLLMProvider.from_config(self.terminal.config)
                _runtime_models = _runtime.list_models()
                rich_models = [
                    {
                        "name": model_name,
                        "size_label": "",
                        "family": "",
                        "quant": "",
                        "execution": "local",
                        "remote_model": "",
                        "remote_host": "",
                        "context_window": 0,
                        "capabilities": [],
                    }
                    for model_name in _runtime_models
                ]
                ollama_err = None if _runtime.is_available() else f"{current_provider} unavailable"
            except Exception as exc:
                rich_models, ollama_err = [], str(exc)
        else:
            # Cloud catalog APIs vary widely and can be expensive. Keep the
            # selected model visible and report credential readiness; users can
            # switch directly with /model provider/model.
            if _get_provider_key(current_provider):
                rich_models = [{
                    "name": current_id,
                    "size_label": "",
                    "family": "",
                    "quant": "",
                    "execution": "remote",
                    "remote_model": current_id,
                    "remote_host": current_provider,
                    "context_window": 0,
                    "capabilities": [],
                }]
                ollama_err = None
            else:
                rich_models = []
                ollama_err = f"API key missing · /apikey set {current_provider} <key>"
        installed_names = {m["name"] for m in rich_models}
        aria_ids        = {m["id"] for m in _get_MODELS().values()}
        runtime_by_id   = {m["name"]: m for m in rich_models}

        # ── Build picker title (one line, shown inside arrow_select header) ──
        _sel_model = _i18n("select_model")
        _installed = _i18n("installed")
        if ollama_err:
            _picker_title = f"{_sel_model}  [{current_provider}: {_actionable(current_provider, ollama_err)}]"
        else:
            n_local = sum(1 for m in rich_models if m.get("execution") != "remote")
            n_remote = len(rich_models) - n_local
            if current_provider == "ollama":
                _picker_title = (
                    f"{_sel_model}  Ollama {len(rich_models)} ready"
                    f" · {n_local} local · {n_remote} cloud"
                )
            else:
                _picker_title = (
                    f"{_sel_model}  {current_provider} {len(rich_models)} ready"
                    " · /model <provider>/<model>"
                )

        def _status_tag(mid: str, badge: str) -> str:
            """Return runtime status, not a hard-coded catalogue badge."""
            runtime = runtime_by_id.get(mid)
            if runtime and runtime.get("execution") == "remote":
                return "☁"
            return "●" if runtime else "○"

        # Get terminal width for safe label truncation
        try:
            _term_cols = os.get_terminal_size().columns
        except Exception:
            _term_cols = 80

        def _cjk_width(s: str) -> int:
            """Display-column width (CJK = 2 cols each)."""
            w = 0
            for ch in s:
                cp = ord(ch)
                w += 2 if (0x2E80 <= cp <= 0xA4CF or 0xAC00 <= cp <= 0xD7AF or
                           0xFF01 <= cp <= 0xFF60 or 0x3000 <= cp <= 0x303F) else 1
            return w

        def _cjk_truncate(s: str, max_cols: int) -> str:
            """Truncate s so its display width ≤ max_cols, adding … if cut."""
            w, out = 0, ""
            for ch in s:
                cw = 2 if (0x2E80 <= ord(ch) <= 0xA4CF or
                           0xAC00 <= ord(ch) <= 0xD7AF or
                           0xFF01 <= ord(ch) <= 0xFF60 or
                           0x3000 <= ord(ch) <= 0x303F) else 1
                if w + cw > max_cols:
                    return out + "…"
                out += ch
                w += cw
            return out

        def _short_desc(m: dict) -> str:
            """Single-line description with right-aligned meta tags."""
            desc  = m.get("description", "")
            badge = m.get("badge", "")
            extras = []
            runtime = runtime_by_id.get(m["id"], {})
            runtime_ctx = int(runtime.get("context_window") or 0)
            runtime_caps = set(runtime.get("capabilities") or [])
            if runtime_ctx:
                extras.append(f"ctx={runtime_ctx//1024}K")
                if "tools" in runtime_caps: extras.append("tools")
                if "thinking" in runtime_caps: extras.append("think")
            elif _get__HAS_MODEL_CAP():
                cap = get_model_capability(m["id"])
                extras.append(f"ctx={cap.context_window//1024}K")
                if cap.tool_calls: extras.append("tools")
                if cap.thinking:   extras.append("think")
            else:
                extras.append(f"ctx={m.get('num_ctx', 8192)//1024}K")
            if runtime.get("execution") == "remote":
                extras.insert(0, "Cloud")
            elif m["id"] in installed_names:
                extras.insert(0, "Local")
            elif badge in ("Fast", "Code", "Think"):
                extras.insert(0, badge)
            meta = "  " + " · ".join(extras) if extras else ""
            # prefix "  N. ☁ ModelName  " ≈ 24 cols; give description 60% of remaining
            _prefix_cols  = 24
            _avail        = max(30, _term_cols - _prefix_cols - len(meta) - 2)
            _desc_budget  = max(20, _avail * 3 // 4)   # 75% of available → description
            desc = _cjk_truncate(desc, _desc_budget)
            return f"{desc}{meta}"

        # Build option list (Codex: numbered, no separators within Aria section)
        options: list = []   # (label_str, desc_str)  for _arrow_select
        all_ids: list = []

        # ── Print numbered list only in non-interactive (-p) mode ────────────
        # In interactive TTY mode the arrow picker below already shows all items.
        # Printing twice causes the visual duplication seen in the session log.
        _is_tty = sys.stdin.isatty()
        idx_counter = 1
        if not _is_tty:
            # Non-interactive (-p mode): show static numbered list then return.
            # The arrow picker cannot run without a TTY.
            community_list = [cm for cm in rich_models if cm["name"] not in aria_ids]
            for key, m in _get_MODELS().items():
                mid    = m["id"]
                is_cur = mid == current_id
                status = _status_tag(mid, m.get("badge", ""))
                cur_tag = "  (current)" if is_cur else ""
                desc = _short_desc(m)
                line = f"  {idx_counter}. {status} {m['name']:<14s}  {desc}{cur_tag}"
                self.context.console.print(line) if self.context.has_rich else print(line)
                idx_counter += 1
            if community_list:
                self.context.console.print() if self.context.has_rich else print()
                lbl = "  Community (Ollama)"
                self.context.console.print(f"[dim]{lbl}[/dim]") if self.context.has_rich else print(lbl)
                for cm in community_list:
                    mid    = cm["name"]
                    is_cur = mid == current_id
                    cur_tag = "  (current)" if is_cur else ""
                    line = f"  {idx_counter}. ● {mid}{cur_tag}"
                    self.context.console.print(line) if self.context.has_rich else print(line)
                    idx_counter += 1
            self.context.console.print() if self.context.has_rich else print()
            self.context.console.print("  [dim]Use /model <id> to switch. E.g. /model deepseek/deepseek-chat[/dim]") if self.context.has_rich else print("  Use /model <id> to switch.")
            return

        # ── Build compact options for _arrow_select ────────────────────────
        # In TTY mode: include short description (static list is suppressed above).
        # In non-TTY: descriptions already shown in static list, keep labels short.
        num = 1
        picker_models = (
            list(_get_MODELS().values())
            if current_provider == "ollama"
            else [
                {
                    "id": item["name"], "name": item["name"],
                    "description": (
                        "可用模型" if str(_lang).lower().startswith("zh")
                        else "available model"
                    ),
                    "num_ctx": item.get("context_window") or 8192,
                }
                for item in rich_models
            ]
        )
        for m in picker_models:
            mid    = m["id"]
            status = _status_tag(mid, m.get("badge", ""))
            is_cur = " ✓" if mid == current_id else ""
            if _is_tty:
                desc_part = f"  {_short_desc(m)}"
            else:
                desc_part = ""
            label  = f"  {num}. {status} {m['name']}{is_cur}{desc_part}"
            options.append((label, ""))
            all_ids.append(mid)
            num += 1

        community = (
            [cm for cm in rich_models if cm["name"] not in aria_ids]
            if current_provider == "ollama" else []
        )
        if community:
            _comm_label = _i18n("community_models")
            options.append((f"  ── {_comm_label} ──", ""))
            all_ids.append(None)
            for cm in community:
                mid    = cm["name"]
                is_cur = " ◀" if mid == current_id else ""
                options.append((f"  {num}. ● {mid}{is_cur}", ""))
                all_ids.append(mid)
                num += 1

        if ollama_err and not rich_models:
            _unreach = _i18n("ollama_unreachable")
            options.append((f"  ── {_unreach} ──────────", ""))
            all_ids.append(None)

        if not any(model_id is not None for model_id in all_ids):
            message = (
                f"{current_provider}: "
                f"{_actionable(current_provider, ollama_err) if ollama_err else 'no models available'}"
            )
            self.context.console.print(f"[yellow]{message}[/yellow]") if self.context.has_rich else print(message)
            return

        # ── Run thread-based arrow picker (short labels = no line wrap) ────
        current_idx = next((i for i, mid in enumerate(all_ids) if mid == current_id), 0)

        while True:
            _controls_hint = (
                "↑↓  Enter  Esc/q 取消"
                if str(_lang).lower().startswith("zh")
                else "↑↓  Enter  Esc/q Cancel"
            )
            choice = await _run_picker_in_thread(
                options, current_idx,
                _picker_title,
                max_visible=min(9, len(options)),
                controls_hint=_controls_hint,
            )
            if choice < 0:
                _msg = _i18n("cancelled")
                self.context.console.print(f"[dim]{_msg}[/dim]" if self.context.has_rich else _msg)
                return
            if all_ids[choice] is None:
                current_idx = min(choice + 1, len(options) - 1)
                continue
            break

        self._set_model_by_id(all_ids[choice], provider=current_provider)

    def _set_model(self, key: str):
        """Set model by _get_MODELS() key."""
        m = _get_MODELS()[key]
        self._set_model_by_id(m["id"])

    def _set_model_by_id(self, model_id: str, provider: str | None = None):
        """Set model by Ollama model ID (works for both built-in and community models)."""
        self.terminal.config["model"] = model_id
        matched = next((m for m in _get_MODELS().values() if m["id"] == model_id), None)
        self.terminal.config["local_provider"] = provider or (
            "ollama" if matched is not None else
            self.terminal.config.get("local_provider", "ollama")
        )
        self.terminal.config["local_mode"] = (
            self.terminal.config["local_provider"]
            in {"ollama", "lmstudio", "vllm", "llamacpp", "jan", "custom"}
        )
        self.terminal.config["backend_chat"] = False
        self.terminal._actual_model = None  # reset: new config model, no known fallback yet
        self.context.save_config(self.terminal.config)
        # Pretty label
        for m in _get_MODELS().values():
            if m["id"] == model_id:
                runtime = "Ollama Cloud" if m.get("badge") == "Cloud" else "Ollama"
                if self.context.has_rich:
                    self.context.console.print(f"[bold]Model:[/bold] [bold]{m['name']} {m['version']}[/bold] "
                                  f"[dim]· {runtime}[/dim]")
                else:
                    print(f"Model: {m['name']} {m['version']} ({m['tag']})")
                return
        # Community / unknown model
        if self.context.has_rich:
            self.context.console.print(f"[bold]Model:[/bold] [bold]{model_id}[/bold]  [dim](local)[/dim]")
        else:
            print(f"Model: {model_id} (local)")

    def cmd_thinking(self, args: str):
        mode = args.strip().lower()

        # Direct set: /thinking on
        if mode in ("on", "thinking"):
            self.terminal.config["thinking_mode"] = "thinking"
        elif mode in ("off", "instant"):
            self.terminal.config["thinking_mode"] = "instant"
        elif mode == "auto":
            self.terminal.config["thinking_mode"] = "auto"
        elif mode:
            # Unknown mode, show picker
            pass
        else:
            # Interactive picker
            current = self.terminal.config.get("thinking_mode", "auto")
            mode_keys = list(_get_THINKING_MODES().keys())
            current_idx = mode_keys.index(current) if current in mode_keys else 0
            options = [(info["label"], info["description"]) for info in _get_THINKING_MODES().values()]
            choice = _arrow_select(options, selected=current_idx, title="Thinking Mode")
            if 0 <= choice < len(mode_keys):
                self.terminal.config["thinking_mode"] = mode_keys[choice]
            else:
                if self.context.has_rich:
                    self.context.console.print("[dim]No change[/dim]")
                else:
                    print("No change")
                return

        self.context.save_config(self.terminal.config)
        result = self.terminal.config["thinking_mode"]
        info = _get_THINKING_MODES().get(result, {})
        if self.context.has_rich:
            self.context.console.print(f"[green]Thinking: {info.get('label', result)}[/green]  [dim]{info.get('description', '')}[/dim]")
        else:
            print(f"Thinking: {result}")

    def cmd_skills(self, args: str):
        """List all available skills grouped by category."""
        import shlex

        raw_parts = shlex.split(args.strip()) if args.strip() else []
        if raw_parts and raw_parts[0].lower() == "add":
            if len(raw_parts) < 2:
                message = "Usage: /skills add owner/repository [--ref tag-or-sha]"
                self.context.console.print(f"[yellow]{message}[/yellow]") if self.context.has_rich else print(message)
                return
            ref = ""
            if "--ref" in raw_parts:
                ref_index = raw_parts.index("--ref")
                if ref_index + 1 >= len(raw_parts):
                    message = "--ref requires a tag, branch, or commit"
                    self.context.console.print(f"[red]{message}[/red]") if self.context.has_rich else print(message)
                    return
                ref = raw_parts[ref_index + 1]
            try:
                from aria_code.packages.aria_skills import install_catalog

                installed = install_catalog(raw_parts[1], ref=ref)
                message = (
                    f"Installed {installed.source.full_name} at {installed.revision[:12]} "
                    f"with {len(installed.skills)} verified skills"
                )
                self.context.console.print(f"[green]{message}[/green]") if self.context.has_rich else print(message)
            except Exception as exc:
                message = f"Skill catalog install failed: {exc}"
                self.context.console.print(f"[red]{message}[/red]") if self.context.has_rich else print(message)
            return

        categories = {}
        for s in _get_SKILLS():
            cat = s["category"]
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(s)

        cat_labels = {
            "research": "Research",
            "analysis": "Analysis",
            "strategy": "Strategy",
            "risk": "Risk Management",
            "quant": "Quantitative",
            "crypto": "Crypto",
            "tools": "Tools",
            "code": "Code Generation",
        }
        try:
            from aria_code.packages.aria_skills import (
                discover_external_skills,
                recent_skill_activation_traces,
            )

            external_skills = discover_external_skills()
            activation_traces = recent_skill_activation_traces()
        except Exception:
            external_skills = []
            activation_traces = []

        subcommand = args.strip().lower()
        if subcommand in {"trace", "traces"}:
            if self.context.has_rich:
                self.context.console.print("\n  [bold]Skill Activation Trace[/bold]")
                if not activation_traces:
                    self.context.console.print("  [dim]No skill activations recorded in this session.[/dim]\n")
                for trace in activation_traces:
                    state = "[green]active[/green]" if trace.activated else "[red]blocked[/red]"
                    self.context.console.print(
                        f"  {state}  [bold]{trace.qualified_name}[/bold]  "
                        f"[dim]{trace.reason} · score {trace.score:g} · {trace.integrity}[/dim]"
                    )
                self.context.console.print()
            else:
                print("\nSkill Activation Trace")
                for trace in activation_traces:
                    state = "active" if trace.activated else "blocked"
                    print(
                        f"  {state} {trace.qualified_name} {trace.reason} "
                        f"score={trace.score:g} integrity={trace.integrity}"
                    )
            return

        if subcommand in {"doctor", "status"}:
            if self.context.has_rich:
                self.context.console.print("\n  [bold]Portable Skill Doctor[/bold]")
                for skill in external_skills:
                    style = "green" if skill.integrity == "verified" else "yellow"
                    self.context.console.print(
                        f"  [{style}]{skill.integrity:8s}[/{style}]  "
                        f"[bold]{skill.qualified_name}[/bold]  "
                        f"[dim]v{skill.plugin_version or '-'} · "
                        f"scripts={skill.policy.script_execution}[/dim]"
                    )
                self.context.console.print()
            else:
                print("\nPortable Skill Doctor")
                for skill in external_skills:
                    print(
                        f"  {skill.integrity:8s} {skill.qualified_name} "
                        f"v{skill.plugin_version or '-'} scripts={skill.policy.script_execution}"
                    )
            return

        if self.context.has_rich:
            self.context.console.print()
            for cat, skills in categories.items():
                label = cat_labels.get(cat, cat.title())
                self.context.console.print(f"  [bold]{label}[/bold]")
                for s in skills:
                    args_hint = f"  [dim]{s.get('args', '')}[/dim]" if s.get("args") else ""
                    self.context.console.print(f"    [bold]{s['command']:20s}[/bold][dim]{s['description']}[/dim]{args_hint}")
                self.context.console.print()

            if external_skills:
                self.context.console.print("  [bold]Portable Workflows[/bold]")
                for skill in external_skills:
                    self.context.console.print(
                        f"    [bold]${skill.qualified_name}[/bold]  "
                        f"[dim]v{skill.plugin_version or '-'} · {skill.integrity}[/dim]\n"
                        f"      [dim]{skill.description}[/dim]"
                    )
                self.context.console.print()

            self.context.console.print("[dim]  Type a skill command to execute, e.g. /deep-analysis AAPL[/dim]\n")
        else:
            print("\nSkills:")
            for cat, skills in categories.items():
                label = cat_labels.get(cat, cat.title())
                print(f"\n  [{label}]")
                for s in skills:
                    print(f"    {s['command']:20s} {s['description']}")
            if external_skills:
                print("\n  [Portable Workflows]")
                for skill in external_skills:
                    print(
                        f"    ${skill.qualified_name} v{skill.plugin_version or '-'} "
                        f"{skill.integrity}  {skill.description}"
                    )

    async def _execute_skill(self, skill: dict, args: str):
        """Execute a skill by expanding its prompt template and sending to AI."""
        parts = args.strip().upper().split() if args.strip() else []
        cmd = skill["command"]

        # Skill invocation header — matches the ⏺ tool-call rhythm
        _skill_name = skill.get("name") or cmd.lstrip("/")
        _arg_hint = f"  [dim]{args.strip()}[/dim]" if args.strip() else ""
        if self.context.has_rich:
            self.context.console.print(f"\n  [#C08050]⏺[/#C08050]  [bold]技能 · {_skill_name}[/bold]{_arg_hint}")
        else:
            print(f"\n  ⏺ 技能 · {_skill_name}  {args.strip()}")

        # Build the prompt from template
        template = skill["prompt"]

        if cmd == "/deep-analysis":
            symbol = parts[0] if parts else "AAPL"
            prompt = template.format(symbol=symbol)

        elif cmd == "/trade-idea":
            context = f" in {' '.join(parts)}" if parts else " in the US market"
            prompt = template.format(context=context)

        elif cmd == "/risk-report":
            if parts:
                symbols = ", ".join(parts)
            else:
                symbols = ", ".join(self.terminal.config.get("watchlist", ["AAPL", "MSFT", "GOOGL"]))
            prompt = template.format(symbols=symbols)

        elif cmd == "/factor-screen":
            factor = " ".join(parts).lower() if parts else "momentum"
            prompt = template.format(factor=factor)

        elif cmd == "/backtest-report":
            strategy = parts[0].lower() if len(parts) > 0 else "momentum"
            symbol = parts[1] if len(parts) > 1 else "SPY"
            start = parts[2] if len(parts) > 2 else "2023-01-01"
            end = parts[3] if len(parts) > 3 else "2025-01-01"
            prompt = template.format(strategy=strategy, symbol=symbol, start=start, end=end)

        elif cmd == "/morning-brief":
            extra = f"\nFocus on: {' '.join(parts)}" if parts else ""
            prompt = template.format(extra=extra)

        elif cmd == "/macro-outlook":
            context = f" for {' '.join(parts)}" if parts else " for the US and global economy"
            prompt = template.format(context=context)

        elif cmd == "/crypto-scan":
            extra = f"\nFocus on: {' '.join(parts)}" if parts else ""
            prompt = template.format(extra=extra)

        elif cmd == "/watchlist-scan":
            symbols = ", ".join(self.terminal.config.get("watchlist", ["AAPL", "MSFT", "GOOGL"]))
            prompt = template.format(symbols=symbols)

        elif cmd == "/sector-rotation":
            prompt = template

        elif cmd == "/gen-strategy":
            strategy = parts[0].lower() if len(parts) > 0 else "momentum"
            symbol = parts[1] if len(parts) > 1 else "SPY"
            prompt = template.format(strategy=strategy, symbol=symbol)

        elif cmd == "/gen-analysis":
            topic = " ".join(parts[:2]).lower() if parts else "technical analysis"
            symbols = ", ".join(parts[2:]) if len(parts) > 2 else "SPY"
            prompt = template.format(topic=topic, symbols=symbols)

        elif cmd == "/gen-bot":
            exchange = parts[0].lower() if len(parts) > 0 else "binance"
            strategy = " ".join(parts[1:]).lower() if len(parts) > 1 else "grid trading"
            prompt = template.format(exchange=exchange, strategy=strategy)

        else:
            prompt = template

        # Show skill activation
        if self.context.has_rich:
            tools = ", ".join(skill.get("tools_hint", [])[:3])
            self.context.console.print(f"[bold]Skill:[/bold] [bold]{skill['name']}[/bold]  [dim]tools: {tools}[/dim]")
        else:
            print(f"Skill: {skill['name']}")

        await self.terminal.send_message(prompt)

    def cmd_tools(self, args: str):
        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("  [bold]Local Tools[/bold] [dim](Code Agent)[/dim]")
            for i, (name, (_, desc)) in enumerate(_get_LOCAL_TOOLS().items(), 1):
                self.context.console.print(f"    [bold]{name:28s}[/bold][dim]{desc}[/dim]")
            self.context.console.print()

            self.context.console.print(f"  [bold]Remote Tools[/bold] [dim]({len(_get_ARIA_TOOLS())})[/dim]")
            for i, (name, desc) in enumerate(_get_ARIA_TOOLS(), 1):
                self.context.console.print(f"    [bold]{name:28s}[/bold][dim]{desc}[/dim]")
            self.context.console.print()
        else:
            print("\nLocal Tools (Code Agent):")
            for i, (name, (_, desc)) in enumerate(_get_LOCAL_TOOLS().items(), 1):
                print(f"  {i:2d}. {name:30s} {desc}")
            print("\nRemote Aria Tools (22):")
            for i, (name, desc) in enumerate(_get_ARIA_TOOLS(), 1):
                print(f"  {i:2d}. {name:30s} {desc}")

    async def cmd_collab(self, args: str):
        """Consult ChatGPT/OpenAI and Claude/Anthropic through their APIs.

        Consumer-app subscriptions deliberately are not treated as credentials.
        This keeps external model use explicit, attributable, and opt-in.
        """
        from aria_code.apps.cli.providers.collaboration import (
            collaboration_readiness, consult, resolve_collaborator,
        )

        parts = args.strip().split(maxsplit=1)
        action = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""
        config = self.terminal.config

        if action in ("status", "help"):
            rows = collaboration_readiness(config, _get_provider_key)
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print("  [bold]多模型协作（API）[/bold]")
                self.context.console.print("  [dim]ChatGPT/Claude 桌面订阅不会被读取；请配置各自 API key。[/dim]")
                self.context.console.print()
                for row in rows:
                    icon = "[green]●[/green]" if row["configured"] else "[dim]○[/dim]"
                    state = "[green]已就绪[/green]" if row["configured"] else "[dim]未配置[/dim]"
                    self.context.console.print(
                        f"  {icon} [bold]{row['label']:18s}[/bold]  {state}"
                        f"  [dim]{row['model']}[/dim]"
                    )
                self.context.console.print()
                self.context.console.print("  [dim]/apikey set openai <key>       配置 ChatGPT/OpenAI API[/dim]")
                self.context.console.print("  [dim]/apikey set anthropic <key>    配置 Claude API[/dim]")
                self.context.console.print("  [dim]/collab use chatgpt [model]    将主对话切换为 OpenAI[/dim]")
                self.context.console.print("  [dim]/collab use claude [model]     将主对话切换为 Claude[/dim]")
                self.context.console.print("  [dim]/collab model <名称> <模型>     设置协作咨询使用的模型[/dim]")
                self.context.console.print("  [dim]/collab ask <问题>              并行获取两份独立意见（会消耗 API 配额）[/dim]")
                self.context.console.print()
            else:
                print("Multi-model collaboration (API; desktop subscriptions are not API keys):")
                for row in rows:
                    state = "ready" if row["configured"] else "not configured"
                    print(f"  {'✓' if row['configured'] else '○'} {row['label']}: {state} ({row['model']})")
                print("  /collab use chatgpt [model] | /collab use claude [model]")
                print("  /collab model <name> <model>")
                print("  /collab ask <prompt>")
            return

        if action == "model":
            tokens = rest.split(maxsplit=1)
            target = resolve_collaborator(tokens[0] if tokens else "", config)
            model = tokens[1].strip() if len(tokens) > 1 else ""
            if target is None or not model:
                msg = "Usage: /collab model chatgpt <model> | /collab model claude <model>"
                self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
                return
            config[f"collab_{target.alias}_model"] = model
            self.context.save_config(config)
            msg = f"✓ {target.label} 协作模型已设为 {model}"
            self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
            return

        if action == "use":
            tokens = rest.split(maxsplit=1)
            name = tokens[0] if tokens else ""
            target = resolve_collaborator(name, config)
            if target is None:
                msg = "Usage: /collab use chatgpt [model] | /collab use claude [model]"
                self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
                return
            if not _get_provider_key(target.provider):
                msg = f"⚠ {target.label} API key 未配置：/apikey set {target.provider} <key>"
                self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
                return
            model = tokens[1].strip() if len(tokens) > 1 else target.default_model
            await self.cmd_model(f"{target.provider}/{model}")
            return

        if action == "ask":
            if not rest:
                msg = "Usage: /collab ask <问题>"
                self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
                return
            targets = [
                target for name in ("chatgpt", "claude")
                if (target := resolve_collaborator(name, config)) is not None
                and _get_provider_key(target.provider)
            ]
            if not targets:
                msg = "没有已配置的协作模型。先运行 /collab status。"
                self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
                return
            notice = f"正在向 {len(targets)} 个模型征询独立意见…"
            self.context.console.print(f"[dim]{notice}[/dim]") if self.context.has_rich else print(notice)
            from aria_code.providers.llm.base import Message
            from aria_code.providers.llm.registry import get_provider

            results = await consult(
                rest, targets, get_provider=get_provider, message_factory=Message,
                timeout=float(config.get("collab_timeout", 60.0)),
            )
            for result in results:
                label = str(result["label"])
                model = str(result["model"])
                if result["success"]:
                    if self.context.has_rich:
                        from rich.panel import Panel
                        self.context.console.print(Panel(
                            str(result["response"]), title=f"{label} · {model}",
                            border_style="cyan", padding=(0, 1),
                        ))
                    else:
                        print(f"\n[{label} · {model}]\n{result['response']}")
                else:
                    msg = f"{label} 未完成：{result['error'] or 'empty response'}"
                    self.context.console.print(f"[yellow]⚠ {msg}[/yellow]") if self.context.has_rich else print(f"⚠ {msg}")
            return

        msg = "Usage: /collab [status|model|use|ask]"
        self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)

    async def cmd_apikey(self, args: str):
        """Manage Cloud API keys.

        Usage:
            /apikey              — 交互式向导：选择 provider → 输入 key → 测试连接
            /apikey set <p> <k>  — 直接保存
            /apikey list         — 列出所有已配置 key
            /apikey remove <p>   — 删除 key
            /apikey test <p>     — 测试连接
        """
        parts = args.strip().split()
        sub   = parts[0].lower() if parts else ""

        # ── 无参数 or "add" → 交互式向导 ─────────────────────────────────────
        if not sub or sub in ("add", "wizard"):
            await self._cmd_apikey_wizard()
            return

        pjson = _load_providers_json()   # dict of {provider: {api_key, base_url, ...}}

        if sub == "set-url":
            # /apikey set-url <provider> <base_url>
            # 允许自定义端点（中转代理、国内镜像等），示例：
            #   /apikey set-url openai https://my-proxy.com
            #   /apikey set-url siliconflow https://api.siliconflow.cn
            if len(parts) < 3:
                msg = "Usage: /apikey set-url <provider> <base_url>"
                self.context.console.print(f"[dim]{msg}[/dim]") if self.context.has_rich else print(msg)
                return
            provider = parts[1].lower()
            url      = parts[2].rstrip("/")
            entry    = pjson.get(provider, {})
            entry["base_url"] = url
            pjson[provider]   = entry
            _save_providers_json(pjson)
            msg = f"✓ {provider.capitalize()} base_url 已更新: {url}"
            self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
            return

        if sub == "set":
            if len(parts) < 3:
                msg = ("Usage: /apikey set <provider> <key>  (e.g. /apikey set deepseek sk-...)\n"
                       "       /apikey set-url <provider> <base_url>  (自定义代理端点)")
                self.context.console.print(f"[dim]{msg}[/dim]") if self.context.has_rich else print(msg)
                return
            provider = parts[1].lower()
            key      = parts[2]
            _all_known = set(_get__PROVIDER_KEY_MAP()) | set(_get__DATA_KEY_MAP()) | set(_get__PROVIDER_BASE_URLS())
            if provider not in _all_known:
                known_llm  = ", ".join(sorted(_get__PROVIDER_KEY_MAP().keys()))
                known_data = ", ".join(sorted(_get__DATA_KEY_MAP().keys()))
                msg = (f"Unknown provider '{provider}'.\n"
                       f"  LLM providers: {known_llm}\n"
                       f"  Data services: {known_data}")
                self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
                return

            # ── Data service key ──────────────────────────────────────────────
            if provider in _get__DATA_KEY_MAP():
                _save_data_key(provider, key)
                env_var = _get__DATA_KEY_MAP()[provider]
                os.environ[env_var] = key  # take effect immediately
                masked = key[:6] + "****" + key[-4:] if len(key) > 10 else "****"
                signup = _get__DATA_SIGNUP_URLS().get(provider, "")
                msg = f"✓ {provider.capitalize()} 数据服务 key 已保存 ({masked})"
                self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
                return

            # ── LLM provider key (original logic) ────────────────────────────
            # Persist to providers.json
            entry = pjson.get(provider, {})
            entry["api_key"] = key
            if provider in _get__PROVIDER_BASE_URLS():
                entry.setdefault("base_url", _get__PROVIDER_BASE_URLS()[provider])
            pjson[provider] = entry
            _save_providers_json(pjson)
            # Also set in current process env so it works immediately
            env_var = _get__PROVIDER_KEY_MAP().get(provider)
            if env_var:
                os.environ[env_var] = key
            masked = key[:6] + "****" + key[-4:] if len(key) > 10 else "****"
            msg = f"✓ {provider.capitalize()} API key 已保存 ({masked})"
            self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)

        elif sub == "list":
            _LLM_ORDER = [
                # 国际
                "deepseek", "anthropic", "openai", "google", "xai",
                "groq", "mistral", "cohere", "perplexity", "together",
                # 国内
                "siliconflow", "dashscope", "moonshot", "zhipu",
                "baidu", "bytedance", "minimax", "stepfun", "01ai",
            ]
            _DATA_ORDER = ["finnhub", "alphavantage", "twelvedata", "polygon",
                           "fmp", "newsapi", "coingecko", "tavily", "brave"]
            data_configured = _load_data_keys()

            if self.context.has_rich:
                from rich.table import Table
                from rich import box as _rbox
                self.context.console.print()
                self.context.console.print("  [bold]🤖 LLM 服务 Keys[/bold]  [dim]— /apikey 进入向导[/dim]")
                self.context.console.print()
                for prov in _LLM_ORDER:
                    env_var = _get__PROVIDER_KEY_MAP().get(prov, "")
                    key_val = os.getenv(env_var or "") or pjson.get(prov, {}).get("api_key", "")
                    desc = _get__PROVIDER_DESC().get(prov, "")
                    if key_val:
                        masked = key_val[:6] + "****" + key_val[-4:] if len(key_val) > 10 else "****"
                        self.context.console.print(f"  [green]●[/green] [green]{prov:<14}[/green] {masked}  [dim]{desc}[/dim]")
                    else:
                        self.context.console.print(f"  [dim]○ {prov:<14} 未配置  {desc}[/dim]")
                self.context.console.print()
                self.context.console.print("  [bold]📊 数据服务 Keys[/bold]  [dim]— 后端离线时直连数据源[/dim]")
                self.context.console.print()
                for svc in _DATA_ORDER:
                    key_val = data_configured.get(svc, "")
                    desc = _get__PROVIDER_DESC().get(svc, "")
                    if key_val:
                        masked = key_val[:6] + "****" + key_val[-4:] if len(key_val) > 10 else "****"
                        self.context.console.print(f"  [green]●[/green] [green]{svc:<14}[/green] {masked}  [dim]{desc}[/dim]")
                    else:
                        self.context.console.print(f"  [dim]○ {svc:<14} 未配置  {desc}[/dim]")
                self.context.console.print()
                self.context.console.print("  [dim]提示: /apikey 进入交互向导  ·  /apikey test <provider> 测试连接[/dim]")
                self.context.console.print()
            else:
                print("\n  LLM Providers:")
                for prov in _LLM_ORDER:
                    env_var = _get__PROVIDER_KEY_MAP().get(prov, "")
                    key_val = os.getenv(env_var or "") or pjson.get(prov, {}).get("api_key", "")
                    status = key_val[:6] + "****" if key_val else "未配置"
                    print(f"  {prov:14s} {status}")
                print("\n  Data Services:")
                for svc in _DATA_ORDER:
                    key_val = data_configured.get(svc, "")
                    status  = key_val[:6] + "****" if key_val else "未配置"
                    print(f"  {svc:16s} {status}")

        elif sub == "remove":
            if len(parts) < 2:
                self.context.console.print("[dim]Usage: /apikey remove <provider>[/dim]") if self.context.has_rich else print("Usage: /apikey remove <provider>")
                return
            provider = parts[1].lower()
            # LLM section
            if provider in pjson:
                pjson[provider].pop("api_key", None)
                if not pjson[provider]:
                    del pjson[provider]
                _save_providers_json(pjson)
            # Data section
            if provider in _get__DATA_KEY_MAP():
                try:
                    if _get_PROVIDERS_FILE().exists():
                        raw = json.loads(_get_PROVIDERS_FILE().read_text(encoding="utf-8"))
                        if provider in raw.get("data", {}):
                            del raw["data"][provider]
                            from aria_code.packages.aria_core.secure_file import write_secret_json
                            write_secret_json(_get_PROVIDERS_FILE(), raw)
                except Exception as _e:
                    logger.debug("apikey delete from file failed: %s", _e)
            # Clear from env
            env_var = _get__PROVIDER_KEY_MAP().get(provider) or _get__DATA_KEY_MAP().get(provider)
            if env_var and env_var in os.environ:
                del os.environ[env_var]
            msg = f"✓ {provider.capitalize()} key 已删除"
            self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)

        elif sub == "test":
            if len(parts) < 2:
                self.context.console.print("[dim]Usage: /apikey test <provider>[/dim]") if self.context.has_rich else print("Usage: /apikey test <provider>")
                return
            provider = parts[1].lower()
            key = _get_provider_key(provider) or _load_data_keys().get(provider, "")
            if not key:
                msg = f"⚠ {provider} API key 未配置，先运行 /apikey {provider}"
                self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
                return
            self.context.console.print(f"[dim]  正在测试 {provider}…[/dim]") if self.context.has_rich else print(f"  测试 {provider}…")
            import asyncio as _aio
            loop = _aio.get_event_loop()
            ok, result_msg = await loop.run_in_executor(None, _test_api_key, provider, key)
            color = "green" if ok else "yellow"
            self.context.console.print(f"  [{color}]{result_msg}[/{color}]") if self.context.has_rich else print(f"  {result_msg}")

        else:
            self.context.console.print("[dim]Usage: /apikey [set|list|remove|test] — 或直接 /apikey 进入向导[/dim]") if self.context.has_rich else print("Usage: /apikey [set|list|remove|test]")

    async def _cmd_apikey_wizard(self):
        """交互式 API Key 配置向导：选 provider → 查看指引 → 输入 key → 测试连接。"""
        import getpass as _getpass

        pjson = _load_providers_json()
        data_cfg = _load_data_keys()

        def _is_configured(name: str) -> bool:
            env = _get__PROVIDER_KEY_MAP().get(name) or _get__DATA_KEY_MAP().get(name)
            if env and os.getenv(env):
                return True
            if name in pjson and pjson[name].get("api_key"):
                return True
            if name in data_cfg:
                return True
            return False

        # ── 分组构建 picker ───────────────────────────────────────────────────
        _LLM_ORDER = [
            # 国际
            "deepseek", "anthropic", "openai", "google", "xai",
            "groq", "mistral", "cohere", "perplexity", "together",
            # 国内
            "siliconflow", "dashscope", "moonshot", "zhipu",
            "baidu", "bytedance", "minimax", "stepfun", "01ai",
        ]
        _DATA_ORDER = ["finnhub", "alphavantage", "twelvedata", "polygon",
                       "fmp", "newsapi", "coingecko", "tavily", "brave"]

        all_items = []  # (label, desc, key_name | None)

        all_items.append(("─── 🤖 LLM 服务  (对话·分析·推理) ", "", None))
        for k in _LLM_ORDER:
            dot = "[green]●[/green]" if _is_configured(k) else "[dim]○[/dim]"
            desc = _get__PROVIDER_DESC().get(k, "")
            configured_tag = "  ✓" if _is_configured(k) else ""
            all_items.append((f"  {k:<14}{configured_tag}", desc, k))

        all_items.append(("─── 📊 数据服务  (行情·财报·新闻) ", "", None))
        for k in _DATA_ORDER:
            desc = _get__PROVIDER_DESC().get(k, "")
            configured_tag = "  ✓" if _is_configured(k) else ""
            all_items.append((f"  {k:<14}{configured_tag}", desc, k))

        picker_opts = [(label, desc) for label, desc, _ in all_items]
        sep_indices = {i for i, (_, _, key) in enumerate(all_items) if key is None}
        key_at = {i: key for i, (_, _, key) in enumerate(all_items) if key}

        # 默认选中第一个真实条目
        first_real = next(i for i in range(len(all_items)) if i not in sep_indices)
        selected = first_real

        while True:
            self.context.console.print() if self.context.has_rich else None

            if self.context.has_rich:
                from rich.panel import Panel as _Panel
                from rich import box as _rbox
                self.context.console.print(_Panel(
                    "  ↑↓ 上下选择  ·  Enter 确认  ·  ESC/q 退出向导\n"
                    "  [green]●[/green] 已配置  [dim]○[/dim] 未配置  ✓ 表示 key 已存在",
                    border_style="dim", box=_rbox.ROUNDED, padding=(0, 2),
                ))

            idx = _arrow_select(picker_opts, selected=selected, title="选择要配置的 Provider", max_visible=20)

            if idx < 0:
                self.context.console.print("[dim]已退出向导[/dim]") if self.context.has_rich else print("已退出")
                return

            if idx in sep_indices:
                nxt = next((i for i in range(idx + 1, len(all_items)) if i not in sep_indices), first_real)
                selected = nxt
                continue

            provider = key_at[idx]
            selected = idx

            # ── 显示获取指引 ──────────────────────────────────────────────────
            guide = _get__PROVIDER_GUIDE().get(provider, "")
            signup = _get__LLM_SIGNUP_URLS().get(provider) or _get__DATA_SIGNUP_URLS().get(provider, "")
            if self.context.has_rich:
                from rich.panel import Panel as _Panel
                from rich import box as _rbox
                guide_body = guide
                if signup:
                    guide_body += f"\n\n[bold cyan]🔗 {signup}[/bold cyan]"
                current_key = _get_provider_key(provider)
                if current_key:
                    masked = current_key[:6] + "****" + current_key[-4:] if len(current_key) > 10 else "****"
                    guide_body += f"\n\n[green]当前 key: {masked}[/green]  (直接回车保留现有 key)"
                self.context.console.print()
                self.context.console.print(_Panel(
                    guide_body,
                    title=f"[bold]{provider.upper()}  配置指引[/bold]",
                    border_style="cyan", box=_rbox.ROUNDED, padding=(0, 2),
                ))
            else:
                print(f"\n=== {provider.upper()} ===")
                print(guide)
                if signup:
                    print(f"注册地址: {signup}")

            # ── 输入 key ──────────────────────────────────────────────────────
            prompt_str = f"  请输入 {provider} API Key (输入后不显示): "
            try:
                raw_key = _getpass.getpass(prompt_str)
            except (KeyboardInterrupt, EOFError):
                self.context.console.print("\n[dim]已跳过[/dim]") if self.context.has_rich else print("\n已跳过")
                continue

            raw_key = raw_key.strip()
            if not raw_key:
                # 保留现有 key，直接回到 picker
                msg = "未输入 key，保留现有配置"
                self.context.console.print(f"[dim]  {msg}[/dim]") if self.context.has_rich else print(f"  {msg}")
                continue

            # ── 保存 ──────────────────────────────────────────────────────────
            if provider in _get__DATA_KEY_MAP():
                _save_data_key(provider, raw_key)
                env_var = _get__DATA_KEY_MAP()[provider]
                os.environ[env_var] = raw_key
            else:
                pjson_fresh = _load_providers_json()
                entry = pjson_fresh.get(provider, {})
                entry["api_key"] = raw_key
                if provider in _get__PROVIDER_BASE_URLS():
                    entry.setdefault("base_url", _get__PROVIDER_BASE_URLS()[provider])
                pjson_fresh[provider] = entry
                _save_providers_json(pjson_fresh)
                env_var = _get__PROVIDER_KEY_MAP().get(provider)
                if env_var:
                    os.environ[env_var] = raw_key
                pjson = pjson_fresh

            masked = raw_key[:6] + "****" + raw_key[-4:] if len(raw_key) > 10 else "****"
            msg = f"✓ {provider} key 已保存  ({masked})"
            self.context.console.print(f"[green]  {msg}[/green]") if self.context.has_rich else print(f"  {msg}")

            # ── 连接测试 ──────────────────────────────────────────────────────
            print("  正在测试连接…", end="", flush=True)
            import asyncio as _aio
            loop = _aio.get_event_loop()
            ok, result_msg = await loop.run_in_executor(None, _test_api_key, provider, raw_key)
            print("\r", end="")  # 清除"正在测试"那行
            if self.context.has_rich:
                color = "green" if ok else "yellow"
                self.context.console.print(f"  [{color}]{result_msg}[/{color}]")
            else:
                print(f"  {result_msg}")

            # ── 继续配置其他 provider？ ───────────────────────────────────────
            self.context.console.print() if self.context.has_rich else None
            try:
                again = input("  继续配置其他 provider? (y/N) › ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                again = "n"
            if again not in ("y", "yes", "是"):
                self.context.console.print("[dim]  向导已完成。输入 /apikey list 查看所有配置。[/dim]") if self.context.has_rich else print("向导完成")
                return

    def _providers_init(self, *, force: bool = False):
        """按本机实际环境生成 ~/.aria/providers.yaml。"""
        from pathlib import Path as _P

        try:
            from aria_code.providers.llm.autoconfig import probe_environment, render_providers_yaml
        except Exception as exc:
            self.context.console.print(f"[red]无法加载配置生成器: {exc}[/red]" if self.context.has_rich else f"无法加载配置生成器: {exc}")
            return

        self.context.console.print("[dim]正在探测本机可用的模型来源…[/dim]" if self.context.has_rich else "正在探测…")
        findings = probe_environment()

        for f in findings:
            mark = "[green]✓[/green]" if f.available else "[dim]·[/dim]"
            line = f"  {mark} {f.provider:<14} {f.detail}"
            self.context.console.print(line if self.context.has_rich else line.replace("[green]✓[/green]", "✓").replace("[dim]·[/dim]", "·"))

        target = _P.home() / ".aria" / "providers.yaml"
        content = render_providers_yaml(findings)

        # 已有配置绝不静默覆盖——那可能是用户手写并调试了很久的东西。
        if target.exists() and not force:
            preview = target.with_suffix(".yaml.generated")
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_text(content, encoding="utf-8")
            msg = (f"\n已有配置 {target} 未改动。\n"
                   f"生成结果写到 {preview}，比对满意后自行替换，\n"
                   f"或用 /providers init --force 直接覆盖。")
            self.context.console.print(f"[yellow]{msg}[/yellow]" if self.context.has_rich else msg)
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.context.console.print(f"\n[green]✓[/green] 已写入 {target}" if self.context.has_rich else f"\n✓ 已写入 {target}")

        if not any(f.available for f in findings):
            tip = "这台机器上还没有可用来源。最快：brew install ollama && ollama serve && ollama pull qwen2.5:7b"
            self.context.console.print(f"[yellow]{tip}[/yellow]" if self.context.has_rich else tip)


    def cmd_providers(self, args: str):
        """Show all LLM providers: local backends + cloud API status (Open Interpreter style).

        `/providers init` 额外生成一份按本机实际环境定制的 providers.yaml——
        探测到什么就写什么，缺什么就把"去哪拿"写成注释放进同一个文件，
        用户不必在文档和配置之间来回对照。
        """
        if args.strip().lower().startswith("init"):
            return self._providers_init(force="--force" in args)

        if self.context.has_rich:
            self.context.console.print()

        # ── Section 1: Local backends ────────────────────────────────────────
        try:
            from local_llm_provider import probe_local_backends, BACKEND_DEFAULTS
            results = probe_local_backends()
            current_provider = self.terminal.config.get("local_provider", "ollama")
            # Count Ollama models if online
            _ollama_count = ""
            if results.get("ollama"):
                try:
                    _omodels, _ = detect_ollama_models_rich(
                        self.terminal.config.get("ollama_url", "http://localhost:11434"))
                    _ollama_count = f"  [dim]{len(_omodels)} 个模型[/dim]" if _omodels else ""
                except Exception:
                    pass

            if self.context.has_rich:
                self.context.console.print("  [bold]本地 Backend[/bold]")
                self.context.console.print()
            else:
                print("  == Local Backends ==")

            for name, available in results.items():
                info   = BACKEND_DEFAULTS.get(name, {})
                url    = info.get("default_url", "")
                color  = "green" if available else "dim"
                icon   = "✅" if available else "○"
                active = " ◀ active" if name == current_provider else ""
                extra  = _ollama_count if (name == "ollama" and available) else ""
                if self.context.has_rich:
                    self.context.console.print(
                        f"  {icon} [{color}]{name:12s}[/{color}]"
                        f" [dim]{url:30s}[/dim]{extra}"
                        f"[green]{active}[/green]"
                    )
                else:
                    status = "✓" if available else "✗"
                    print(f"  {status} {name:12s} {url}{active}")
        except ImportError:
            pass

        # ── Section 2: Cloud provider API keys ───────────────────────────────
        pjson = _load_providers_json()
        _CLOUD_LIST = [
            # ── 国际云端 ────────────────────────────────────────────────
            ("deepseek",    "DeepSeek",      "deepseek/deepseek-chat"),
            ("anthropic",   "Anthropic",     "anthropic/claude-sonnet-4-6"),
            ("openai",      "OpenAI",        "openai/gpt-4.5"),
            ("google",      "Google Gemini", "google/gemini-3.5-flash"),
            ("xai",         "xAI Grok",      "xai/grok-3-fast"),
            ("groq",        "Groq",          "groq/llama-3.3-70b-versatile"),
            ("mistral",     "Mistral",       "mistral/mistral-large-latest"),
            ("cohere",      "Cohere",        "cohere/command-r-plus"),
            ("perplexity",  "Perplexity",    "perplexity/sonar-pro"),
            ("together",    "Together",      "together/meta-llama/Meta-Llama-3.1-70B"),
            # ── 国内云端 ────────────────────────────────────────────────
            ("siliconflow", "SiliconFlow",   "siliconflow/Qwen/Qwen2.5-7B-Instruct"),
            ("dashscope",   "DashScope",     "dashscope/qwen-max"),
            ("moonshot",    "Moonshot Kimi", "moonshot/moonshot-v1-128k"),
            ("zhipu",       "Zhipu GLM",     "zhipu/glm-4-plus"),
            ("baidu",       "Baidu ERNIE",   "baidu/ernie-4.5-turbo-128k"),
            ("bytedance",   "ByteDance",     "bytedance/<endpoint-id>"),
            ("minimax",     "MiniMax",       "minimax/MiniMax-Text-01"),
            ("stepfun",     "StepFun",       "stepfun/step-2-16k"),
            ("01ai",        "01.AI Yi",      "01ai/yi-large"),
        ]
        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("  [bold]Cloud Provider API[/bold]")
            self.context.console.print()
        else:
            print()
            print("  == Cloud Providers ==")

        for prov, label, example_model in _CLOUD_LIST:
            env_var = _get__PROVIDER_KEY_MAP().get(prov, "")
            key = (os.getenv(env_var, "") if env_var else "") or \
                  (pjson.get(prov, {}).get("api_key", "") if isinstance(pjson, dict) else "")
            if key:
                masked = key[:6] + "****" + key[-4:] if len(key) > 10 else "****"
                if self.context.has_rich:
                    self.context.console.print(f"  🔑 [green]{label:14s}[/green] [dim]{masked}[/dim]")
                else:
                    print(f"  ✓ {label:14s} {masked}")
            else:
                hint = f"/apikey set {prov} <key>"
                if self.context.has_rich:
                    self.context.console.print(f"  ○ [dim]{label:14s} 未配置  →  {hint}[/dim]")
                else:
                    print(f"  ✗ {label:14s} {hint}")

        # ── Custom endpoint ──────────────────────────────────────────────────
        custom_ep = self.terminal.config.get("custom_endpoint", "")
        custom_m  = self.terminal.config.get("custom_model", "")
        if custom_ep:
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print(f"  🔧 [bold]Custom endpoint[/bold]  [dim]{custom_ep}[/dim]  model=[cyan]{custom_m or '?'}[/cyan]")
            else:
                print(f"\n  Custom: {custom_ep}  model={custom_m}")

        # ── Data service keys section ─────────────────────────────────────────
        _data_keys = _load_data_keys()
        _DATA_DISPLAY = [
            ("finnhub",      "Finnhub",       "股票+新闻"),
            ("newsapi",      "NewsAPI",        "财经新闻"),
            ("brave",        "Brave Search",   "网页搜索"),
            ("alphavantage", "Alpha Vantage",  "历史数据"),
            ("coingecko",    "CoinGecko Pro",  "加密数据"),
            ("twelvedata",   "Twelve Data",    "全球行情"),
        ]
        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("  [bold]📊 数据服务 API[/bold]  [dim](后端离线时的本地数据源)[/dim]")
            self.context.console.print()
        else:
            print("\n  == Data Service APIs ==")
        for svc, label, desc in _DATA_DISPLAY:
            key_val = _data_keys.get(svc, "")
            if key_val:
                masked = key_val[:6] + "****" + key_val[-4:] if len(key_val) > 10 else "****"
                signup = _get__DATA_SIGNUP_URLS().get(svc, "")
                if self.context.has_rich:
                    self.context.console.print(f"  🔑 [green]{label:18s}[/green] [dim]{masked}  {desc}[/dim]")
                else:
                    print(f"  ✓ {label:18s} {masked}")
            else:
                hint   = f"/apikey set {svc} <key>"
                signup = _get__DATA_SIGNUP_URLS().get(svc, "")
                if self.context.has_rich:
                    self.context.console.print(f"  ○ [dim]{label:18s} 未配置  →  {hint}[/dim]")
                else:
                    print(f"  ✗ {label:18s} {hint}")

        # ── Free data source registry (akshare / yfinance / tushare) ────────────
        try:
            from aria_code.datasources.router import DataRouter as _DR
            free_sources = _DR().list_sources()
        except Exception:
            free_sources = []

        if free_sources:
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print("  [bold]免费行情数据源[/bold]  [dim](datasources/router — no API key required)[/dim]")
                self.context.console.print()
            else:
                print("\n  == Free Market Data Sources ==")
            for s in free_sources:
                ok_icon = "[green]✓[/green]" if s["configured"] else "[dim]○[/dim]"
                key_tag = " [dim](no key)[/dim]" if not s["needs_key"] else " [dim](API key)[/dim]"
                mkts    = ", ".join(s.get("markets", []))
                if self.context.has_rich:
                    self.context.console.print(
                        f"  {ok_icon} [bold]{s['name']:12s}[/bold]  "
                        f"[dim]{mkts:22s}[/dim]{key_tag}"
                    )
                else:
                    ok   = "✓" if s["configured"] else "○"
                    key  = "(no key)" if not s["needs_key"] else "(key)"
                    print(f"  {ok} {s['name']:12s}  {mkts:22s}  {key}")
            if self.context.has_rich:
                self.context.console.print("  [dim]Config: ~/.aria/datasources.yaml[/dim]")

        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("  [dim]配置 LLM Key:   /apikey set deepseek <key>[/dim]")
            self.context.console.print("  [dim]配置数据 Key:   /apikey set finnhub <key>[/dim]")
            self.context.console.print("  [dim]切换模型:       /model deepseek/deepseek-chat[/dim]")
            self.context.console.print("  [dim]首次向导:       /setup[/dim]")
            self.context.console.print("  [dim]自定义端点:     /config set custom_endpoint=http://...[/dim]")
            self.context.console.print()

    async def cmd_cloud(self, args: str):
        """
        Manage Alibaba Cloud data service connection.

        Usage:
          /cloud status              — show connection status & circuit breaker state
          /cloud set <url>           — set cloud_api_server URL (e.g. http://your-aliyun-ip:8000)
          /cloud data <url>          — set akshare_data_server URL (e.g. http://your-aliyun-ip:8002)
          /cloud token <jwt-token>   — set API token
          /cloud health              — live health-check both services
          /cloud reset               — reset circuit breakers
        """
        try:
            from aliyun_data_client import AliyunDataClient, save_cloud_config, summarize_cloud_health
        except ImportError:
            if self.context.has_rich:
                self.context.console.print("  [red]aliyun_data_client.py not found[/red]")
            else:
                print("  aliyun_data_client.py not found")
            return

        parts = args.strip().split(None, 2)
        sub   = parts[0].lower() if parts else "status"

        if sub == "set" and len(parts) >= 2:
            url = parts[1]
            save_cloud_config(cloud_url=url)
            AliyunDataClient.reset()
            if self.context.has_rich:
                self.context.console.print(f"  [green]Cloud API URL set to: {url}[/green]")
                self.context.console.print("  [dim]Saved to ~/.arthera/config.json[/dim]")
            return

        if sub == "data" and len(parts) >= 2:
            url = parts[1]
            save_cloud_config(data_url=url)
            AliyunDataClient.reset()
            if self.context.has_rich:
                self.context.console.print(f"  [green]AKShare Data URL set to: {url}[/green]")
                self.context.console.print("  [dim]Saved to ~/.arthera/config.json[/dim]")
            return

        if sub == "token" and len(parts) >= 2:
            token = parts[1]
            save_cloud_config(api_token=token)
            AliyunDataClient.reset()
            if self.context.has_rich:
                self.context.console.print(f"  [green]API token saved (length {len(token)})[/green]")
            return

        if sub == "reset":
            AliyunDataClient.reset()
            if self.context.has_rich:
                self.context.console.print("  [green]Circuit breakers reset, config reloaded[/green]")
            return

        client = AliyunDataClient.get()

        if sub == "health":
            if self.context.has_rich:
                self.context.console.print("  [dim]Checking health…[/dim]")
            with self.context.console.status("[dim]Checking cloud services…[/dim]", spinner="dots") if self.context.has_rich else _null_ctx():
                cloud_h = await client.health_cloud()
                data_h  = await client.health_data()
                st = client.status()
                summary = summarize_cloud_health(cloud_h, data_h, st)

            def _svc_label(name: str, health: dict) -> str:
                status = str(health.get("status", "?"))
                ok = status in ("healthy", "ok", "ready", "online")
                color = "green" if ok else "red"
                icon = "✓" if ok else "✗"
                breaker = st.get("cloud_cb" if name == "cloud_api_server" else "data_cb", "?")
                return f"  [{color}]●[/{color}] {name}  {icon} {status}  [dim]breaker={breaker}[/dim]"

            def _print_health_detail(title: str, health: dict):
                if self.context.has_rich:
                    self.context.console.print()
                    self.context.console.print(_svc_label(title, health))
                    detail_keys = [
                        (k, v) for k, v in health.items()
                        if k not in {"status", "services", "cloud_url", "data_url"}
                    ]
                    if detail_keys:
                        for k, v in detail_keys:
                            self.context.console.print(f"    [dim]{k}: {v}[/dim]")
                    services = health.get("services") or {}
                    if services:
                        for svc, svc_status in services.items():
                            svc_ok = "online" in str(svc_status) or "ready" in str(svc_status)
                            svc_icon = "✓" if svc_ok else "○"
                            svc_color = "green" if svc_ok else "yellow"
                            self.context.console.print(f"    [dim]{svc_icon} {svc}: [{svc_color}]{svc_status}[/{svc_color}][/dim]")
                else:
                    print(f"  {title}: {health.get('status', '?')}")
                    for k, v in health.items():
                        if k not in {"status", "services", "cloud_url", "data_url"}:
                            print(f"    {k}: {v}")
                    for svc, svc_status in (health.get("services") or {}).items():
                        print(f"    {svc}: {svc_status}")

            if self.context.has_rich:
                self.context.console.print()
                color = "green" if summary.status == "ok" else "yellow" if summary.status == "warn" else "red"
                self.context.console.print(f"  [bold]Summary[/bold]  [{color}]{summary.detail}[/{color}]")
                self.context.console.print(f"  [dim]breaker_open={summary.breaker_open}  token_set={summary.token_set}[/dim]")
                self.context.console.print(f"  [dim]suggestion: {summary.suggestion}[/dim]")
                self.context.console.print(f"  [dim]cloud_api_server: {client.cloud_url}[/dim]")
                self.context.console.print(f"  [dim]akshare_data_server: {client.data_url}[/dim]")
                _print_health_detail("cloud_api_server", cloud_h)
                _print_health_detail("akshare_data_server", data_h)
                self.context.console.print()
            else:
                print(f"  Summary: {summary.detail} ({summary.status})")
                print(f"  breaker_open={summary.breaker_open} token_set={summary.token_set}")
                print(f"  suggestion: {summary.suggestion}")
                _print_health_detail("cloud_api_server", cloud_h)
                _print_health_detail("akshare_data_server", data_h)
            return

        # Default: /cloud status
        st = client.status()
        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("  [bold]Alibaba Cloud Data Services[/bold]")
            self.context.console.print()
            health_summary = st.get("health_summary") or {}
            color = "green" if health_summary.get("status") == "ok" else "yellow" if health_summary.get("status") == "warn" else "red"
            if health_summary:
                self.context.console.print(f"  [bold]Health[/bold]  [{color}]{health_summary.get('detail', '')}[/{color}]")
                self.context.console.print(f"  [dim]breaker_open={health_summary.get('breaker_open', 0)}  token_set={health_summary.get('token_set', False)}[/dim]")
            _c = "green" if st["cloud_cb"] == "closed" else "red"
            _d = "green" if st["data_cb"]  == "closed" else "red"
            self.context.console.print(f"  [{_c}]●[/{_c}] cloud_api_server   [dim]{st['cloud_url']}[/dim]"
                          f"  [{_c}]{st['cloud_cb']}[/{_c}]")
            self.context.console.print(f"  [{_d}]●[/{_d}] akshare_data_server [dim]{st['data_url']}[/dim]"
                          f"  [{_d}]{st['data_cb']}[/{_d}]")
            tok_str = "[green]set[/green]" if st["has_token"] else "[dim]not set[/dim]"
            self.context.console.print(f"  Auth token: {tok_str}")
            self.context.console.print()
            self.context.console.print("  [dim]Configure: /cloud set <url>  /cloud data <url>  /cloud token <jwt>[/dim]")
            self.context.console.print("  [dim]Health:    /cloud health[/dim]")
            self.context.console.print()
        else:
            health_summary = st.get("health_summary") or {}
            if health_summary:
                print(f"  Health: {health_summary.get('detail', '')} ({health_summary.get('status', '')})")
            print(f"  Cloud: {st['cloud_url']} ({st['cloud_cb']})")
            print(f"  Data:  {st['data_url']} ({st['data_cb']})")
            print(f"  Token: {'set' if st['has_token'] else 'not set'}")

    def cmd_permissions(self, args: str):
        """Tool permission policy manager.

        /permissions              — show current policy table
        /permissions allow <tool> — auto-approve this tool forever
        /permissions deny  <tool> — permanently block this tool
        /permissions ask   <tool> — always prompt before this tool
        /permissions reset        — clear all custom rules
        /permissions remove <tool>— remove this tool from policy
        """
        try:
            from aria_code.runtime.tool_policy import (
                load_tool_policy, save_tool_policy,
                add_to_policy, remove_from_policy,
            )
        except ImportError as _e:
            self.context.console.print(f"[red]runtime.tool_policy not available: {_e}[/red]") if self.context.has_rich else print(f"Error: {_e}")
            return

        parts = args.strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else "show"

        if sub in ("allow", "deny", "ask"):
            if len(parts) < 2:
                msg = f"Usage: /permissions {sub} <tool_name>"
                self.context.console.print(f"[dim]{msg}[/dim]") if self.context.has_rich else print(msg)
                return
            tool = parts[1].strip()
            add_to_policy(tool, sub)
            labels = {"allow": ("[green]✓ 白名单[/green]", "auto-approve"), "deny": ("[red]✗ 黑名单[/red]", "block"), "ask": ("[yellow]? 询问[/yellow]", "ask-always")}
            rich_label, plain_label = labels[sub]
            if self.context.has_rich:
                self.context.console.print(f"  {rich_label}  [bold]{tool}[/bold]  [dim]已更新[/dim]")
            else:
                print(f"  {plain_label}: {tool}")
            return

        if sub == "reset":
            save_tool_policy({"allowed": [], "denied": [], "ask_always": []})
            msg = "✓ 工具权限策略已重置"
            self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
            return

        if sub in ("remove", "rm"):
            if len(parts) < 2:
                msg = "Usage: /permissions remove <tool_name>"
                self.context.console.print(f"[dim]{msg}[/dim]") if self.context.has_rich else print(msg)
                return
            tool = parts[1].strip()
            if remove_from_policy(tool):
                msg = f"✓ '{tool}' 已从策略中移除"
                self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
            else:
                msg = f"'{tool}' 不在任何策略列表中"
                self.context.console.print(f"[dim]{msg}[/dim]") if self.context.has_rich else print(msg)
            return

        # Default: show policy table
        policy = load_tool_policy()
        allowed   = policy.get("allowed", [])
        denied    = policy.get("denied", [])
        ask_always = policy.get("ask_always", [])

        if self.context.has_rich:
            from rich.table import Table
            from rich import box as _rbox
            self.context.console.print()
            self.context.console.print("  [bold]Tool Permissions[/bold]  [dim](~/.arthera/tool_policy.json)[/dim]")
            self.context.console.print()
            t = Table(box=_rbox.SIMPLE, padding=(0, 1), show_header=True)
            t.add_column("Tool", style="bold", min_width=22)
            t.add_column("Policy", min_width=12)
            t.add_column("Effect", style="dim")
            for tool in sorted(allowed):
                t.add_row(tool, "[green]✓ allow[/green]", "auto-approve, never prompt")
            for tool in sorted(denied):
                t.add_row(tool, "[red]✗ deny[/red]", "always reject, never run")
            for tool in sorted(ask_always):
                t.add_row(tool, "[yellow]? ask[/yellow]", "always prompt even in auto mode")
            if not (allowed or denied or ask_always):
                t.add_row("[dim]— no custom rules —[/dim]", "", "")
            self.context.console.print(t)
            self.context.console.print("  [dim]/permissions allow <tool>   — 白名单（自动批准）[/dim]")
            self.context.console.print("  [dim]/permissions deny  <tool>   — 黑名单（始终拒绝）[/dim]")
            self.context.console.print("  [dim]/permissions ask   <tool>   — 始终询问[/dim]")
            self.context.console.print("  [dim]/permissions remove <tool>  — 移除规则[/dim]")
            self.context.console.print("  [dim]/permissions reset           — 清空所有规则[/dim]")
            self.context.console.print()
        else:
            print("\n  Tool Permissions  (~/.arthera/tool_policy.json)")
            print(f"  Allow:    {', '.join(allowed) or 'none'}")
            print(f"  Deny:     {', '.join(denied) or 'none'}")
            print(f"  Ask:      {', '.join(ask_always) or 'none'}")
            print()
            print("  Usage: /permissions allow|deny|ask|remove|reset <tool>")

    def cmd_config(self, args: str):
        """Show or set CLI configuration."""
        from aria_code.apps.cli.config_paths import config_snapshot
        parts = args.strip().split(maxsplit=1)
        if not parts or parts[0] == "show":
            # Show current config
            cfg = self.terminal.config
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print("[bold]Configuration[/bold]")
                self.context.console.print()
                snap = config_snapshot()
                for key in ("api_url", "ollama_url", "local_provider", "model",
                            "provider_fallback", "llm_base_url", "thinking_mode",
                            "command_policy", "permission_mode", "network_enabled",
                            "approval_mode", "auto_approve_level", "review_gate", "delivery_report",
                            "write_policy", "lsp_autocheck", "input_style", "input_theme",
                            "response_footer", "auto_compact_context",
                            "auto_compact_threshold", "auto_save_sessions"):
                    val = cfg.get(key, "-")
                    self.context.console.print(f"  [dim]{key:<24s}[/dim]{val}")
                self.context.console.print(f"  [dim]{'config_dir':<24s}[/dim]{snap['config_dir']}")
                self.context.console.print(f"  [dim]{'config_file':<24s}[/dim]{snap['config_file']}")
                self.context.console.print(f"  [dim]{'sessions_dir':<24s}[/dim]{snap['sessions_dir']}")
                self.context.console.print(f"  [dim]{'user_output_root':<24s}[/dim]{snap['user_output_root']}")
                # Show notification/search config from resolved config.json
                try:
                    import json as _j
                    _ncfg_path = pathlib.Path(snap["config_file"])
                    _ncfg = _j.loads(_ncfg_path.read_text()) if _ncfg_path.exists() else {}
                    if _wh := _ncfg.get("notify_webhook"):
                        self.context.console.print(f"  [dim]{'notify_webhook':<24s}[/dim]{_wh[:50]}{'…' if len(_wh)>50 else ''}")
                except Exception:
                    pass
                import os as _os_show
                if _os_show.getenv("BRAVE_SEARCH_API_KEY"):
                    self.context.console.print(f"  [dim]{'brave_key':<24s}[/dim][green]已配置[/green]")
                else:
                    self.context.console.print(f"  [dim]{'brave_key':<24s}[/dim][dim]未设置 — /config set brave_key=BSAAxxx[/dim]")
                # Security check: warn if providers.json has plaintext api_key
                _pf = pathlib.Path(snap["providers_file"])
                if _pf.exists():
                    try:
                        _pd = _j.loads(_pf.read_text())
                        _has_plain = any(
                            v.get("api_key") for v in _pd.values()
                            if isinstance(v, dict) and v.get("api_key")
                            and not str(v["api_key"]).startswith("${")
                        )
                        if _has_plain:
                            self.context.console.print()
                            self.context.console.print(
                                "  [yellow]⚠  ~/.arthera/providers.json 含明文 API Key[/yellow]\n"
                                "  [dim]  建议迁移到环境变量: export OPENAI_API_KEY=sk-...[/dim]\n"
                                "  [dim]  然后删除 providers.json 中的 api_key 字段[/dim]"
                            )
                    except Exception:
                        pass
                self.context.console.print()
            else:
                for key in ("api_url", "ollama_url", "local_provider", "model",
                            "provider_fallback", "llm_base_url", "thinking_mode",
                            "command_policy", "permission_mode", "network_enabled",
                            "approval_mode", "auto_approve_level", "review_gate", "delivery_report",
                            "write_policy", "lsp_autocheck", "input_style", "input_theme",
                            "response_footer", "auto_compact_context",
                            "auto_compact_threshold"):
                    print(f"  {key}: {cfg.get(key, '-')}")
        elif len(parts) == 2 and parts[0] == "set":
            # Parse key=value
            kv = parts[1].split("=", 1)
            if len(kv) == 2:
                key, val = kv[0].strip(), kv[1].strip()
                # Validate known config keys
                if key == "command_policy":
                    if val not in {"safe", "balanced", "full"}:
                        msg = "command_policy must be one of: safe | balanced | full"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "permission_mode":
                    if val not in {"read-only", "workspace-write", "full-access"}:
                        msg = "permission_mode must be one of: read-only | workspace-write | full-access"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "approval_mode":
                    if val not in {"manual", "risk"}:
                        msg = ("approval_mode must be one of: manual | risk "
                               "(risk: auto-approve actions at or below auto_approve_level)")
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "auto_approve_level":
                    if val not in {"0", "1", "2"}:
                        msg = "auto_approve_level must be 0, 1 or 2 (L3 and L4 always ask)"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                    val = int(val)
                elif key in {"network_enabled", "data_sharing", "feedback_upload", "review_gate", "delivery_report"}:
                    if val.lower() in {"true", "1", "yes", "on"}:
                        val = True
                    elif val.lower() in {"false", "0", "no", "off"}:
                        val = False
                    else:
                        msg = f"{key} must be: true | false"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "thinking_mode":
                    if val not in {"auto", "instant", "thinking"}:
                        msg = "thinking_mode must be one of: auto | instant | thinking"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "provider_fallback":
                    if val not in {"off", "configured", "auto"}:
                        msg = "provider_fallback must be: off | configured | auto"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "local_provider":
                    from aria_code.apps.cli.providers.chat_routing import normalize_provider_name

                    val = normalize_provider_name(val)
                elif key == "model":
                    resolved = _get_MODEL_ALIASES().get(val) or (val if val in _get_MODELS() else None)
                    if not resolved:
                        valid = ", ".join(sorted(_get_MODEL_ALIASES().keys()))
                        msg = f"Unknown model '{val}'. Valid: {valid}"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                    val = _get_MODELS()[resolved]["id"]
                elif key == "auto_save_sessions":
                    if val.lower() in {"true", "1", "yes", "on"}:
                        val = True
                    elif val.lower() in {"false", "0", "no", "off"}:
                        val = False
                    else:
                        msg = "auto_save_sessions must be: true | false"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "auto_compact_context":
                    if val.lower() in {"true", "1", "yes", "on"}:
                        val = True
                    elif val.lower() in {"false", "0", "no", "off"}:
                        val = False
                    else:
                        msg = "auto_compact_context must be: true | false"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "lsp_autocheck":
                    if val.lower() in {"true", "1", "yes", "on"}:
                        val = True
                    elif val.lower() in {"false", "0", "no", "off"}:
                        val = False
                    else:
                        msg = "lsp_autocheck must be: true | false"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "auto_compact_threshold":
                    try:
                        val = float(val)
                    except Exception:
                        msg = "auto_compact_threshold must be a number between 0.50 and 0.95"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                    if not 0.50 <= val <= 0.95:
                        msg = "auto_compact_threshold must be between 0.50 and 0.95"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "write_policy":
                    if val not in {"desktop_only", "confirm_outside", "always_confirm"}:
                        msg = "write_policy must be: desktop_only | confirm_outside | always_confirm"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "local_mode":
                    if val.lower() in {"true", "1", "yes", "on"}:
                        val = True
                    elif val.lower() in {"false", "0", "no", "off"}:
                        val = False
                    else:
                        msg = "local_mode must be: true | false"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "banner":
                    if val not in {"full", "compact", "off"}:
                        msg = "banner must be: full | compact | off"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "input_style":
                    if val not in {"panel", "box", "plain"}:
                        msg = "input_style must be: panel | box | plain"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "input_theme":
                    if val not in {"auto", "dark", "light"}:
                        msg = "input_theme must be: auto | dark | light"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "response_footer":
                    if val not in {"compact", "full", "off"}:
                        msg = "response_footer must be: compact | full | off"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                elif key == "ui_lang":
                    if val not in {"zh", "en", "ja", "ko", "auto"}:
                        msg = "ui_lang must be: zh | en | auto  (auto = detect from OS locale)"
                        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                        return
                    if val == "auto":
                        try:
                            from aria_code.apps.cli.i18n import detect_system_lang as _dsl
                            val = _dsl()
                        except Exception:
                            val = "en"
                    msg = f"✓ UI 语言已设为 {val}  (重启生效)" if val == "zh" else f"✓ UI language set to {val}  (takes effect on restart)"
                    self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
                    self.terminal.config[key] = val
                    self.context.save_config(self.terminal.config)
                    return
                elif key == "notify_webhook":
                    # /config set notify_webhook=https://qyapi.weixin.qq.com/...
                    # 写入 ~/.arthera/config.json（notification_tools 直接读取）
                    try:
                        _ncfg_path = aria_home() / "config.json"
                        _ncfg = json.loads(_ncfg_path.read_text()) if _ncfg_path.exists() else {}
                        _ncfg["notify_webhook"] = val
                        _ncfg_path.write_text(json.dumps(_ncfg, indent=2, ensure_ascii=False))
                    except Exception as _e:
                        logger.debug("notify_webhook save failed: %s", _e)
                    msg = f"✓ 通知 Webhook 已设为 {val[:60]}…" if len(val) > 60 else f"✓ 通知 Webhook 已设为 {val}"
                    self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
                    return
                elif key == "brave_key":
                    # /config set brave_key=BSAAxxx  → 写入 ~/.aria/.env
                    _env_path = pathlib.Path.home() / ".aria" / ".env"
                    _env_path.parent.mkdir(parents=True, exist_ok=True)
                    existing = _env_path.read_text() if _env_path.exists() else ""
                    import re as _re_cfg
                    if "BRAVE_SEARCH_API_KEY" in existing:
                        existing = _re_cfg.sub(r"BRAVE_SEARCH_API_KEY=.*", f"BRAVE_SEARCH_API_KEY={val}", existing)
                    else:
                        existing = existing.rstrip("\n") + f"\nBRAVE_SEARCH_API_KEY={val}\n"
                    _env_path.write_text(existing)
                    _env_path.chmod(0o600)
                    import os as _os_cfg
                    _os_cfg.environ["BRAVE_SEARCH_API_KEY"] = val
                    msg = "✓ Brave Search API key 已保存到~/.aria/.env (生效于当前会话)"
                    self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
                    return
                elif key == "custom_endpoint":
                    # /config set custom_endpoint=http://my-litellm:4000/v1
                    # Automatically sets local_provider=custom
                    self.terminal.config["local_provider"] = "custom"
                    self.terminal.config["custom_endpoint"] = val
                    _sync_write_policy(self.terminal.config)
                    self.context.save_config(self.terminal.config)
                    msg = f"✓ 自定义 endpoint 设为 {val}  (local_provider=custom)"
                    self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
                    return
                elif key == "custom_model":
                    # /config set custom_model=gpt-4o
                    self.terminal.config["custom_model"] = val
                    if self.terminal.config.get("local_provider") == "custom":
                        self.terminal.config["model"] = val
                    _sync_write_policy(self.terminal.config)
                    self.context.save_config(self.terminal.config)
                    self.context.console.print(f"  [dim]custom_model[/dim] = {val}" if self.context.has_rich else f"  custom_model = {val}")
                    return
                self.terminal.config[key] = val
                _sync_write_policy(self.terminal.config)
                self.context.save_config(self.terminal.config)
                self.context.console.print(f"  [dim]{key}[/dim] = {val}" if self.context.has_rich else f"  {key} = {val}")
            else:
                self.context.console.print("[dim]Usage: /config set key=value[/dim]" if self.context.has_rich
                              else "Usage: /config set key=value")
        elif parts[0] == "reload":
            fresh = load_config()
            self.terminal.config.update(fresh)
            msg = f"Config reloaded from {config_snapshot()['config_file']}"
            self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)

        elif parts[0] == "allow":
            # /config allow <tool_name>  — permanently auto-approve this tool
            if len(parts) < 2:
                msg = "Usage: /config allow <tool_name>  (e.g. /config allow read_file)"
                self.context.console.print(f"[dim]{msg}[/dim]") if self.context.has_rich else print(msg)
                return
            tool = parts[1].strip()
            try:
                from aria_code.runtime.tool_policy import add_to_policy
                add_to_policy(tool, "allow")
                msg = f"✓ 工具 '{tool}' 加入永久白名单（始终自动批准，无需确认）"
                self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
            except Exception as _e:
                self.context.console.print(f"[red]Error: {_e}[/red]") if self.context.has_rich else print(f"Error: {_e}")

        elif parts[0] == "deny":
            # /config deny <tool_name>  — permanently block this tool
            if len(parts) < 2:
                msg = "Usage: /config deny <tool_name>  (e.g. /config deny run_command)"
                self.context.console.print(f"[dim]{msg}[/dim]") if self.context.has_rich else print(msg)
                return
            tool = parts[1].strip()
            try:
                from aria_code.runtime.tool_policy import add_to_policy
                add_to_policy(tool, "deny")
                msg = f"✓ 工具 '{tool}' 加入黑名单（始终拒绝，不会执行）"
                self.context.console.print(f"[red]{msg}[/red]") if self.context.has_rich else print(msg)
            except Exception as _e:
                self.context.console.print(f"[red]Error: {_e}[/red]") if self.context.has_rich else print(f"Error: {_e}")

        elif parts[0] == "ask":
            # /config ask <tool_name>  — always prompt before this tool
            if len(parts) < 2:
                msg = "Usage: /config ask <tool_name>  (e.g. /config ask write_file)"
                self.context.console.print(f"[dim]{msg}[/dim]") if self.context.has_rich else print(msg)
                return
            tool = parts[1].strip()
            try:
                from aria_code.runtime.tool_policy import add_to_policy
                add_to_policy(tool, "ask")
                msg = f"✓ 工具 '{tool}' 设为始终询问（每次执行前都弹出确认）"
                self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
            except Exception as _e:
                self.context.console.print(f"[red]Error: {_e}[/red]") if self.context.has_rich else print(f"Error: {_e}")

        elif parts[0] == "policy":
            # /config policy          — show all policy settings
            # /config policy reset    — reset to defaults
            sub = parts[1].lower() if len(parts) > 1 else "show"
            try:
                from aria_code.runtime.tool_policy import load_tool_policy, save_tool_policy, remove_from_policy
                if sub == "reset":
                    save_tool_policy({"allowed": [], "denied": [], "ask_always": []})
                    msg = "✓ 工具权限策略已重置为默认值"
                    self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
                elif sub in ("remove", "rm") and len(parts) > 2:
                    tool = parts[2].strip()
                    removed = remove_from_policy(tool)
                    if removed:
                        msg = f"✓ '{tool}' 已从策略中移除"
                        self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
                    else:
                        msg = f"'{tool}' 不在任何策略列表中"
                        self.context.console.print(f"[dim]{msg}[/dim]") if self.context.has_rich else print(msg)
                else:
                    policy = load_tool_policy()
                    if self.context.has_rich:
                        self.context.console.print()
                        self.context.console.print("  [bold]工具权限策略[/bold]  [dim]~/.arthera/tool_policy.json[/dim]")
                        self.context.console.print()
                        allowed = policy.get("allowed", [])
                        denied = policy.get("denied", [])
                        asked = policy.get("ask_always", [])
                        if allowed:
                            self.context.console.print(f"  [green]✓ 白名单（自动允许）:[/green]  {', '.join(allowed)}")
                        else:
                            self.context.console.print("  [dim]✓ 白名单: 空[/dim]")
                        if denied:
                            self.context.console.print(f"  [red]✗ 黑名单（始终拒绝）:[/red]  {', '.join(denied)}")
                        else:
                            self.context.console.print("  [dim]✗ 黑名单: 空[/dim]")
                        if asked:
                            self.context.console.print(f"  [yellow]? 始终询问:[/yellow]  {', '.join(asked)}")
                        else:
                            self.context.console.print("  [dim]? 始终询问: 空[/dim]")
                        self.context.console.print()
                        self.context.console.print("  [dim]/config allow <tool>   — 加入白名单[/dim]")
                        self.context.console.print("  [dim]/config deny  <tool>   — 加入黑名单[/dim]")
                        self.context.console.print("  [dim]/config ask   <tool>   — 设为始终询问[/dim]")
                        self.context.console.print("  [dim]/config policy remove <tool>  — 移除策略[/dim]")
                        self.context.console.print("  [dim]/config policy reset          — 清空所有策略[/dim]")
                        self.context.console.print()
                    else:
                        print("\n  Tool Policy:")
                        print(f"  Allowed:    {', '.join(policy.get('allowed', [])) or 'none'}")
                        print(f"  Denied:     {', '.join(policy.get('denied', [])) or 'none'}")
                        print(f"  Ask-always: {', '.join(policy.get('ask_always', [])) or 'none'}")
            except Exception as _e:
                self.context.console.print(f"[red]Error: {_e}[/red]") if self.context.has_rich else print(f"Error: {_e}")

        else:
            self.context.console.print(
                "[dim]Usage: /config [show] | /config set key=value | /config reload\n"
                "       /config allow <tool> | /config deny <tool> | /config ask <tool>\n"
                "       /config policy [reset|remove <tool>][/dim]"
                if self.context.has_rich else
                "Usage: /config [show] | /config set key=value | /config reload\n"
                "       /config allow <tool> | /config deny <tool> | /config ask <tool>\n"
                "       /config policy [reset|remove <tool>]"
            )
