"""One interactive chat turn: ArtheraTerminal.send_message.

Moved out of aria_cli.py unchanged, at 1,200 lines the largest method there.
It still runs on aria_cli's module-level names (console, LOCAL_TOOLS,
_PROJECT_CONTEXT, _run_deterministic_chain, …): aria_cli binds a copy of
ChatTurnMixin to its own namespace with mixin_binding.bind_mixin and
ArtheraTerminal inherits it.
"""

from __future__ import annotations


class ChatTurnMixin:
    """send_message for ArtheraTerminal."""

    async def send_message(self, message: str, system_override: Optional[str] = None,
                           evidence_grounded: bool = False, route_text: bool = False):
        """Send message to Aria AI with agentic tool loop, smart fallback, markdown."""
        if getattr(self, "_streaming", False):
            is_zh = str(self.config.get("ui_lang", "en")).lower().startswith("zh")
            notice = (
                "上一条请求仍在处理中；按 Esc 取消后再提交。"
                if is_zh else
                "A request is already running. Press Esc to cancel it before submitting another."
            )
            if HAS_RICH:
                console.print(f"  [yellow]{notice}[/yellow]")
            else:
                print(f"  {notice}")
            return
        reference_context = ""
        reference_service = getattr(self, "_reference_service", None)
        if reference_service is not None and "@" in message:
            prepared = reference_service.prepare(message)
            if prepared.errors:
                self._print_reference_errors(prepared)
                return
            if prepared.references:
                self._print_reference_summary(prepared)
                message = prepared.expanded_text
                reference_context = prepared.context_block

        # Resolve same-name securities before deterministic routing or the LLM
        # can silently pick the first ticker.  The clarification lives outside
        # conversation history so the eventual request is recorded only once.
        if not system_override and not message.lstrip().startswith("/"):
            from apps.cli.market_universe import (
                ambiguous_market_candidates,
                select_market_candidate,
            )

            def _show_market_choices(name: str, candidates: list[Any]) -> str:
                lines = [f"“{name}”对应多个交易标的，请先确认："]
                for index, candidate in enumerate(candidates, start=1):
                    lines.append(
                        f"{index}. {candidate.name} — {candidate.symbol} · {candidate.market}"
                    )
                lines.append("回复编号、证券代码或市场（如“2”“1234.HK”“港股”）；输入“取消”退出。")
                prompt = "\n".join(lines)
                if HAS_RICH:
                    console.print("\n[bold]Aria[/bold]  [dim]标的确认[/dim]")
                    console.print(make_markdown(prompt))
                    console.print()
                else:
                    print(f"\nAria  标的确认\n{prompt}\n")
                return prompt

            pending_resolution = dict(self._pending_market_resolution or {})
            if pending_resolution:
                candidates = list(pending_resolution.get("candidates") or [])
                selected = select_market_candidate(message, candidates)
                if message.strip().lower() in {"取消", "cancel", "算了", "不用了"}:
                    self._pending_market_resolution = None
                    notice = "已取消标的选择。"
                    console.print(f"[dim]{notice}[/dim]") if HAS_RICH else print(notice)
                    return
                if selected is not None:
                    original = str(pending_resolution.get("original") or "")
                    start = int(pending_resolution.get("start") or 0)
                    mention = str(pending_resolution.get("mention") or "")
                    message = f"{original[:start]}{selected.symbol}{original[start + len(mention):]}"
                    self._pending_market_resolution = None
                    notice = f"已确认：{selected.name} · {selected.symbol} · {selected.market}"
                    if HAS_RICH:
                        console.print(f"[green]✓[/green] {notice}")
                    else:
                        print(notice)
                elif re.fullmatch(r"[\w.^=\-]+|港股|美股|A股|沪深|香港|美国", message.strip(), re.I):
                    _show_market_choices(str(pending_resolution.get("mention") or "该名称"), candidates)
                    return
                else:
                    # A new natural-language request replaces the pending one.
                    self._pending_market_resolution = None

            ambiguous = ambiguous_market_candidates(message)
            if ambiguous:
                start, mention, candidates = ambiguous[0]
                self._pending_market_resolution = {
                    "original": message,
                    "start": start,
                    "mention": mention,
                    "candidates": candidates,
                }
                _show_market_choices(mention, candidates)
                return
        # Only what the person typed is routed to a command. A prompt a command
        # built and sent here was routed too: /report's "为 MSFT 生成一份专业
        # Markdown 投研报告" matched the report route and ran /report again.
        if (
            route_text
            and not system_override
            and self._pending_image is None
            and not (self._file_session is not None and self._file_session.get_active() is not None)
            and self._project_session is None
            and not getattr(self, "_send_message_route_active", False)
        ):
            routed = route_top_level_text(message, set(self.commands.commands))
            if routed is not None:
                self._send_message_route_active = True
                try:
                    _announce_route(routed.text)
                    self._maybe_show_intent_preflight(routed.text)
                    await self.commands.execute(routed.text)
                finally:
                    self._send_message_route_active = False
                return
            routed = _natural_language_visual_artifact_route(message, set(self.commands.commands))
            if routed is not None:
                self._send_message_route_active = True
                try:
                    _announce_route(routed.text)
                    self._maybe_show_intent_preflight(routed.text)
                    await self.commands.execute(routed.text)
                finally:
                    self._send_message_route_active = False
                return
        self._maybe_show_intent_preflight(message)
        # Store optional system prompt override (used by /file analyze)
        self._system_override = system_override
        # Fire prompt_submit hook (Claude Code: UserPromptSubmit)
        _run_event_hook("prompt_submit", {
            "ARIA_MESSAGE":  message[:500],
            "ARIA_SESSION":  self.session_id,
            "ARIA_PROVIDER": self._last_provider,
        })
        # Attach pending image block if /vision was used before this message
        if self._pending_image is not None:
            user_content = [
                {"type": "text", "text": message},
                self._pending_image,
            ]
            self._pending_image = None
        elif (self._file_session is not None and
              self._file_session.get_active() is not None):
            # Inject loaded-file context as a text block before the user question.
            # Only inject for the FIRST message after /file load (tracked via flag),
            # then keep the file in system prompt for follow-up turns.
            _fc = self._file_session.get_active()
            _fc_ctx = self._file_session.build_context_block(max_chars=14_000)
            user_content = f"[文件上下文已加载: {_fc.filename}]\n\n{message}"
            # Persist file context in system prompt so follow-up questions work
            if not hasattr(self, "_file_ctx_injected") or not self._file_ctx_injected:
                self._file_ctx_injected = True
                # Pre-pend file content to the very first user message
                user_content = (f"以下是用户上传的文件内容，请在回答时参考：\n{_fc_ctx}\n\n"
                                f"---用户问题---\n{message}")
        elif self._project_session is not None:
            # Inject project context for the first message, then rely on history
            _ps = self._project_session
            user_content = f"[项目已加载: {_ps.name}]\n\n{message}"
            if not self._project_ctx_injected:
                self._project_ctx_injected = True
                _pc_ctx = _ps.build_llm_context(max_chars=14_000)
                user_content = (
                    f"以下是已加载的项目信息，请在完成任务时参考：\n{_pc_ctx}\n\n"
                    f"---用户请求---\n{message}"
                )
        else:
            user_content = message
            self._file_ctx_injected = False  # reset when no file loaded
            self._project_ctx_injected = False  # reset when no project loaded

        if reference_context:
            if isinstance(user_content, list) and user_content and isinstance(user_content[0], dict):
                first_text = str(user_content[0].get("text") or "")
                user_content[0]["text"] = f"{first_text}\n\n{reference_context}"
            elif isinstance(user_content, str):
                user_content = f"{user_content}\n\n{reference_context}"

        try:
            _incoming_for_compact = (
                json.dumps(user_content, ensure_ascii=False)
                if isinstance(user_content, (list, dict))
                else str(user_content)
            )
            await self._maybe_auto_compact_before_turn(_incoming_for_compact)
        except Exception:
            pass
        self.conversation.append({"role": "user", "content": user_content})

        # ── 路由决策：支持工具调用的模型走 LLM+tool call，否则走确定性路由 ──
        # 支持 function calling 的模型（Claude / GPT-4 class / qwen-72b+）能自己
        # 识别公司名 → ticker 并调 get_market_data，不需要硬编码字典。
        # 本地小模型（<14B）工具调用不稳定，保留确定性路由作降级。
        _curr_model_id = self.config.get("model", "")
        from aria_code.apps.cli.workspace_route import workspace_config
        _runtime_config = workspace_config(message, _curr_model_id, self.config, self.api_url)
        _model_has_tools = False
        if _HAS_MODEL_CAP:
            try:
                _mc = get_model_capability(_curr_model_id)
                _model_has_tools = bool(_mc.tool_calls and _mc.context_window >= 8192)
            except Exception:
                pass
        if _model_has_tools:
            try:
                from apps.cli.providers.chat_routing import model_receives_local_tools
                _model_has_tools = model_receives_local_tools(_curr_model_id, _runtime_config, self.api_url)
            except Exception:
                pass

        # ── Broker guide intent: broad discovery should not start an add wizard ──
        if _is_broker_guide_intent(message):
            if HAS_RICH:
                console.print("\n[bold]Aria[/bold]  [dim]  正在打开券商与服务指南…[/dim]\n")
            await self.commands.cmd_broker("guide")
            await self.commands.cmd_broker("services")
            await self.commands.cmd_packages("services")
            return

        # ── Broker setup intent: intercept before LLM / deterministic routing ──
        if _is_broker_setup_intent(message):
            _btype = _detect_broker_type(message)
            if HAS_RICH:
                from apps.cli.utils.market_detect import _BROKER_SETUP_NAMES
                _display = _BROKER_SETUP_NAMES.get(_btype, ("",))[0] if _btype else ""
                _label = f"  正在启动{_display}配置向导…" if _display else "  正在启动券商配置向导…"
                console.print(f"\n[bold]Aria[/bold]  [dim]{_label}[/dim]\n")
            await self.commands._cmd_broker_add(_btype)
            return

        if _is_artifact_action_followup(message) and getattr(self, "_pending_market_artifact", None):
            pending_artifact = dict(self._pending_market_artifact or {})
            action_summary = _handle_pending_artifact_action(pending_artifact, message)
            if HAS_RICH:
                console.print("\n[bold]Aria[/bold]\n")
                console.print(make_markdown(_strip_latex(action_summary)))
                console.print()
            else:
                print("\nAria\n")
                print(action_summary)
            self.conversation.append({"role": "assistant", "content": action_summary})
            return

        if _is_artifact_location_followup(message) and getattr(self, "_pending_market_artifact", None):
            pending_artifact = dict(self._pending_market_artifact or {})
            _print_pending_artifact_location(pending_artifact)
            _paths = [
                str(pending_artifact.get(key) or "").strip()
                for key in ("pine_path", "html_path", "png_path", "chart_path", "path", "raw_path", "url")
                if str(pending_artifact.get(key) or "").strip()
            ]
            _children = pending_artifact.get("children") or []
            if isinstance(_children, list):
                for _child in _children:
                    if isinstance(_child, dict):
                        _child_path = str(_child.get("html_path") or _child.get("chart_path") or _child.get("path") or "").strip()
                        if _child_path:
                            _paths.append(_child_path)
            _summary = "最近生成文件：\n" + "\n".join(f"- {p}" for p in _paths) if _paths else "最近任务尚未记录具体文件路径。"
            self.conversation.append({"role": "assistant", "content": _summary})
            return

        if _is_market_artifact_followup(message) and getattr(self, "_pending_market_artifact", None):
            pending_artifact = dict(self._pending_market_artifact or {})
            symbol = str(pending_artifact.get("symbol") or "").strip()
            period = str(pending_artifact.get("period") or "1y").strip() or "1y"
            if symbol:
                if HAS_RICH:
                    console.print(f"\n[bold]Aria[/bold]\n")
                    console.print(f"  [dim]继续上一项市场图表任务：{symbol} {period}[/dim]")
                await self.commands.cmd_chart(f"{symbol} {period}")
                return

        # ── Football prediction intercept → built-in Poisson handler ──────────
        if await self._try_football_nl_intercept(message):
            return

        _det_wants_analysis = False  # set True for snapshot + analysis query → LLM follows
        deterministic = _run_deterministic_chain(
            message, model_has_tools=_model_has_tools, history=self.conversation[:-1])
        if deterministic.get("success") or _is_stock_chart_analysis_request(message):
            # The handlers answer in the language of the question; so does the
            # frame around their answer.
            _det_en = _detect_lang(message) == "en"
            final_text = deterministic.get("response", "")
            if not final_text:
                final_text = (
                    f"Market analysis did not finish: {deterministic.get('error', 'unknown error')}"
                    if _det_en else
                    f"市场分析未完成：{deterministic.get('error', '未知错误')}"
                )
            _tools = deterministic.get("tools_used", [])
            _tool_label = ({
                "market_snapshot": "Market snapshot",
                "stock_chart":     "Chart analysis",
                "broker_query":    "Account data",
                "realty_query":    "Real-estate data",
                "strategy_advice":  "Strategy framework",
            } if _det_en else {
                "market_snapshot": "市场快照",
                "stock_chart":     "图表分析",
                "broker_query":    "账户数据",
                "realty_query":    "房地产数据",
                "strategy_advice":  "策略框架",
            }).get(_tools[0], _tools[0]) if _tools else ("Local analysis" if _det_en else "本地分析")
            _rate_limited = deterministic.get("rate_limited", False)
            _rl_note = (
                f"  [yellow]⚠ {'Data source rate-limited' if _det_en else '数据源限流'}[/yellow]"
                if _rate_limited else ""
            )
            if _tools and _tools[0] == "stock_chart":
                _chart_symbol = deterministic.get("symbol") or _extract_market_symbol(message)
                self._pending_market_artifact = {
                    "kind": "stock_chart",
                    "symbol": _chart_symbol,
                    "period": "1y",
                    "html_path": deterministic.get("chart_path", ""),
                    "command": f"/chart {_chart_symbol or 'AAPL'} 1y",
                }
            if _tools and _tools[0] == "market_snapshot":
                _snapshot_now = time.time()
                if not _is_market_snapshot_refresh_request(message):
                    _repeat_notice = _build_market_snapshot_repeat_notice(
                        deterministic,
                        self._last_market_snapshot_cache,
                        now=_snapshot_now,
                        lang="en" if _det_en else "zh",
                    )
                    if _repeat_notice:
                        final_text = _repeat_notice
                        deterministic = dict(
                            deterministic,
                            response=final_text,
                            analysis_complete=True,
                            compressed_repeat=True,
                        )
                _cache_entry = _market_snapshot_cache_entry(deterministic, now=_snapshot_now)
                if _cache_entry:
                    self._last_market_snapshot_cache = _cache_entry
            # For snapshot + "分析" queries: show data then continue to LLM for deep analysis
            _det_wants_analysis = (
                not bool(deterministic.get("analysis_complete"))
                and any(k in message for k in ("分析", "analyze", "analysis", "对比", "比较", "compare"))
                and bool(_tools) and _tools[0] == "market_snapshot"
            )
            _disclaimer = "" if _tools and _tools[0] == "strategy_advice" else (
                " · Not investment advice" if _det_en else " · 本内容不构成投资建议")
            if HAS_RICH:
                # ⏺/✓ workflow indicator — mirrors LLM tool call display style
                _t_icon = _tools[0] if _tools else "local"
                console.print(f"\n  [#C08050]⏺[/#C08050]  [bold]{_t_icon}[/bold]")
                if deterministic.get("compressed_repeat"):
                    _done_label = "unchanged" if _det_en else "未变化"
                elif _tools and _tools[0] == "strategy_advice":
                    _done_label = "ready" if _det_en else "已生成"
                else:
                    _done_label = "fetched" if _det_en else "数据已获取"
                console.print(f"  [green]✓[/green]  [dim]{_tool_label} {_done_label}[/dim]")
                console.print()
                console.print(make_markdown(_strip_latex(final_text)))
                console.print(f"\n[dim]{_tool_label}{_disclaimer}[/dim]{_rl_note}\n")
            else:
                print("\nAria\n")
                print(final_text)
                print(f"\n{_tool_label}{_disclaimer}\n")
            self.conversation.append({"role": "assistant", "content": final_text})
            self._last_response = final_text
            if not _det_wants_analysis:
                return
            # Analysis query: fall through to LLM for deep commentary on the snapshot data

        model = self.config.get("model", "qwen2.5:7b")
        thinking_mode = self.config.get("thinking_mode", "auto")
        auth_token = self.config.get("auth_token")
        user_context = _build_user_context(self.config)
        self.cancel_event = asyncio.Event()
        self._streaming = True
        set_robot_state(RobotState.THINKING)
        _esc_watcher.start(self.cancel_event)

        # Context pressure warning — only once per session when > 85% full
        _est_tokens = sum(len(m.get("content", "")) for m in self.conversation) // 3
        _max_ctx    = get_model_cfg(self.config.get("model", "qwen2.5:7b")).get("num_ctx", 16384)
        from ui.render.output import print_context_warning as _pcw
        _pcw(_est_tokens, _max_ctx, console=console, has_rich=HAS_RICH,
             session_id=getattr(self, "session_id", ""))

        if HAS_RICH:
            console.print()
        start_time = time.time()
        self._begin_runtime_run(message)

        # --- Dynamic max_rounds: scale with task complexity ---
        # Treat this as a soft budget. If the model is still making concrete
        # tool progress at the soft limit, keep going so one user instruction
        # can finish end-to-end instead of stopping after a tool result.
        # (decision logic: apps/cli/turn_planning.py, unit-tested in isolation)
        _task_complexity_signals = is_complex_task(message)
        max_rounds, hard_max_rounds = round_budget_for(_task_complexity_signals)

        # --- Task decomposition for complex multi-step requests ---
        # For long or multi-step messages, ask the AI to produce a plan first,
        # then inject it as context so the agentic loop follows a clear path.
        _decomp_plan: str = ""
        if should_decompose(message, _task_complexity_signals):
            self._transition_runtime_run(
                RunStatus.PLANNING,
                reason="complex_request_decomposition",
            )
            _decomp_prompt = (
                "Break the following user request into a numbered step-by-step execution plan "
                "(max 8 steps, one line each). Be concrete and tool-aware. "
                "Output ONLY the numbered list, nothing else.\n\n"
                f"Request: {message[:600]}"
            )
            try:
                _plan_result = await stream_provider_result(
                    OllamaProvider(
                        self.config.get("ollama_url", "http://localhost:11434"),
                        self.config.get("model", "qwen2.5:7b"),
                        show_market_prefetch_status=False,
                    ),
                    _decomp_prompt,
                    [],
                    tools=[],
                )
                if _plan_result.get("success") and _plan_result.get("response"):
                    _decomp_plan = _plan_result["response"].strip()
            except Exception:
                pass  # decomposition is best-effort

        self._transition_runtime_run(
            RunStatus.RUNNING,
            reason="agent_loop_started",
        )

        # Inject plan as a prefix to the first turn's message so the AI
        # follows the decomposed steps rather than free-forming the approach.
        # (assembly decisions: apps/cli/prompt_assembly.py, unit-tested)
        current_message = build_base_message(
            message,
            wants_analysis_commentary=_det_wants_analysis,
            decomposition_plan=_decomp_plan,
            snapshot=deterministic.get("response", "") if _det_wants_analysis else "",
            lang=self.config.get("ui_lang", "en") or "en",
        )

        # Referenced paths stay as pointers. The model must use audited file
        # tools, preserving permissions, tool traces, and context efficiency.
        if should_prepend_file_tool_hint(_det_wants_analysis, reference_context):
            _file_tool_hint = _build_file_tool_hint(message)
            if _file_tool_hint:
                current_message = _file_tool_hint + current_message

        # ── ML 预测信号注入：聊天中自动检测标的并注入 5 日预测参考 ──────────
        # 仅在 LLM 路径触发（已过确定性路由），且消息含分析意图时启用。
        # 信号注入 current_message（不污染 conversation history），3s 超时。
        _ml_signal_syms: list = []
        try:
            if _is_stock_analysis_intent(message) and not _model_has_tools:
                _ml_signal_syms = _extract_market_symbols(message, limit=3)
                if _ml_signal_syms:
                    _ml_sig = _fetch_quick_ml_signal(_ml_signal_syms)
                    current_message = with_ml_signal_prefix(current_message, _ml_sig)
        except Exception:
            _ml_signal_syms = []

        turn_state = AgentTurnState(provider="aws")
        provider = turn_state.provider
        token_count = 0
        thinking_tokens = 0
        elapsed = 0.0

        try:
            from apps.cli.todo_tracker import clear_todos as _clear_todos
            _clear_todos()  # reset task checklist for this new turn
        except Exception:
            pass

        _response_header_printed = False

        def _print_response_header() -> None:
            nonlocal _response_header_printed
            if _response_header_printed:
                return
            _response_header_printed = True
            _answer_model = self._actual_model or self.config.get("model", "")
            _answer_meta = f"  [dim]· {_answer_model}[/dim]" if _answer_model else ""
            _agent_name = getattr(self, "_active_agent_name", "Aria")
            _agent_color = getattr(self, "_active_agent_color", "blue")
            if _agent_name == "Aria":
                _agent_color = "bold"
            if HAS_RICH:
                console.print(f"[{_agent_color}]{_agent_name}[/{_agent_color}]{_answer_meta}")
            else:
                print(f"{_agent_name}{' · ' + _answer_model if _answer_model else ''}")
        # A task about this folder on a model that cannot open it gets a guess
        # that reads like a result (backend_chat answered "5" for a function
        # that returns -1). Stop this task before generating a guessed result.
        if not _model_has_tools:
            from apps.cli.workspace_route import needs_workspace, no_tools_message
            if needs_workspace(message):
                self._no_tools_notice_shown = True
                _zh_nt = str(self.config.get("ui_lang", "en")).lower().startswith("zh")
                _why = no_tools_message(self.config, lang="zh" if _zh_nt else "en")
                if HAS_RICH:
                    from aria_code.ui.render.output import print_hanging
                    print_hanging(console, "  ! ", _why, style="yellow")
                else:
                    print(f"  ! {_why}")
                return
        # ── Single-shot turn through the shared runtime Gateway ─────────────
        # The per-round inline agent loop that used to live here was removed
        # (2026-07) after the runtime path was validated with real turns:
        # plain chat, inline-parity, and native tool-calling all served by
        # gateway.run_turn → run_agent, which owns rounds, tool execution,
        # loop-guard and (now) per-tool approval. This block only adapts the
        # terminal — stream consumer, approval UI, run-store transitions —
        # to that loop and renders its outcome.
        from apps.cli.providers.runtime_bridge import run_chat_via_runtime

        response_text = ""
        stream_consumer = TerminalRuntimeEventConsumer(
            terminal=self,
            console=console,
            has_rich=HAS_RICH,
            markdown_cls=make_markdown,
            live_cls=Live,
            strip_latex=_strip_latex,
            set_robot_state=set_robot_state,
            streaming_state=RobotState.STREAMING,
            print_tool_call=_print_tool_call,
            print_tool_done=_print_tool_done,
            fallback_from=self._last_provider or "local",
            ui_lang=self.config.get("ui_lang", "en") or "en",
            on_response_start=_print_response_header,
        )
        _start_spinner = stream_consumer.start_spinner
        _stop_spinner = stream_consumer.stop_spinner
        _stop_live = stream_consumer.stop_live
        _flush_latex_buf = stream_consumer.flush_latex_buf
        _first_token_received = stream_consumer.first_token_received_ref
        _use_plain_print = stream_consumer.use_plain_print_ref
        _use_batch_render = stream_consumer.use_batch_render_ref
        _latex_buf = stream_consumer.latex_buf_ref
        _in_latex = stream_consumer.in_latex_ref

        _start_spinner()

        on_token = stream_consumer.on_token
        on_thinking = stream_consumer.on_thinking
        on_tool_call = stream_consumer.on_tool_call
        on_tool_result = stream_consumer.on_tool_result
        on_status = stream_consumer.on_status

        # Interactive tool approval, threaded through the gateway into
        # run_agent's tool loop (previously an inline-loop exclusive — the
        # runtime path used to execute _CONFIRM_TOOLS without prompting).
        approval_consumer = TerminalApprovalEventConsumer(
            terminal=self,
            console=console,
            has_rich=HAS_RICH,
            confirm_decision=_confirm_tool_execution_decision,
            apply_decision=_apply_tool_approval,
            save_config=save_config,
        )

        async def _approval_callback(tool_name: str, tool_params: dict) -> ApprovalDecision:
            self._transition_runtime_run(
                RunStatus.WAITING_APPROVAL,
                reason="tool_requires_approval",
                data={"tool": tool_name},
            )
            try:
                return await approval_consumer.approve(
                    tool_name,
                    tool_params,
                    stop_before_prompt=_stop_live,
                )
            finally:
                self._transition_runtime_run(
                    RunStatus.RUNNING,
                    reason="tool_approval_resolved",
                    data={"tool": tool_name},
                )

        def _approval_applier(tool_params: dict, approval: ApprovalDecision) -> dict:
            return approval_consumer.apply(tool_params, approval)

        # Parity with the old inline local_mode rendering: Ollama generates
        # with no live display — accumulate silently, Rich-render at the end.
        if self.config.get("local_mode", False):
            _use_plain_print[0] = True
            _use_batch_render[0] = True

        # Capture the pending system-role override WITHOUT consuming it yet:
        # only a confirmed-successful turn clears it.
        _rt_sys_ov = getattr(self, "_system_override", None)
        _rt_turn = None
        from packages.aria_services.research_protocol import (
            grounding_tool_names,
            requires_financial_evidence,
        )
        _requires_financial_evidence = requires_financial_evidence(message)
        # 空响应自动重试:云端模型偶发空补全时,同 provider 重放本轮一次,
        # 而不是把"请重试"推给用户(60s 的工具结果/思考不该因一次抽风作废)。
        # 仅对 empty_response 重试;其他错误(配额/鉴权等)走原有 rescue 链。
        for _rt_attempt in range(2):
            try:
                _rt_turn = await run_chat_via_runtime(
                    prompt=current_message, history=self.conversation[:-1],
                    local_tools=LOCAL_TOOLS, tool_schemas=LOCAL_TOOL_SCHEMAS,
                    model=model, config=_runtime_config, api_url=self.api_url,
                    ollama_url=self.config.get("ollama_url", "http://localhost:11434"),
                    cancel_event=self.cancel_event,
                    on_token=on_token, on_thinking=on_thinking,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result, on_status=on_status,
                    thinking_mode=thinking_mode, user_context=user_context,
                    auth_token=auth_token, project_context=_PROJECT_CONTEXT,
                    system_override=_rt_sys_ov,
                    max_rounds=hard_max_rounds,
                    confirm_tools=_CONFIRM_TOOLS,
                    approval_callback=_approval_callback,
                    approval_applier=_approval_applier,
                    requires_evidence=_requires_financial_evidence,
                    grounding_tools=grounding_tool_names(LOCAL_TOOL_SCHEMAS),
                    evidence_already_grounded=bool(
                        evidence_grounded
                        or (_det_wants_analysis and deterministic.get("success"))
                    ),
                    execution_context=lambda: {
                        "_run_id": self._active_run_id,
                        "_session_id": self.session_id,
                    },
                    return_result=True,
                )
            except Exception as _rt_err:
                logger.error("Runtime turn failed: %s", _rt_err)
                _rt_turn = None

            _rt_probe_text = getattr(_rt_turn, "text", "") if _rt_turn is not None else ""
            _rt_probe_cancelled = bool(getattr(_rt_turn, "cancelled", False)) or bool(
                self.cancel_event is not None and self.cancel_event.is_set()
            )
            if _rt_probe_cancelled or (_rt_probe_text or "").strip():
                break
            _rt_probe_err = (
                getattr(_rt_turn, "error", None) if _rt_turn is not None else None
            ) or "empty_response"
            if _rt_attempt == 0 and "empty_response" in str(_rt_probe_err) and "ARIA-4223" not in str(_rt_probe_err):
                if HAS_RICH:
                    console.print("  [dim yellow]⚠ Empty response from model; auto-retrying once...[/dim yellow]")
                else:
                    print("  ⚠ Empty response from model; auto-retrying once...")
                continue
            break

        response_text = stream_consumer.response_text
        token_count = stream_consumer.token_count
        thinking_tokens = stream_consumer.thinking_tokens
        _stop_live()

        _rt_text = getattr(_rt_turn, "text", "") if _rt_turn is not None else ""
        _rt_cancelled = bool(getattr(_rt_turn, "cancelled", False)) or bool(
            self.cancel_event is not None and self.cancel_event.is_set()
        )
        if _rt_cancelled:
            result = {"success": False, "response": response_text, "cancelled": True}
            turn_state.append_response(response_text)
        elif (_rt_text or "").strip():
            self._system_override = None  # consumed by the successful turn
            _rt_final = getattr(_rt_turn, "final", None)
            _rt_metadata = getattr(_rt_final, "metadata", None)
            _rt_provider = (
                getattr(_rt_final, "provider", "")
                or str(self.config.get("local_provider") or "ollama")
            )
            result = {
                "success": bool(getattr(_rt_turn, "ok", False)),
                "response": _rt_text,
                "error": getattr(_rt_turn, "error", None) or getattr(_rt_final, "error", ""),
                "stop_reason": getattr(_rt_final, "stop_reason", "completed"),
                "acceptance": getattr(_rt_final, "acceptance", None),
                "provider": _rt_provider,
                "cancelled": False,
                "usage": {
                    "prompt_tokens": getattr(_rt_metadata, "prompt_tokens", 0),
                    "completion_tokens": getattr(_rt_metadata, "completion_tokens", 0),
                    "thinking_tokens": getattr(_rt_metadata, "thinking_tokens", 0),
                },
                "tools_used": list(getattr(_rt_final, "tools", []) or []),
                "sources": list(getattr(_rt_final, "sources", []) or []),
            }
            # 状态条 ctx% 的真实数据源:provider 上报的本轮上下文占用
            # (prompt 含系统提示/skills,字符估算看不见这部分)。
            try:
                self._last_prompt_tokens = int(
                    (getattr(_rt_metadata, "prompt_tokens", 0) or 0)
                    + (getattr(_rt_metadata, "completion_tokens", 0) or 0)
                )
                self._last_prompt_tokens_msgs = len(self.conversation)
            except Exception:
                pass
            response_text = stream_consumer.response_text or _rt_text
            turn_state.provider = _rt_provider
            turn_state.apply_model_result(result, response_text)
            provider = turn_state.provider
            self._last_provider = _rt_provider
        else:
            # Runtime turn failed or produced nothing. Keep the old inline
            # chain's most valuable recovery in compact form: one direct
            # cloud-API rescue (providers/llm/registry fallback chain),
            # honoring the provider_fallback config.
            _err = (getattr(_rt_turn, "error", None) if _rt_turn is not None else None) or "empty_response"
            result = {"success": False, "error": str(_err), "response": "", "cancelled": False}
            _fallback_mode = str(self.config.get("provider_fallback", "configured")).lower()
            _rescue = None
            if _fallback_mode != "off" and "ARIA-4223" not in str(_err):
                try:
                    from providers.llm.registry import stream_cloud_fallback
                    _rescue = await stream_cloud_fallback(
                        current_message, self.conversation,
                        on_token=on_token,
                        cancel_event=self.cancel_event,
                        include_defaults=_fallback_mode == "auto",
                    )
                except Exception as _rescue_err:
                    logger.debug(
                        "Cloud rescue after runtime failure did not complete: %s",
                        _rescue_err,
                    )
                    _rescue = None
            if _rescue is not None and _rescue.get("cancelled"):
                response_text = stream_consumer.response_text
                result = {"success": False, "response": response_text, "cancelled": True}
                turn_state.append_response(response_text)
            elif (
                _rescue is not None
                and _rescue.get("success")
                and (_rescue.get("response") or "").strip()
            ):
                self._system_override = None
                result = _rescue
                response_text = stream_consumer.response_text or result.get("response", "")
                token_count = stream_consumer.token_count
                thinking_tokens = stream_consumer.thinking_tokens
                turn_state.provider = result.get("provider", "cloud")
                turn_state.apply_model_result(result, response_text)
                provider = turn_state.provider
                self._last_provider = provider
            else:
                stream_consumer.finish(TurnPhase.ERROR)
                set_robot_state(RobotState.ERROR)
                turn_result = turn_state.build_error_result(
                    result.get("error"),
                    elapsed=elapsed,
                    fallback_response=response_text,
                    token_count=token_count,
                    thinking_tokens=thinking_tokens,
                )
                self._last_turn_envelope = turn_result.to_envelope()
                _turn_envelope = self._last_turn_envelope.to_dict()
                if self.runtime_trace is not None:
                    try:
                        self.runtime_trace.add_turn_result(_turn_envelope)
                    except Exception:
                        pass
                error_presentation = AgentErrorPresentation.from_error(
                    result.get("error", "Unknown error"),
                    lang=self.config.get("ui_lang", "en") or "en",
                )
                _offline_answer = (
                    _data_without_model(message, self.conversation[:-1])
                    if _model_unavailable(result.get("error")) else {}
                )
                if _offline_answer:
                    # The data is the answer; the model problem is a note under it.
                    _en_note = _detect_lang(message) == "en"
                    _answer = _offline_answer["response"]
                    if HAS_RICH:
                        console.print()
                        console.print(make_markdown(_strip_latex(_answer)))   # its header carries the disclaimer
                    else:
                        print("\n" + _answer)
                    self.conversation.append({"role": "assistant", "content": _answer})
                    self._last_response = _answer
                    error_presentation = AgentErrorPresentation(
                        error=error_presentation.error,
                        level="warning",
                        lines=[
                            ("模型不可用，以上为数据快照（未经模型分析）。" if not _en_note else
                             "The model is unavailable, so this is the data snapshot without model analysis."),
                            *error_presentation.lines[-1:],
                        ],
                    )
                console.print() if HAS_RICH else print()
                if error_presentation.use_generic_error_prefix:
                    _print_error(error_presentation.lines[0])
                else:
                    _tone = "red" if error_presentation.level == "error" else "yellow"
                    for idx, ln in enumerate(error_presentation.lines):
                        if HAS_RICH:
                            from aria_code.ui.render.output import print_hanging
                            style = f"bold {_tone}" if idx == 0 and len(error_presentation.lines) > 1 else _tone
                            print_hanging(console, "  ", ln, style)
                        else:
                            print(f"  {ln}")
                console.print() if HAS_RICH else print()

        # --- Turn finished (runtime loop done) — render + record the outcome ---
        _esc_watcher.stop()
        self._streaming = False
        if result.get("cancelled"):
            set_robot_state(RobotState.IDLE)
        elif result.get("success"):
            set_robot_state(RobotState.DONE)
        else:
            set_robot_state(RobotState.ERROR)
        elapsed = time.time() - start_time

        # ── Unified cancellation path ──────────────────────────────────────────
        # All cancel sources (model cancel, ESC between tools, KeyboardInterrupt)
        # converge here via result["cancelled"]=True.  A single AgentTurnResult
        # carries partial text and timing so callers see a consistent shape.
        if result.get("cancelled"):
            stream_consumer.finish(TurnPhase.CANCELLED)
            _stop_live()
            turn_result = turn_state.build_cancelled_result(
                elapsed=elapsed,
                token_count=token_count,
                thinking_tokens=thinking_tokens,
            )
            # Repetition protection is surfaced by providers as a cancelled
            # turn.  Sanitize the partial answer before rendering, persisting or
            # feeding it back into the next context; otherwise the internal
            # marker and an unfinished Markdown table leak into the conversation.
            if stream_consumer.repetition_stopped or "*[model stopped — repetition detected]*" in (turn_result.final_text or ""):
                from dataclasses import replace as _replace_turn_result

                turn_result = _replace_turn_result(
                    turn_result,
                    final_text=_recover_repetition_stopped_text(turn_result.final_text),
                )
            self._last_turn_envelope = turn_result.to_envelope()
            _turn_envelope = self._last_turn_envelope.to_dict()
            if self.runtime_trace is not None:
                try:
                    self.runtime_trace.add_turn_result(_turn_envelope)
                except Exception:
                    pass
            self._transition_runtime_run(
                RunStatus.CANCELLED,
                reason="user_or_provider_cancelled",
                provider=turn_result.provider,
                data={"elapsed_seconds": elapsed},
            )
            if HAS_RICH:
                # Batch-render mode (Ollama): tokens were silently accumulated.
                # Render whatever was generated before the cancel so the user
                # can see partial output rather than a blank screen.
                if turn_result.final_text and _use_batch_render[0]:
                    _stop_spinner()
                    console.print(make_markdown(_strip_latex(turn_result.final_text)))
                _cancel_text = "已停止重复输出" if stream_consumer.repetition_stopped else "Cancelled"
                console.print(f"\n[dim]{_cancel_text}[/dim]")
                console.print(Rule(style="dim"))
            else:
                if turn_result.final_text:
                    print(turn_result.final_text)
                print("\n  (cancelled)")
            if turn_result.final_text:
                self.conversation.append(
                    {"role": "assistant", "content": turn_result.final_text}
                )
            _run_event_hook("response_done", {
                "ARIA_RESPONSE":  (turn_result.final_text or "")[:500],
                "ARIA_PROVIDER":  turn_result.provider,
                "ARIA_TOKENS":    str((token_count or 0)),
                "ARIA_SESSION":   self.session_id,
                "ARIA_TURN_STATUS": _turn_envelope.get("status", ""),
                "ARIA_TURN_SUMMARY": _turn_envelope.get("summary", "")[:500],
            })
            if _HAS_JSON_HOOKS:
                try:
                    _fire_json_hook(
                        "ResponseDone",
                        response=(turn_result.final_text or "")[:500],
                        session_id=self.session_id,
                        turn=_turn_envelope,
                        hooks=_JSON_HOOKS,
                    )
                except Exception:
                    pass
            if self.config.get("auto_save_sessions") and self._jsonl_store is not None:
                try:
                    if turn_result.final_text:
                        self._jsonl_store.append_message(self.session_id, "assistant", turn_result.final_text)
                    self._jsonl_store.flush_meta(
                        self.session_id,
                        extra={
                            "last_turn_status": self._last_turn_envelope.status,
                            "last_turn_provider": self._last_turn_envelope.provider,
                            "last_turn_summary": self._last_turn_envelope.summary,
                        },
                    )
                except Exception:
                    pass
            return

        if result.get("success") and not result.get("cancelled"):
            turn_result = turn_state.build_result(
                elapsed=elapsed,
                fallback_response=result.get("response", ""),
                token_count=token_count,
                thinking_tokens=thinking_tokens,
            )
            self._last_turn_envelope = turn_result.to_envelope()
            _turn_envelope = self._last_turn_envelope.to_dict()
            final_text = turn_result.final_text

            if not (final_text or "").strip():
                stream_consumer.finish(TurnPhase.ERROR)
                set_robot_state(RobotState.ERROR)
                empty_result = turn_state.build_error_result(
                    "empty_response",
                    elapsed=elapsed,
                    token_count=token_count,
                    thinking_tokens=thinking_tokens,
                )
                self._last_turn_envelope = empty_result.to_envelope()
                if self.runtime_trace is not None:
                    try:
                        self.runtime_trace.add_turn_result(self._last_turn_envelope.to_dict())
                    except Exception:
                        pass
                self._transition_runtime_run(
                    RunStatus.FAILED,
                    reason="response_validation_failed",
                    error="empty_response",
                    provider=empty_result.provider,
                    data={"check": "non_empty_response"},
                )
                presentation = AgentErrorPresentation.from_error(
                    "empty_response",
                    lang=self.config.get("ui_lang", "en") or "en",
                )
                _tone = "red" if presentation.level == "error" else "yellow"
                for idx, line in enumerate(presentation.lines):
                    if HAS_RICH:
                        from aria_code.ui.render.output import print_hanging
                        style = f"bold {_tone}" if idx == 0 else _tone
                        print_hanging(console, "  ", line, style)
                    else:
                        print(f"  {line}")
                return

            if self.runtime_trace is not None:
                try:
                    self.runtime_trace.add_turn_result(_turn_envelope)
                except Exception:
                    pass

            self._transition_runtime_run(
                RunStatus.VERIFYING,
                reason="model_turn_completed",
                provider=turn_result.provider,
                data={
                    "checks": ["provider_success", "non_empty_response"],
                    "tools_used": turn_state.unique_tools(),
                },
            )
            self.runtime_trace.emit("verification_completed", {
                "passed": True,
                "checks": ["provider_success", "non_empty_response"],
            })

            # Flush any unclosed LaTeX buffer (e.g. stream cut off mid-formula).
            # This only matters for the non-batch plain-print path; in batch-render
            # mode the full raw response is rendered below anyway.
            if _in_latex[0] and _latex_buf[0]:
                _leftover = _flush_latex_buf()
                final_text = (final_text or "") + _leftover
                if _use_plain_print[0] and not _use_batch_render[0]:
                    print(_leftover, end="", flush=True)
            final_text = _recover_repetition_stopped_text(final_text)

            # Stop progressive Live display (final state stays in terminal)
            _stop_live()

            # ── Render final response ──────────────────────────────────────
            if _use_batch_render[0] and final_text:
                # Ollama batch-render: spinner was kept running during generation.
                # Stop it and render the COMPLETE response through Rich Markdown +
                # _strip_latex in one pass.  This correctly handles:
                #   • "$$" split across two single-"$" tokens (tokeniser-dependent)
                #   • All LaTeX spacing commands (\; \, \quad etc.)
                #   • Markdown headings, bold, tables
                _stop_spinner()
                stream_consumer.ensure_response_started()
                _render_answer_block(final_text)
            elif token_count == 0 and final_text:
                # Non-streamed response (e.g. complete() API path): render markdown.
                stream_consumer.ensure_response_started()
                _render_answer_block(final_text)

            stream_consumer.finish(TurnPhase.DONE)

            self.conversation.append({"role": "assistant", "content": final_text})
            import time as _time_ts
            self._last_turn_ts = _time_ts.time()

            # ── 预测反馈记录：为本轮检测到的标的写入 DPO 训练素材 ──────────────
            if _ml_signal_syms and final_text:
                for _sym in _ml_signal_syms:
                    self._record_prediction(_sym, final_text)

            # Metadata line — detailed stats
            metadata = turn_result.metadata
            prompt_t = metadata.prompt_tokens
            completion_t = metadata.completion_tokens
            think_t = metadata.thinking_tokens
            self._last_response = final_text   # for /copy
            _context_compacted_from_usage = False

            _ctx_max = get_model_cfg(self.config.get("model", "qwen2.5:7b")).get("num_ctx", 16384)
            if HAS_RICH:
                from ui.render.output import format_turn_footer as _format_turn_footer
                _footer = _format_turn_footer(
                    metadata,
                    mode=self.config.get("response_footer", "compact"),
                    copy_available=bool(final_text),
                )
                if _footer:
                    console.print(f"\n[dim]{_footer}[/dim]")
            else:
                from ui.render.output import format_turn_footer as _format_turn_footer
                _footer = _format_turn_footer(
                    metadata,
                    mode=self.config.get("response_footer", "compact"),
                    copy_available=bool(final_text),
                )
                if _footer:
                    print(f"\n{_footer}\n")

            # Context pressure: if the real provider prompt is already hot,
            # compact immediately after this turn. This catches cases where the
            # provider count includes large system/tool context that the local
            # char estimate misses.
            if prompt_t > 0 and _ctx_max > 0:
                _ctx_fill_pct = prompt_t / _ctx_max
                try:
                    _compact_threshold = float(self.config.get("auto_compact_threshold", 0.78))
                except Exception:
                    _compact_threshold = 0.78
                _compact_threshold = max(0.50, min(0.95, _compact_threshold))
                if bool(self.config.get("auto_compact_context", True)) and _ctx_fill_pct >= _compact_threshold:
                    _old_pct = int(_ctx_fill_pct * 100)
                    try:
                        await self.commands._smart_compact_async(silent=True)
                    except Exception:
                        try:
                            self.conversation = _compact_messages(
                                self.conversation,
                                model_key=self.config.get("model", "qwen2.5:7b"),
                            )
                        except Exception:
                            if len(self.conversation) > 10:
                                self.conversation = self.conversation[-10:]
                    self._auto_compact_count += 1
                    _context_compacted_from_usage = True
                    if HAS_RICH:
                        console.print(f"  [dim]↩ Auto-compacted context after response ({_old_pct}% full)[/dim]")
                elif _ctx_fill_pct >= 0.85:
                    from ui.render.output import print_context_warning as _print_context_warning
                    _print_context_warning(
                        prompt_t,
                        _ctx_max,
                        console=console,
                        has_rich=HAS_RICH,
                        session_id=self.session_id,
                    )
                elif _ctx_fill_pct >= 0.70 and HAS_RICH:
                    _ctx_color = "#aa8800"
                    console.print(
                        f"[{_ctx_color}]  ⚠ 上下文 {int(_ctx_fill_pct * 100)}% 已用，"
                        f"将按阈值 {int(_compact_threshold * 100)}% 自动压缩。[/{_ctx_color}]"
                    )

            # ── Accumulate session-level usage stats (for /cost) ──────────
            self._session_input_tokens  += prompt_t or 0
            self._session_output_tokens += completion_t or 0
            self._session_thinking_tokens += think_t or 0
            self._session_turns += 1

            # Fire response_done lifecycle hooks (shell + JSON)
            _turn_envelope = self._last_turn_envelope.to_dict() if self._last_turn_envelope else {}
            _run_event_hook("response_done", {
                "ARIA_RESPONSE":  (final_text or "")[:500],
                "ARIA_PROVIDER":  turn_result.provider,
                "ARIA_TOKENS":    str((prompt_t or 0) + (completion_t or 0)),
                "ARIA_SESSION":   self.session_id,
                "ARIA_TURN_STATUS": _turn_envelope.get("status", ""),
                "ARIA_TURN_SUMMARY": _turn_envelope.get("summary", "")[:500],
            })
            if _HAS_JSON_HOOKS:
                try:
                    _fire_json_hook(
                        "ResponseDone",
                        response=(final_text or "")[:500],
                        session_id=self.session_id,
                        turn=_turn_envelope,
                        hooks=_JSON_HOOKS,
                    )
                except Exception:
                    pass

            # Auto-capture user preferences / facts expressed in this turn
            try:
                from memory_manager import auto_capture_from_turn as _acft, MemoryManager as _MM
                _acft(message, final_text or "", _MM())
            except Exception:
                pass

            # Trim conversation history to prevent unbounded growth
            if len(self.conversation) > 40:
                self.conversation = self.conversation[-40:]

            # Auto-warn when context approaches the limit; auto-compact before
            # the prompt is already at the edge and tool traces become noisy.
            # The decision is in apps/cli/turn_planning.py, which is also where
            # the other two compaction paths' rules are documented: this one
            # used to ignore auto_compact_context and auto_compact_threshold
            # entirely, so turning auto-compaction off did not turn it off.
            _est = estimate_context_tokens(self.conversation)
            _max = get_model_cfg(self.config.get("model", "qwen2.5:7b")).get("num_ctx", 16384)
            _ctx = post_turn_context_decision(
                _est, _max,
                auto_compact_enabled=bool(self.config.get("auto_compact_context", True)),
                threshold=self.config.get("auto_compact_threshold", 0.78),
                already_compacted=_context_compacted_from_usage,
            )
            _pct = _ctx["fill_pct"]
            if _ctx["should_compact"]:
                # Auto-compact: silently summarise and truncate
                try:
                    await self.commands._smart_compact_async(silent=True)
                except Exception:
                    # Fallback: hard trim
                    self.conversation = self.conversation[-10:]
                if HAS_RICH:
                    console.print(f"  [dim]↩ Auto-compacted context (was {_pct}% full)[/dim]")
            elif _ctx["should_warn"] and HAS_RICH:
                _color = "yellow" if _pct < 85 else "red"
                console.print(
                    f"  [{_color}]⚠ Context {_pct}% full "
                    f"({_est:,}/{_max:,} tokens) — /compact to free space[/{_color}]"
                )

            # Auto-save session (JSON + JSONL dual write)
            if self.config.get("auto_save_sessions"):
                try:
                    self.session_mgr.save_session(self.session_id, self.conversation)
                except Exception:
                    pass
                # JSONL: append only the two new messages (user + assistant) for crash safety
                if self._jsonl_store is not None:
                    try:
                        self._jsonl_store.append_message(self.session_id, "user", message)
                        if final_text:
                            self._jsonl_store.append_message(self.session_id, "assistant", final_text)
                        if self._last_turn_envelope is not None:
                            self._jsonl_store.flush_meta(
                                self.session_id,
                                extra={
                                    "last_turn_status": self._last_turn_envelope.status,
                                    "last_turn_provider": self._last_turn_envelope.provider,
                                    "last_turn_summary": self._last_turn_envelope.summary,
                                },
                            )
                    except Exception:
                        pass

            # Auto-extract preference signals into global memory
            if self.memory_mgr and final_text:
                try:
                    from memory_manager import extract_preference_signal
                    _sig = extract_preference_signal(message, final_text)
                    if _sig:
                        self.memory_mgr.append("user_profile", _sig, title="User Profile")
                except Exception:
                    pass

            self._transition_runtime_run(
                RunStatus.SUCCEEDED,
                reason="turn_completed",
                provider=turn_result.provider,
                data={
                    "elapsed_seconds": elapsed,
                    "prompt_tokens": prompt_t,
                    "completion_tokens": completion_t,
                    "thinking_tokens": think_t,
                    "tools_used": turn_state.unique_tools(),
                },
            )

        if not result.get("success") and not result.get("cancelled"):
            self._transition_runtime_run(
                RunStatus.FAILED,
                reason="provider_or_agent_error",
                error=str(result.get("error") or "unknown_error"),
                provider=str(result.get("provider") or provider),
                data={"elapsed_seconds": elapsed},
            )


__all__ = ["ChatTurnMixin"]
