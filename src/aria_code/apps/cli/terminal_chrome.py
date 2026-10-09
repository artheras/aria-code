"""What frames the conversation: the banner, the status line, the toolbar, the keys.

Moved out of aria_cli.py unchanged: ArtheraTerminal.print_header, _status_line,
_build_keybindings, _workspace_git_state and _bottom_toolbar. They still use
aria_cli's module-level names; aria_cli binds a copy of ChromeMixin to its own
namespace with mixin_binding.bind_mixin and ArtheraTerminal inherits it.
"""

from __future__ import annotations


class ChromeMixin:
    """The terminal's banner, status line, bottom toolbar and key bindings."""

    def print_header(self):
        # Resolve current model info
        current_id  = self.config.get("model", "qwen2.5:7b")

        # ── 模型自动配对（仅限本地模型）──────────────────────────────────────
        # 检测本机已安装的 Ollama 模型；若配置的**本地**模型未安装，配对到
        # 一个能力足够的可用模型并持久化（与运行时 fallback 共用选择逻辑）。
        #
        # 云端模型不参与配对。Ollama 的列表里永远没有 google/gemini-2.5-pro,
        # 所以这个判断对云端配置恒为真 —— 以前它会把用户显式选的云端模型改掉
        # 并 save_config() 落盘,每次启动都改一次。pick_best_installed_model
        # 现在对 provider 前缀的 id 直接返回 None。
        self._auto_healed_from: Optional[str] = None   # 原配置模型（仅本次显示用）
        self._ollama_alive = False
        self._installed_models: set = set()
        try:
            _rm, _ = detect_ollama_models_rich(
                self.config.get("ollama_url", "http://localhost:11434"))
            self._installed_models = {_x["name"] for _x in _rm}
            self._ollama_alive = bool(self._installed_models)
        except Exception:
            pass
        # Only a model this session would run on Ollama is paired. The check
        # below it looks for a provider prefix, so a bare cloud id such as
        # "gemini-3.8-flash" (with local_provider=vertex, or backend_chat on)
        # read as a missing local model: it was replaced by whatever Ollama
        # had installed — a 1.5B model — and the change saved to the config.
        try:
            from aria_code.apps.cli.providers.chat_routing import first_round_route
            _runs_on_ollama = first_round_route(current_id, self.config, self.api_url) == "ollama"
        except Exception:
            _runs_on_ollama = False
        if _runs_on_ollama and self._installed_models and current_id not in self._installed_models:
            _resolved = _pick_best_installed_model(self._installed_models, current_id)
            if _resolved:
                self._auto_healed_from = current_id
                current_id = _resolved
                self.config["model"] = _resolved
                self._actual_model = None   # config now matches reality
                try:
                    save_config(self.config)
                except Exception:
                    pass

        current_key = next((k for k, v in MODELS.items() if v["id"] == current_id), None)
        _default_m  = MODELS.get("qwen7b") or MODELS.get("qwen-fast") or next(iter(MODELS.values()))
        m = MODELS.get(current_key, _default_m) if current_key else _default_m
        cwd = os.getcwd()
        _git_branch, _git_dirty = self._workspace_git_state()
        # Shorten home directory to ~
        home = os.path.expanduser("~")
        if cwd.startswith(home):
            cwd = "~" + cwd[len(home):]
        wl = self.config.get("watchlist", [])
        tool_count = len(ARIA_TOOLS) + len(LOCAL_TOOLS)
        skill_count = len(SKILLS)
        _mcp_configured = 0
        if _HAS_MCP:
            try:
                _mcp_cfg = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
                _mcp_configured = sum(
                    1 for item in (_mcp_cfg.get("servers") or [])
                    if isinstance(item, dict) and item.get("enabled", True)
                )
            except Exception:
                pass

        # Watchlist string
        wl_str = ""
        if wl:
            wl_str = ", ".join(wl[:5])
            if len(wl) > 5:
                wl_str += f" +{len(wl) - 5}"

        from apps.cli.providers.chat_routing import model_provider
        _provider = model_provider(current_id)
        _cloud_provider = bool(_provider and _provider not in {"ollama", "lmstudio"})
        _runtime = (
            "cloud" if self.config.get("backend_chat") or _cloud_provider
            or m.get("badge") == "Cloud" or "cloud" in current_id.lower()
            else "local"
        )
        _badge = "Cloud" if _runtime == "cloud" else m.get("badge", "")
        _uses_google = (
            _provider in {"google", "vertexai", "vertex-ai", "google-genai"}
            or current_id.lower().startswith("gemini")
            or self.config.get("local_provider") in {"vertex", "google"}
        )
        _missing = ""
        if _uses_google and not self.config.get("backend_chat"):
            from apps.cli.providers.base import google_readiness
            _missing = google_readiness(self.config)
        _health_status = (
            ("Google Cloud · via Arthera API" if _uses_google else "Arthera API · cloud")
            if self.config.get("backend_chat") else
            f"⚠ {_missing} · /model" if _missing else
            "Cloud model configured" if _cloud_provider else
            self._ollama_status_label(rich=True)
        )
        _banner_mode = self._session_banner_mode or self.config.get("banner", "full")
        _mascot = "[bold #C08050]▣[/bold #C08050]"

        if _banner_mode == "off":
            return   # Silent startup for scripts / automation

        if HAS_RICH:
            console.print()

            _ui_lang = self.config.get("ui_lang", "en") or "en"
            if _banner_mode == "compact":
                _model_label = f"{m['name']} {m['version']}" if current_key else current_id
                from ui.banner import render_compact_banner as _rcb
                try:
                    from apps.cli.update_check import get_update_notice as _gun
                    _update_notice = _gun(wait_ms=0)
                except Exception:
                    _update_notice = None
                _rcb(
                    version=__version__,
                    model_label=_model_label,
                    runtime=_runtime,
                    cwd=cwd,
                    control_status_rich=self._control_status_label(rich=True),
                    tool_count=tool_count,
                    update_notice=_update_notice,
                    console=console,
                    has_rich=HAS_RICH,
                    lang=_ui_lang,
                )
            else:
                _model_label = f"{m['name']} {m['version']}" if current_key else current_id
                try:
                    from apps.cli.i18n import t as _i18n_t
                    _lite_word  = _i18n_t("lite", lang=_ui_lang)
                    _cloud_word = _i18n_t("cloud", lang=_ui_lang)
                    _local_word = _i18n_t("local", lang=_ui_lang)
                except Exception:
                    _lite_word, _cloud_word, _local_word = "lite", "cloud", "local"
                if _badge == "Fast":
                    _rt_label = f"{_model_label}  [dim]{_lite_word}[/dim]"
                elif _badge == "Cloud":
                    _rt_label = f"{_model_label}  [dim]{_cloud_word}[/dim]"
                else:
                    _rt_label = f"{_model_label}  [dim]{_local_word}[/dim]"

                _best_id = (MODELS.get("qwen7b") or {}).get("id", "qwen2.5:7b")
                from ui.banner import render_startup_dashboard as _rsd, render_try_hints as _rth
                from ui.startup_dashboard import StartupDashboardViewModel as _StartupDashboardViewModel
                try:
                    from apps.cli.update_check import get_update_notice as _gun
                    _update_notice = _gun(wait_ms=0)
                except Exception:
                    _update_notice = None
                _first_run = not bool(self.config.get("first_run_seen"))
                _dashboard = _StartupDashboardViewModel(
                    version=__version__,
                    runtime_label=_rt_label,
                    cwd=cwd,
                    control_status=self._control_status_label(rich=True),
                    health_status=_health_status,
                    tool_count=tool_count,
                    skill_count=skill_count,
                    lang=_ui_lang,
                    first_run=_first_run,
                    update_notice=_update_notice,
                    auto_healed_from=self._auto_healed_from or "",
                    current_id=current_id,
                    badge=_badge,
                    best_lite_id=_best_id,
                    best_lite_installed=_best_id in self._installed_models,
                    git_branch=_git_branch,
                    git_dirty=_git_dirty,
                    mcp_server_count=_mcp_configured,
                )
                _rsd(
                    _dashboard,
                    console=console,
                    has_rich=HAS_RICH,
                    rich_box=rich_box,
                )
                _rth(console, HAS_RICH, lang=_ui_lang)
                if not self.config.get("first_run_seen"):
                    self.config["first_run_seen"] = True
                    save_config(self.config)
                    # One-time transparency note. Unlike Claude Code (which does
                    # NOT train on feedback), Aria may use opted-in feedback to
                    # improve its finance model — so disclose it up front.
                    import os as _os
                    if not _os.environ.get("ARIA_NO_TELEMETRY"):
                        # Padded, not prefixed with spaces: in 80 columns the
                        # second line fell back to column 0.
                        from rich.padding import Padding as _Pad
                        from rich.text import Text as _Txt
                        console.print(_Pad(_Txt.from_markup(
                            "[dim]隐私：反馈默认[bold]仅存本地[/bold]，不上传。"
                            "opt-in 后可用于改进金融模型 · /privacy 查看与开关 · /bug 报告问题[/dim]"
                            if str(_ui_lang).lower().startswith("zh") else
                            "[dim]Feedback stays [bold]on this machine[/bold] unless you opt in to "
                            "help improve Aria · /privacy · /bug[/dim]"
                        ), (0, 0, 0, 2)))
        else:
            if _banner_mode != "off":
                from ui.banner import render_full_banner as _rfb
                _rfb(
                    version=__version__,
                    rt_label=_runtime,
                    cwd=cwd,
                    control_status_rich=self._control_status_label(),
                    ollama_status_rich=self._ollama_status_label(),
                    tool_count=tool_count,
                    skill_count=skill_count,
                    console=console,
                    has_rich=HAS_RICH,
                    rich_box=rich_box,
                )

    def _status_line(self) -> str:
        current_id = self.config.get("model", "qwen2.5:7b")
        # If Ollama switched to a different model, show the actual running model
        display_id = self._actual_model or current_id
        model_name = display_id  # fallback: raw model ID
        for k, v in MODELS.items():
            if v["id"] == display_id:
                model_name = v["name"].replace("Aria ", "")
                break
            # also match by actual model ID (e.g. gpt-oss:120b-cloud)
            if v["id"] == current_id and self._actual_model is None:
                model_name = v["name"].replace("Aria ", "")
                break
        # If actual_model differs from config, append a ⚑ warning marker
        _mismatch = (self._actual_model is not None and self._actual_model != current_id)
        if _mismatch:
            model_name = f"{self._actual_model} ⚑"
        # Determine runtime label
        _lp = self._last_provider or ""
        _model_badge = next(
            (v.get("badge", "") for v in MODELS.values() if v["id"] == current_id), ""
        )
        if _lp == "ollama":
            runtime = "local"
        elif _lp in ("deepseek", "openai", "anthropic", "groq", "dashscope", "together"):
            runtime = "cloud"
        elif _model_badge == "Cloud" or "cloud" in current_id.lower():
            runtime = "cloud"
        elif not _lp:
            runtime = "local" if getattr(self, "_ollama_alive", False) else "—"
        else:
            runtime = "cloud"
        # Context source tags
        _ctx_tags = []
        if getattr(self, "_project_session", None):
            _ctx_tags.append(f"proj:{self._project_session.name}")
        elif getattr(self, "_file_session", None) and self._file_session.get_active():
            _ctx_tags.append(f"file:{self._file_session.get_active().filename}")
        _ctx = f"  ·  {_ctx_tags[0]}" if _ctx_tags else ""
        privacy = "share" if bool(self.config.get("data_sharing", False)) else "local-only"
        permission = self.config.get("permission_mode", "workspace-write")
        return f"aria  ·  {runtime}  ·  {permission}  ·  {privacy}{_ctx}"

    def _build_keybindings(self):
        """Build prompt_toolkit KeyBindings for REPL shortcuts."""
        kb = _PTKeyBindings()

        @kb.add("s-tab")
        def _cycle_permission(event):
            """Shift+Tab → cycle permission mode."""
            cur = _ACTIVE_PERMISSION_MODE[0]
            try:
                idx = _PERMISSION_CYCLE.index(cur)
            except ValueError:
                idx = 0
            nxt = _PERMISSION_CYCLE[(idx + 1) % len(_PERMISSION_CYCLE)]
            _ACTIVE_PERMISSION_MODE[0] = nxt
            self.config["permission_mode"] = nxt
            label = {"read-only": "🔒 read-only", "workspace-write": "✏️  workspace-write", "full-access": "⚡ full-access"}.get(nxt, nxt)
            event.app.current_buffer.text = ""
            # Print inline so user sees the change immediately
            import sys as _sys
            _sys.stderr.write(f"\r  Mode → {label}                \n")
            _sys.stderr.flush()

        @kb.add("escape", "t")
        def _toggle_thinking(event):
            """Alt+T → toggle thinking mode."""
            cur = self.config.get("thinking", False)
            self.config["thinking"] = not cur
            state = "ON" if not cur else "OFF"
            import sys as _sys
            _sys.stderr.write(f"\r  Thinking → {state}             \n")
            _sys.stderr.flush()

        @kb.add("escape", "p")
        def _switch_model(event):
            """Alt+P → insert /model into prompt buffer."""
            buf = event.app.current_buffer
            if not buf.text:
                buf.text = "/model "
                buf.cursor_position = len(buf.text)

        @kb.add("c-l")
        def _redraw(event):
            """Ctrl+L → clear and redraw screen."""
            event.app.renderer.clear()

        @kb.add("c-o")
        def _toggle_transcript(event):
            """Ctrl+O → show/hide recent tool calls + full thinking of last turn."""
            self._transcript_visible = not self._transcript_visible
            _details = list(getattr(self, "_action_details", None) or [])
            if self._transcript_visible and (self._transcript_log or self._last_thinking or _details):
                import sys as _sys
                _sys.stderr.write("\n")
                if self._last_thinking:
                    _sys.stderr.write("  ✻ Thinking\n")
                    for tline in self._last_thinking.splitlines() or [self._last_thinking]:
                        # wrap-soft: indent each line, cap very long lines
                        _sys.stderr.write(f"    {tline[:200]}\n")
                    _sys.stderr.write("\n")
                if _details:
                    # Every action of the last turn in full: commands with
                    # their whole output, edits with their whole diff.
                    from aria_code.ui.render.actions import format_action_details
                    _sys.stderr.write("  ⏺ Actions\n")
                    for _style, line in format_action_details(_details):
                        _sys.stderr.write(f"    {line}\n")
                elif self._transcript_log:
                    _sys.stderr.write("  ⏺ Tool calls\n")
                    for line in self._transcript_log[-20:]:
                        _sys.stderr.write(f"    {line}\n")
                _sys.stderr.write("  [Ctrl+O to close]\n\n")
                _sys.stderr.flush()
            else:
                self._transcript_visible = False

        @kb.add("c-t")
        def _toggle_tasklist(event):
            """Ctrl+T → show/hide task list."""
            self._task_list_visible = not self._task_list_visible
            if self._task_list_visible and self._task_list:
                import sys as _sys
                _sys.stderr.write("\n  📋 Tasks:\n")
                icons = {"pending": "○", "in_progress": "◉", "completed": "✓", "failed": "✗"}
                for t in self._task_list:
                    icon = icons.get(t.get("status", "pending"), "○")
                    _sys.stderr.write(f"    {icon} {t.get('title', '')}\n")
                _sys.stderr.write("\n")
                _sys.stderr.flush()

        return kb

    def _workspace_git_state(self) -> tuple[str, bool]:
        """Return the current branch and tracked-worktree dirty state."""
        try:
            import subprocess as _sp
            branch = _sp.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                stderr=_sp.DEVNULL,
                timeout=1,
            ).decode().strip()
            if not branch or branch == "HEAD":
                return "", False
            dirty = bool(_sp.check_output(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                stderr=_sp.DEVNULL,
                timeout=1,
            ).decode().strip())
            return branch, dirty
        except Exception:
            return "", False

    def _bottom_toolbar(self):
        """Bottom toolbar content for prompt_toolkit."""
        model_label, cwd, privacy, est_tokens, max_ctx = self._bottom_toolbar_parts()
        ctx_color = "#606060" if est_tokens / max_ctx < 0.6 else (
            "#aa8800" if est_tokens / max_ctx < 0.85 else "#cc4444"
        )
        perm = _ACTIVE_PERMISSION_MODE[0]
        perm_color = {"read-only": "#888800", "workspace-write": "#606060", "full-access": "#cc4444"}.get(perm, "#606060")
        perm_short = {"read-only": "ro", "workspace-write": "rw", "full-access": "full"}.get(perm, perm)
        # PR / git branch info
        _branch_name, _git_dirty = self._workspace_git_state()
        _branch = f" ⎇ {_branch_name}{'*' if _git_dirty else ''}" if _branch_name else ""
        # Task list indicator
        _tasks = ""
        if self._task_list:
            _done = sum(1 for t in self._task_list if t.get("status") == "completed")
            _total = len(self._task_list)
            _tasks = f" · ✓{_done}/{_total}"
        return HTML(
            f'<style fg="#C08050">{model_label}</style>'
            f'<style fg="#8a8a8a"> · {cwd}{_branch}{_tasks} · </style>'
            f'<style fg="{perm_color}">{perm_short}</style>'
            f'<style fg="#8a8a8a"> · {privacy} · /help · </style>'
            f'<style fg="{ctx_color}">{est_tokens:,}/{max_ctx:,}</style>'
        )


__all__ = ["ChromeMixin"]
