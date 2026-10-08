"""Non-interactive Aria: `aria-code -p "…"` and `--watch`.

Moved out of aria_cli.py, where ArtheraTerminal held it alongside the REPL.
The bodies are unchanged and still use aria_cli's names (console, LOCAL_TOOLS,
_run_deterministic_chain, …): aria_cli binds a copy of this class to its own
globals with bind_to() and puts it among ArtheraTerminal's bases. A copy per
binding, because aria_cli loads under two names (aria_cli and
aria_code.aria_cli); rebinding one shared class would leave both terminals
on whichever module loaded last.
"""

from __future__ import annotations

def _headless_approval(config: dict, auto_approve, always_allow):
    """Answer approvals with what the operator granted up front, and nothing more.

    ``auto_approve`` and ``always_allow`` are read at call time from the module
    that is actually running (aria_cli, aria_code.aria_cli or __main__ under
    ``python -m``), which is where --dangerously-skip-permissions and
    --allow-tools set them.
    """
    from aria_code.runtime.approval import ApprovalDecision

    def decide(tool_name: str, params: dict):
        if not (auto_approve() or tool_name in (always_allow() or ())):
            return ApprovalDecision.deny("no one is here to approve it; pass --allow-tools or "
                                         "--dangerously-skip-permissions")
        if tool_name == "run_command":
            return ApprovalDecision.allow(policy=config.get("command_policy", "safe"), user_approved=True)
        return ApprovalDecision.allow()

    return decide


def _apply_approval_decision(params: dict, decision) -> dict:
    from aria_code.runtime.approval import apply_approval_decision

    return apply_approval_decision(params, decision)


class HeadlessMixin:
    """run_prompt, _run_prompt_turn, _finish_prompt and run_watch for ArtheraTerminal."""

    async def run_prompt(self, prompt: str, json_output: bool = False,
                         fmt: str = "table", output_file: str = None, quiet: bool = False,
                         machine_out=None):
        """Run a single prompt (non-interactive / pipe mode).

        ``machine_out`` is the real stdout when the output is JSON (sys.stdout
        then points at stderr). With fmt "jsonl" each step is written there as
        one JSON event per line as it happens.
        """
        from aria_code.apps.cli.exec_events import ExecEvents, json_safe
        events = ExecEvents(machine_out if fmt == "jsonl" else None)
        events.emit("turn.started", prompt=prompt, model=self.config.get("model", ""))
        result = await self._run_prompt_turn(prompt, quiet=quiet, events=events)
        self._finish_prompt(result, json_output=json_output, fmt=fmt, output_file=output_file,
                            quiet=quiet, machine_out=machine_out, events=events)

    async def _run_prompt_turn(self, prompt: str, *, quiet: bool, events) -> dict:
        """The work of run_prompt: a slash command, a deterministic answer, or an agent turn."""
        model = self.config.get("model", "qwen2.5:7b")
        thinking_mode = self.config.get("thinking_mode", "auto")
        auth_token = self.config.get("auth_token")
        user_context = _build_user_context(self.config)

        local_mode = self.config.get("local_mode", False)

        # Dispatch slash commands in -p mode (same as interactive REPL loop).
        # Without this, /memory /note /init /review are sent to the LLM as plain text.
        _stripped_prompt = prompt.strip()
        if self.commands.is_command(_stripped_prompt):
            self._maybe_show_intent_preflight(_stripped_prompt, quiet=quiet)
            await self.commands.execute(_stripped_prompt)
            return {"success": True, "command": _stripped_prompt.split()[0], "response": ""}

        _reference_context = ""
        _reference_service = getattr(self, "_reference_service", None)
        if _reference_service is not None and "@" in prompt:
            _prepared_references = _reference_service.prepare(prompt)
            if _prepared_references.errors:
                self._print_reference_errors(_prepared_references)
                return {"success": False, "response": "", "error": "unresolved @ reference"}
            if _prepared_references.references:
                self._print_reference_summary(_prepared_references)
                prompt = _prepared_references.expanded_text
                _reference_context = _prepared_references.context_block

        # Keep referenced local files as pointers; the model reads them through
        # audited tools instead of silently embedding their contents.
        if _reference_context:
            prompt = f"{prompt}\n\n{_reference_context}"
        else:
            _file_tool_hint = _build_file_tool_hint(prompt)
            if _file_tool_hint:
                prompt = _file_tool_hint + prompt
        self._maybe_show_intent_preflight(prompt, quiet=quiet)

        _curr_model_id_p = self.config.get("model", "")
        # The route counts as much as the model: on backend_chat the model gets
        # no local tools whatever it could do with them. This only looked at the
        # model, so the deterministic chain skipped its data pre-fetch there.
        from aria_code.apps.cli.workspace_route import no_tools_message, needs_workspace, route_has_tools, workspace_config
        _runtime_config = workspace_config(prompt, _curr_model_id_p, self.config, self.api_url)
        _model_has_tools_p = route_has_tools(_curr_model_id_p, _runtime_config, self.api_url)

        # ── Broker guide intent: broad discovery should not start an add wizard ──
        if _is_broker_guide_intent(prompt):
            if HAS_RICH:
                console.print("\n[bold]Aria[/bold]  [dim]  正在打开券商与服务指南…[/dim]\n")
            await self.commands.cmd_broker("guide")
            await self.commands.cmd_broker("services")
            await self.commands.cmd_packages("services")
            return {"success": True, "command": "/broker guide", "response": ""}

        # ── Broker setup intent: intercept before LLM / deterministic routing ──
        if _is_broker_setup_intent(prompt):
            _btype_p = _detect_broker_type(prompt)
            if HAS_RICH:
                from apps.cli.utils.market_detect import _BROKER_SETUP_NAMES
                _display_p = _BROKER_SETUP_NAMES.get(_btype_p, ("",))[0] if _btype_p else ""
                _label_p = f"  正在启动{_display_p}配置向导…" if _display_p else "  正在启动券商配置向导…"
                console.print(f"\n[bold]Aria[/bold]  [dim]{_label_p}[/dim]\n")
            await self.commands._cmd_broker_add(_btype_p)
            return {"success": True, "command": "/broker add", "response": ""}

        # ── Football prediction intercept → built-in Poisson handler ──────────
        if await self._try_football_nl_intercept(prompt):
            return {"success": True, "command": "football", "response": ""}

        deterministic = _run_deterministic_chain(
            prompt, model_has_tools=_model_has_tools_p)
        if deterministic.get("success") or _is_stock_chart_analysis_request(prompt):
            result = deterministic
        else:
            # No one reads a headless answer before a script acts on it. A task
            # about this folder on a model that cannot open it would come back as
            # a confident guess with exit 0; fail before sending it instead.
            if not _model_has_tools_p and needs_workspace(prompt):
                _zh = str(self.config.get("ui_lang", "en")).lower().startswith("zh")
                _why = no_tools_message(self.config, lang="zh" if _zh else "en")
                print(_why, file=sys.stderr)
                return {"success": False, "response": "", "error": "model_cannot_use_tools", "detail": _why}
            # Spinner for terminal usage: gives visual feedback while the model generates.
            # Only starts when we actually need to call the LLM (not for deterministic responses).
            _prompt_spinner = None
            if HAS_RICH and sys.stdout.isatty():
                try:
                    _prompt_spinner = console.status("", spinner="dots", spinner_style="dim")
                    _prompt_spinner.__enter__()
                except Exception:
                    _prompt_spinner = None
            try:
                # Headless -p runs the SAME loop as the REPL.
                #
                # It used to call stream_provider_result directly: one provider
                # round, tool schemas advertised but nothing executing what came
                # back, and a single best-effort pass over tool_calls_pending
                # afterwards whose results the model never saw. So `-p` was not
                # agentic at all — no rounds, no tool results fed back, no loop
                # guard, no acceptance gate. Every non-interactive user (CI, a
                # pipe, the eval harness) got a chat reply where the REPL would
                # have done the work.
                #
                # run_chat_via_runtime is the documented single entry point and
                # already handles provider selection and cloud→Ollama fallback,
                # which is what the two branches here were open-coding.
                from aria_code.apps.cli.providers.runtime_bridge import run_chat_via_runtime
                # This method runs on aria_cli's names; these two live here.
                from aria_code.apps.cli.headless import _apply_approval_decision, _headless_approval

                _turn = await run_chat_via_runtime(
                    prompt=prompt, history=[],
                    local_tools=LOCAL_TOOLS, tool_schemas=LOCAL_TOOL_SCHEMAS,
                    model=model, config=_runtime_config, api_url=self.api_url,
                    ollama_url=self.config.get("ollama_url", "http://localhost:11434"),
                    thinking_mode=thinking_mode, user_context=user_context,
                    auth_token=auth_token, project_context=_PROJECT_CONTEXT,
                    max_rounds=int(self.config.get("max_rounds", 30) or 30),
                    # No approval UI exists in headless mode; the operator opts in
                    # with --dangerously-skip-permissions or --allow-tools. Those
                    # used to take the tools out of the confirm set, so no
                    # approval ever happened — and an approval is what lets
                    # run_command past the default "safe" policy (the REPL's
                    # "allow" upgrades it to balanced). Every `python3 script.py`
                    # was blocked: the evals' agents wrote scripts they could not
                    # run, then computed by hand or stopped. Now the confirm set
                    # stays, and the pre-approval answers it the way the REPL would.
                    confirm_tools=frozenset(_CONFIRM_TOOLS),
                    approval_callback=_headless_approval(
                        self.config, lambda: _auto_approve_session, lambda: _session_always_allow),
                    approval_applier=_apply_approval_decision,
                    on_tool_call=events.tool_started,
                    on_tool_result=events.tool_completed,
                    return_result=True,
                )
                _tools_used = list(getattr(_turn.final, "tools", []) or [])
                # A missing closing message can count as success only with
                # passing acceptance evidence, never merely because a tool ran.
                _empty_after_work = (
                    _turn.error == "empty_response" and bool(_tools_used)
                    and (getattr(_turn.final, "acceptance", None) or {}).get("verified") is True
                )
                result = {
                    "success": _turn.ok or _empty_after_work,
                    "response": (
                        _turn.text
                        or (f"（模型未给出收尾说明。已执行的工具：{', '.join(_tools_used)}）"
                            if _empty_after_work else "")
                    ),
                    "error": "" if _empty_after_work else (_turn.error or ""),
                    "provider": getattr(_turn.final, "provider", ""),
                    "tools_used": _tools_used,
                    "acceptance": getattr(_turn.final, "acceptance", None),
                    "stop_reason": getattr(_turn.final, "stop_reason", "") or _turn.error or "completed",
                }
            finally:
                if _prompt_spinner is not None:
                    try:
                        _prompt_spinner.__exit__(None, None, None)
                    except Exception:
                        pass

        return result

    def _finish_prompt(self, result: dict, *, json_output: bool, fmt: str, output_file,
                       quiet: bool, machine_out, events) -> None:
        """Print or save run_prompt's result, and exit 1 if it failed."""
        from aria_code.apps.cli.exec_events import json_safe
        result = result or {"success": False, "response": "", "error": "no result"}
        # The agent loop executes tools now, so there is nothing left over to
        # run here — only stream_provider_result ever populated
        # tool_calls_pending, and the deterministic chain never does. Keeping
        # the old pass would risk running a write twice.
        #
        # What is worth surfacing instead is the acceptance verdict: in headless
        # mode nobody watched the run, so a turn that changed files and failed
        # its checks must say so on stderr rather than print a confident summary
        # and exit 0.
        _acceptance = (result or {}).get("acceptance") or {}
        if _acceptance.get("verified") is False:
            result["success"] = False
            result["error"] = result.get("error") or "checks_failed"
            result["stop_reason"] = "checks_failed"
        if _acceptance.get("verified") is False and not quiet:
            _zh = str(self.config.get("ui_lang", "en")).lower().startswith("zh")
            print(f"⚠ {'验收未通过' if _zh else 'Checks failed'}: {_acceptance.get('headline', '')}",
                  file=sys.stderr)

        events.emit("turn.completed", **json_safe(result))
        out = machine_out or sys.stdout
        if fmt == "jsonl":
            content = None              # the events were the output
        elif json_output or fmt == "json":
            content = json.dumps(json_safe(result), ensure_ascii=False, indent=2)
        elif fmt == "csv":
            content = f"role,content\nassistant,\"{result.get('response', '').replace(chr(34), chr(34)+chr(34))}\""
        elif fmt == "md":
            content = f"# Aria Code AI Response\n\n{result.get('response', '')}\n"
        else:
            content = result.get("response", "") if result.get("success") else f"Error: {result.get('error', 'Unknown')}"

        # Output routing
        if content is None or (fmt == "table" and not json_output and result.get("command") and not content):
            pass                        # jsonl, or a command that printed its own output
        elif output_file:
            with open(output_file, "w") as f:
                f.write(content)
            if not quiet:
                console.print(f"[green]Saved to {output_file}[/green]" if HAS_RICH
                              else f"Saved: {output_file}")
        elif not result.get("success") and fmt == "table" and not json_output:
            print(f"Error: {result.get('error', 'Unknown')}", file=sys.stderr)
        # In default (table) format: render with Rich Markdown when output is
        # a terminal.  This gives properly formatted headings, bold, and tables
        # in interactive use.  When piped/redirected, fall back to plain text
        # for scripting compatibility.
        elif HAS_RICH and fmt == "table" and not json_output and sys.stdout.isatty() and result.get("success"):
            console.print(make_markdown(_strip_latex(content)))
        else:
            print(content, file=out, flush=True)
        # A failed turn exits 1 in every format. Only table did; with --json a
        # script got exit 0 and had to read the body to learn it had failed.
        if not result.get("success"):
            sys.exit(1)

    async def run_watch(self, command_fn, interval: int, cmd_args: str):
        """Run a command repeatedly with interval (like Unix watch)."""
        try:
            while True:
                if not self.config.get("_quiet"):
                    os.system("clear" if os.name == "posix" else "cls")
                    ts = datetime.now().strftime("%H:%M:%S")
                    if HAS_RICH:
                        console.print(f"[dim]Every {interval}s | {ts} | Ctrl+C to stop[/dim]\n")
                    else:
                        print(f"Every {interval}s | {ts} | Ctrl+C to stop\n")

                await command_fn(cmd_args)

                await asyncio.sleep(interval)
        except (KeyboardInterrupt, asyncio.CancelledError):
            if HAS_RICH:
                console.print("\n[dim]Watch stopped[/dim]")
            else:
                print("\nStopped")


def bind_to(namespace: dict) -> type:
    """A copy of HeadlessMixin whose methods resolve bare names in ``namespace``."""
    from aria_code.apps.cli.mixin_binding import bind_mixin

    return bind_mixin(HeadlessMixin, namespace)


__all__ = ["HeadlessMixin", "bind_to"]
