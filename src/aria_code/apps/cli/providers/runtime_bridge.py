"""Phase-3 bridge: run one chat turn through the shared ``runtime.run_agent``.

The documented runtime next step is to route the CLI tool loop through
``run_agent`` and keep aria_cli as orchestration glue. This module is that engine
— the two adapters run_agent needs plus a thin driver:

  • build_tool_executor() — wraps the CLI's ``LOCAL_TOOLS`` ({name: (handler, schema)})
  • make_provider_fn()     — selects the provider (chat_routing) + streams it
  • run_chat_via_runtime() — drives run_agent, renders via callbacks, returns text

This is THE chat path: ``send_message`` runs every turn through it (the old
inline per-round loop was retired in 2026-07 after the runtime path was
validated with real turns). Tool approval and execution context are threaded
through to run_agent's tool loop; on failure send_message attempts one direct
cloud rescue (``providers/llm/registry.stream_cloud_fallback``) before
presenting the error.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from .chat_routing import first_round_route, is_placeholder_response, should_fallback


def build_tool_executor(
    local_tools,
    config: Optional[dict] = None,
    execution_context: Optional[Callable[[], dict]] = None,
):
    """Wrap the CLI's LOCAL_TOOLS registry for run_agent."""
    from aria_code.runtime.tool_executor import ToolExecutor
    return ToolExecutor(
        local_tools,
        config=config or {},
        execution_context=execution_context,
    )


# Modes in which nothing can be written, so nothing can need verifying.
_READ_ONLY_MODES = frozenset({"read-only", "readonly", "read_only", "plan"})


def _declared_acceptance_commands(cfg: dict, message: str) -> tuple:
    """What this workspace says "correct" means for this particular message.

    Precedence, most specific first:

      1. ``acceptance_commands`` in the session config — the user said it out
         loud for this session, so nothing overrides it.
      2. The active domain packs' commands. A pack contributes only when it
         resolved a concrete entity from *this* message, so a declared
         logistics check cannot fire on a payments question.
      3. The workspace's ``acceptance.default`` from ``.ariarc``.

    Nothing declared falls through to inference from the changed files, which
    is the behaviour that existed before packs could speak here at all.
    """
    explicit = tuple(cfg.get("acceptance_commands") or ())
    if explicit:
        return explicit

    commands: list = []
    try:
        from aria_code.packs import (
            activate_packs,
            active_acceptance_commands,
            load_builtin_packs,
        )

        load_builtin_packs()
        commands.extend(active_acceptance_commands(activate_packs(message or "")))
    except Exception:
        pass

    try:
        from aria_code.packs.rules import default_acceptance_commands

        for command in default_acceptance_commands():
            if command not in commands:
                commands.append(command)
    except Exception:
        pass

    return tuple(commands)


def _workspace_root(cfg: dict, executor=None) -> str:
    """Where this turn's tools act: the executor's workspace if it names one."""
    import os

    root = cfg.get("_session_workspace_root") or cfg.get("workspace_root") or os.getcwd()
    if getattr(executor, "execution_context", None) is not None:
        root = dict(executor.execution_context() or {}).get("_workspace") or root
    return str(root)


def build_acceptance_gate(executor, config: Optional[dict] = None, message: str = ""):
    """The CLI's acceptance gate, or ``None`` when this session shouldn't have one.

    The gate runs its checks through the session's own ``run_command`` tool
    rather than spawning subprocesses itself, so the workspace sandbox, the
    command policy and the trace all apply to a verification run exactly as
    they apply to a command the model asked for.

    ``user_approved`` is set because the commands are the *planner's*, not the
    model's or the user's prose — the same inferred plan ``/verify`` already
    runs on request. Anything else would put an approval prompt between the
    model finishing and the check that tells us whether it finished correctly,
    which is the one place a prompt cannot help.
    """
    cfg = config or {}
    if not cfg.get("acceptance_gate", True):
        return None
    mode = str(cfg.get("permission_mode", "workspace-write") or "")
    if mode in _READ_ONLY_MODES:
        return None
    if "run_command" not in getattr(executor, "local_tools", {}):
        return None

    from aria_code.runtime.acceptance import AcceptanceGate
    from aria_code.runtime.approval import ApprovalDecision

    timeout = int(cfg.get("acceptance_timeout", 300) or 300)
    root = _workspace_root(cfg, executor)

    def _runner(command: str) -> dict:
        return executor.execute_local("run_command", {
            "command": command,
            "cwd": str(root),
            "timeout": timeout,
        }, approval=ApprovalDecision.allow(policy="balanced", user_approved=True))

    return AcceptanceGate(
        runner=_runner,
        root=root,
        max_attempts=int(cfg.get("acceptance_max_attempts", 2) or 2),
        commands=_declared_acceptance_commands(cfg, message),
    )


def build_change_contract(config: Optional[dict] = None, message: str = "", executor=None):
    """The project's change contract with this request as its goal, or None.

    Declared in ``.aria/policy.yaml`` under the workspace root. A file that
    cannot be read yields a fail-closed contract rather than none — see
    ``ChangeContract.fail_closed``.
    """
    from aria_code.runtime.contract import ChangeContract, ContractError

    root = _workspace_root(config or {}, executor)
    try:
        contract = ChangeContract.load(root)
    except ContractError as exc:
        contract = ChangeContract.fail_closed(root, str(exc))
    except Exception as exc:  # unreadable file, missing yaml, …: still fail closed
        contract = ChangeContract.fail_closed(root, f"{type(exc).__name__}: {exc}")
    return contract.with_goal(message) if contract is not None else None


def build_review_gate(config: Optional[dict] = None, *, model: str, api_url: Optional[str],
                      ollama_url: str, thinking_mode: str = "auto", auth_token: Optional[str] = None):
    """The independent reviewer for this turn, or None unless review_gate is on.

    Same model, fresh context: no history, no tools, no project context — the
    reviewer judges the diff it is given, not the conversation that made it.
    """
    cfg = config or {}
    if not cfg.get("review_gate", False):
        return None
    if str(cfg.get("permission_mode", "workspace-write") or "") in _READ_ONLY_MODES:
        return None
    from aria_code.runtime.review import REVIEWER_SYSTEM, ReviewGate

    reviewer_fn = make_provider_fn(
        model=model, config=cfg, api_url=api_url, ollama_url=ollama_url,
        tool_schemas=[], thinking_mode=thinking_mode, auth_token=auth_token,
        system_override=REVIEWER_SYSTEM,
    )

    async def _review(prompt: str) -> str:
        result = await reviewer_fn(prompt, [])
        if not result.get("success", True):
            raise RuntimeError(result.get("error") or "reviewer call failed")
        return str(result.get("response") or "")

    try:
        attempts = int(cfg.get("review_max_attempts", 1))
    except (TypeError, ValueError):
        attempts = 1
    return ReviewGate(reviewer=_review, max_attempts=max(0, attempts))


async def run_with_fallback(
    route: str,
    *,
    run_cloud: Callable,
    run_ollama: Callable,
    on_token: Optional[Callable[[str], None]] = None,
) -> dict:
    """Primary generation per ``route``, with cloud→Ollama fallback parity.

    Mirrors ``send_message``'s inline fallback, but keyed on *route* (via
    ``should_fallback``) so a genuinely-good forced-backend answer is kept
    instead of being discarded and re-run:

      • ``skip`` / ``ollama`` → run local Ollama directly (no cloud round)
      • ``cloud``            → run cloud; if it fails or returns an empty answer,
                               fall back to
                               local Ollama

    ``run_cloud`` / ``run_ollama`` are async ``(on_token) -> result dict``
    closures; injecting them keeps this orchestration unit-testable without
    real providers or network.
    """
    if route in ("skip", "ollama", "configured"):
        return await run_ollama(on_token)

    # Route == "cloud": track streamed chunks for telemetry only. A response
    # delivered in one final event is still a valid answer.
    _tokens = [0]

    def _counting_on_token(tok: str) -> None:
        _tokens[0] += 1
        if on_token is not None:
            on_token(tok)

    result = await run_cloud(_counting_on_token)
    if result.get("cancelled"):
        return result  # user-cancelled — never silently re-run on a different provider

    placeholder = is_placeholder_response(result.get("response", ""), _tokens[0])
    if should_fallback("cloud", result, is_placeholder=placeholder):
        return await run_ollama(on_token)
    return result


def make_provider_fn(
    *,
    model: str,
    config: dict,
    api_url: Optional[str],
    ollama_url: str,
    tool_schemas: List[dict],
    thinking_mode: str = "auto",
    user_context: Optional[dict] = None,
    auth_token: Optional[str] = None,
    project_context: Any = None,
    system_override: Optional[str] = None,
) -> Callable:
    """Build an async ``provider_fn`` for run_agent.

    Selects the provider per chat_routing (cloud → AriaSSE backend; ollama/skip →
    local Ollama) and streams it through the shared ``stream_provider_result``.
    A pending system-role override is threaded the same way ``send_message`` does
    it: cloud via ``user_context['system_role_override']``, Ollama via the
    provider's ``system_override`` argument.
    """
    # Event identity is part of the streaming contract. Importing the bare
    # compatibility root creates a second LLMToken/LLMDone class, which the
    # SDK's isinstance checks silently discard even when the API answered.
    from aria_code.apps.cli.providers.base import AriaSSEProvider, ConfiguredProvider, OllamaProvider
    from aria_code.packages.aria_sdk.streaming import stream_provider_result

    _cloud_uctx = dict(user_context or {})
    if system_override:
        _cloud_uctx["system_role_override"] = system_override

    def _scoped_tools(prompt: str) -> List[dict]:
        """Tools this message may use: core always, domain only when claimed."""
        try:
            from aria_code.apps.cli.tool_scope import select_tool_schemas

            return select_tool_schemas(tool_schemas, prompt)
        except Exception:
            return list(tool_schemas)

    def _system_for(prompt: str) -> str:
        """The rules this turn runs under, for the non-Ollama providers.

        ollama_stream assembles its own (with prefetched data and sized
        project context); everything else used to get nothing at all, so a
        cloud model was never told the tool discipline. Built per message
        because the right prompt depends on what was asked.
        """
        try:
            from aria_code.apps.cli.prompts.select import build_turn_system_prompt

            return build_turn_system_prompt(
                prompt,
                override=system_override,
                project_context=str(project_context or ""),
            )
        except Exception:
            return system_override or ""

    async def _provider_fn(prompt, history, *, on_token=None, on_thinking=None,
                           on_tool_call=None, on_tool_result=None, on_status=None,
                           cancel_event=None):
        route = first_round_route(model, config, api_url)

        # ollama_stream does its own intent-based selection; the other
        # providers had none, so a coding turn reached Gemini carrying all 74
        # tool schemas including 34 domain tools. Scoped per message because
        # what a turn may call depends on what it asked.
        _scoped = _scoped_tools(prompt)

        async def _stream(provider, _on_token):
            return await stream_provider_result(
                provider, prompt, history, tools=_scoped,
                cancel_event=cancel_event, on_token=_on_token, on_thinking=on_thinking,
                on_tool_call=on_tool_call, on_tool_result=on_tool_result, on_status=on_status,
            )

        async def run_cloud(_on_token):
            return await _stream(
                AriaSSEProvider(
                    api_url, model, thinking_mode=thinking_mode,
                    user_context=_cloud_uctx, auth_token=auth_token,
                    project_context=project_context,
                    use_react_gateway=bool(config.get("arthera_react_gateway")),
                    local_tools=bool(config.get("backend_local_tools")),
                ),
                _on_token,
            )

        async def run_ollama(_on_token):
            if route == "ollama":
                # stream_ollama still borrows 12 names from aria_cli's module
                # globals and only resolves them after that module's import-time
                # rebind, so an out-of-CLI caller has to import it first. Doing
                # it here rather than at the caller's import time means only the
                # route that needs it pays: measured, `import aria_cli` is 1766ms
                # against 63ms for the daemon's own dependencies, and a
                # Vertex/Gemini config routes to "configured", which never
                # touches stream_ollama. The daemon was paying 1.7s to forward an
                # alert it then analysed through the cloud.
                #
                # Idempotent and cached by sys.modules, so the second alert pays
                # nothing. Inside the function, not the module, because importing
                # aria_cli at this module's import time would put the cost back
                # on every consumer.
                import aria_cli  # noqa: F401  (side effect: rebinds stream_ollama)

            selected = (
                OllamaProvider(ollama_url, model, system_override=system_override)
                if route == "ollama" else
                ConfiguredProvider(config, model, system_override=_system_for(prompt))
            )
            return await _stream(
                selected,
                _on_token,
            )

        return await run_with_fallback(
            route, run_cloud=run_cloud, run_ollama=run_ollama, on_token=on_token,
        )

    return _provider_fn


async def run_chat_via_runtime(
    *,
    prompt: str,
    history: list,
    local_tools,
    tool_schemas: List[dict],
    model: str,
    config: dict,
    api_url: Optional[str],
    ollama_url: str,
    cancel_event=None,
    on_token: Optional[Callable[[str], None]] = None,
    on_thinking: Optional[Callable[[str], None]] = None,
    on_tool_call: Optional[Callable[[str, dict], None]] = None,
    on_tool_result: Optional[Callable[[str, dict], None]] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
    thinking_mode: str = "auto",
    user_context: Optional[dict] = None,
    auth_token: Optional[str] = None,
    project_context: Any = None,
    system_override: Optional[str] = None,
    max_rounds: int = 30,
    return_result: bool = False,
    execution_context: Optional[Callable[[], dict]] = None,
    confirm_tools=(),
    approval_callback: Optional[Callable] = None,
    approval_applier: Optional[Callable] = None,
    requires_evidence: bool = False,
    grounding_tools=(),
    evidence_already_grounded: bool = False,
):
    """Run one chat turn through the shared runtime Gateway.

    This is the CLI *adapter* for ``runtime.gateway.run_turn``: it builds the
    CLI's ``provider_fn`` (AriaSSE/Ollama selection + cloud→Ollama fallback) and
    tool executor (the LOCAL_TOOLS registry), then hands them to the neutral
    gateway, which drives ``run_agent`` and streams via the callbacks. By
    default this returns assistant text for compatibility. ``return_result``
    exposes the gateway result so terminal adapters can preserve provider and
    usage metadata during final rendering.
    """
    from aria_code.runtime.gateway import run_turn
    from aria_code.apps.cli.workspace_route import workspace_config

    config = workspace_config(prompt, model, config, api_url)

    provider_fn = make_provider_fn(
        model=model, config=config, api_url=api_url, ollama_url=ollama_url,
        tool_schemas=tool_schemas, thinking_mode=thinking_mode,
        user_context=user_context, auth_token=auth_token, project_context=project_context,
        system_override=system_override,
    )
    executor = build_tool_executor(local_tools, config, execution_context)
    gate = build_acceptance_gate(executor, config, prompt)
    contract = build_change_contract(config, prompt, executor)
    review = build_review_gate(config, model=model, api_url=api_url, ollama_url=ollama_url,
                               thinking_mode=thinking_mode, auth_token=auth_token)

    result = await run_turn(
        prompt, history,
        provider_fn=provider_fn, tool_executor=executor,
        tool_schemas=list(tool_schemas),
        on_token=on_token, on_thinking=on_thinking,
        on_tool_call=on_tool_call, on_tool_result=on_tool_result, on_status=on_status,
        cancel_event=cancel_event, max_rounds=max_rounds,
        confirm_tools=confirm_tools,
        approval_callback=approval_callback,
        approval_applier=approval_applier,
        requires_evidence=requires_evidence,
        grounding_tools=grounding_tools,
        evidence_already_grounded=evidence_already_grounded,
        acceptance=gate,
        contract=contract,
        review=review,
    )
    return result if return_result else result.text
