"""Neutral agent-turn Gateway — the single entry every adapter calls.

This is the documented runtime convergence point: one tested turn driver behind
a thin set of adapters (interactive CLI, headless ``-p``, daemon, HTTP API). It
is deliberately free of UI/provider specifics — callers supply:

  • ``provider_fn``   — which model/backend a round calls (built by each adapter)
  • ``tool_executor`` — the local tool registry
  • streaming callbacks — rendered however the adapter likes

``run_turn`` drives :func:`runtime.run_agent`, folds its event stream into a
structured :class:`TurnResult`, and forwards callbacks live.

Because it imports only ``runtime`` primitives (no ``apps.cli``, no concrete
providers), it can be reused from another process/repo — e.g. the Arthera
FastAPI backend — so the CLI and the API share ONE agent loop instead of
re-implementing turn management three times.

Streaming note: ``run_agent`` emits tool-call / tool-result / status / complete /
cancelled / error *events*, but streams **tokens and thinking through callbacks**
(it never yields token events). So ``run_turn`` passes ``on_token``/``on_thinking``
straight to ``run_agent`` (single fire, enables live streaming) while consuming
tool/status/lifecycle via the event stream (avoids double-firing those).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional

# Import from the submodule (not the ``runtime`` package) so this module can be
# imported from ``runtime/__init__`` without a circular dependency.
from aria_code.runtime.agent_loop import (
    AgentEventCancelled,
    AgentEventComplete,
    AgentEventError,
    AgentEventStatus,
    AgentEventToolCall,
    AgentEventToolResult,
    AgentOptions,
    run_agent,
)


@dataclass(frozen=True)
class TurnResult:
    """Adapter-agnostic outcome of one agent turn."""

    text: str = ""
    final: Any = None             # AgentTurnResult when the turn completed
    error: Optional[str] = None
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return (
            self.error is None and not self.cancelled and self.final is not None
            and bool(getattr(self.final, "success", False))
            and not getattr(self.final, "cancelled", False)
            and (getattr(self.final, "acceptance", None) or {}).get("verified") is not False
        )


async def run_turn(
    prompt: str,
    history: list,
    *,
    provider_fn: Callable,
    tool_executor,
    tool_schemas: Optional[List[dict]] = None,
    on_token: Optional[Callable[[str], None]] = None,
    on_thinking: Optional[Callable[[str], None]] = None,
    on_tool_call: Optional[Callable[[str, dict], None]] = None,
    on_tool_result: Optional[Callable[[str, dict], None]] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
    cancel_event=None,
    max_rounds: int = 30,
    confirm_tools=(),
    approval_callback: Optional[Callable] = None,
    approval_applier: Optional[Callable] = None,
    requires_evidence: bool = False,
    grounding_tools=(),
    evidence_already_grounded: bool = False,
    acceptance=None,
    contract=None,
    review=None,
) -> TurnResult:
    """Drive one ``run_agent`` turn; return its text + lifecycle as a TurnResult.

    Tokens are forwarded to ``on_token`` as they stream and accumulated here, so
    the returned ``text`` is independent of the result object's field names; it
    falls back to the turn result's authoritative ``final_text`` only if nothing
    streamed (e.g. a non-streaming provider).

    ``acceptance`` takes an :class:`~aria_code.runtime.acceptance.AcceptanceGate`.
    Passing one makes "done" conditional on the inferred checks going green for
    every adapter at once — CLI, headless, daemon and API — which is the point
    of having a single gateway. Omitting it keeps the previous behaviour.

    ``contract`` takes a :class:`~aria_code.runtime.contract.ChangeContract`,
    enforced on every tool call the same way for every adapter.
    ``review`` takes a :class:`~aria_code.runtime.review.ReviewGate`.
    """
    schemas = list(tool_schemas or [])
    acc: List[str] = []
    final = None
    error: Optional[str] = None
    cancelled = False

    def _on_token(tok: str) -> None:
        acc.append(tok)
        if on_token is not None:
            on_token(tok)

    async for ev in run_agent(
        prompt,
        history,
        provider_fn=provider_fn,
        tool_executor=tool_executor,
        options=AgentOptions(
            max_rounds=max_rounds,
            tool_schemas=schemas,
            confirm_tools=frozenset(confirm_tools or ()),
            approval_callback=approval_callback,
            approval_applier=approval_applier,
            requires_evidence=bool(requires_evidence),
            grounding_tools=frozenset(grounding_tools or ()),
            evidence_already_grounded=bool(evidence_already_grounded),
            acceptance=acceptance,
            contract=contract,
            review=review,
        ),
        on_token=_on_token,         # streamed live (run_agent emits no token events)
        on_thinking=on_thinking,    # streamed live (no thinking events either)
        cancel_event=cancel_event,
    ):
        if isinstance(ev, AgentEventToolCall):
            if on_tool_call is not None:
                on_tool_call(ev.tool, dict(ev.params))
        elif isinstance(ev, AgentEventToolResult):
            if on_tool_result is not None:
                result = dict(ev.result)
                result["_elapsed_s"] = float(getattr(ev, "elapsed", 0.0))
                on_tool_result(ev.tool, result)
        elif isinstance(ev, AgentEventStatus):
            if on_status is not None:
                phase = getattr(ev, "phase", "") or getattr(ev, "state", "") or ""
                on_status(phase, ev.message)
        elif isinstance(ev, AgentEventComplete):
            final = ev.result
        elif isinstance(ev, AgentEventCancelled):
            cancelled = True
            if not acc:
                partial = getattr(ev, "partial_text", "") or ""
                if partial:
                    acc.append(partial)
            break
        elif isinstance(ev, AgentEventError):
            error = ev.error
            break

    text = "".join(acc)
    if final is not None and not getattr(final, "success", False):
        error = error or getattr(final, "error", "") or getattr(final, "stop_reason", "incomplete")
    if not text and final is not None:
        text = getattr(final, "final_text", "") or ""
    return TurnResult(text=text, final=final, error=error, cancelled=cancelled)
