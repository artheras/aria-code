"""Shared deterministic routing for Aria agent entrypoints.

This module is intentionally UI-free.  The legacy CLI, future daemon/webhook
entrypoints, and the public SDK can all use the same routing order without
importing the terminal implementation from ``aria_cli.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from aria_code.apps.cli.handlers.broker_handlers import handle_broker_query
from aria_code.apps.cli.handlers.chart_handlers import handle_stock_chart_analysis
from aria_code.apps.cli.handlers.market_handlers import (
    _try_handle_market_overview,
    _try_handle_market_snapshot_analysis,
)
from aria_code.apps.cli.handlers.realty_handlers import handle_realty_query
from aria_code.apps.cli.handlers.strategy_advice import handle_strategy_advice
from aria_code.apps.cli.utils.market_detect import (
    _CN_CITIES,
    _INTL_CITIES,
    _extract_market_symbol,
    _is_broker_intent,
    _is_realty_query,
    _is_stock_chart_analysis_request,
)
from aria_code.packages.aria_services.research_protocol import requires_financial_evidence


BrokerRegistryFactory = Callable[[], Any]


def _pack_handlers(message: str) -> tuple:
    """Deterministic handlers contributed by the packs this message activates.

    Falls back to the legacy finance handlers if the pack layer is unavailable,
    so an import problem degrades to the previous behaviour rather than
    silently dropping domain routing.
    """
    try:
        from aria_code.packs import (
            activate_packs,
            active_handlers,
            load_builtin_packs,
        )

        load_builtin_packs()
        return active_handlers(activate_packs(message))
    except Exception:
        return (handle_strategy_advice, _handle_stock_chart_analysis)


# Names of the handlers that decide for themselves whether a message is theirs
# and answer without fetching anything. They need no pack activation; see the
# call site for why.
#
# Names rather than function objects, resolved at call time: a module-level
# tuple of references captures whatever was imported and then ignores anything
# that replaces the module attribute later, which silently defeats both
# monkeypatching in tests and any runtime substitution.
_SELF_GATED_HANDLER_NAMES = ("handle_strategy_advice",)


def _self_gated_handlers() -> tuple:
    return tuple(globals()[name] for name in _SELF_GATED_HANDLER_NAMES)


def _missing_broker_registry() -> None:
    return None


@dataclass(frozen=True)
class DeterministicRouterConfig:
    """Configuration for deterministic routing outside the model loop."""

    model_has_tools: bool = True
    has_brokers: bool = False
    get_broker_registry: BrokerRegistryFactory | None = None


def _handle_broker_query(message: str, config: DeterministicRouterConfig) -> dict:
    return handle_broker_query(
        message,
        has_brokers=config.has_brokers,
        is_broker_intent=_is_broker_intent,
        get_broker_registry=config.get_broker_registry or _missing_broker_registry,
    )


def _handle_realty_query(message: str) -> dict:
    return handle_realty_query(
        message,
        is_realty_query=_is_realty_query,
        cn_cities=_CN_CITIES,
        intl_cities=_INTL_CITIES,
    )


def _handle_stock_chart_analysis(message: str) -> dict:
    return handle_stock_chart_analysis(
        message,
        is_chart_request=_is_stock_chart_analysis_request,
        extract_symbol=_extract_market_symbol,
    )


def run_deterministic_chain(
    message: str,
    *,
    model_has_tools: bool,
    history: list | None = None,
    has_brokers: bool = False,
    get_broker_registry: BrokerRegistryFactory | None = None,
) -> dict:
    """Run the deterministic routing chain used before LLM fallback.

    Order matters:
    - broker account reads are only used when the model cannot call tools;
    - realty must run before market parsing, so housing questions do not inherit
      a ticker;
    - chart requests run before snapshots;
    - whole-market overview runs before single-symbol snapshot parsing, so
      "分析A股" is treated as the A-share market instead of ticker ``A``.
    """

    from aria_code.apps.cli.conversation_policy import is_capability_question
    if is_capability_question(message):
        return {"success": False}
    config = DeterministicRouterConfig(
        model_has_tools=model_has_tools,
        has_brokers=has_brokers,
        get_broker_registry=get_broker_registry,
    )

    deterministic: dict = {"success": False}
    if not config.model_has_tools:
        deterministic = _handle_broker_query(message, config)

    # Domain handlers run only for packs the message actually activated.  They
    # used to run against every message: a question about this repository was
    # walked through the strategy, realty and stock-chart handlers before the
    # model saw it, which is how a code request came back as a stock quote.
    # Activation is entity-gated, so a message that names no instrument never
    # reaches the finance handlers at all.  See aria_code.packs.
    #
    # Realty was the last handler still running ungated here, and it moved
    # behind the same gate in the realty pack: a message must name a city and
    # a housing term, or name the national market outright. A property-domain
    # word on its own ("物业", "地产") no longer reaches it, because that word
    # also appears in the source of every property-management codebase.
    for handler in _pack_handlers(message):
        if deterministic.get("success"):
            break
        deterministic = handler(message)

    # Self-gated handlers run without a pack activation, because they do not
    # depend on one.
    #
    # The entity gate exists to stop a handler that ANSWERS WITH DATA from
    # firing when no instrument was named — that is how a question about this
    # repository came back as a MongoDB stock quote. handle_strategy_advice
    # answers with static methodology text, names no instrument, and fetches
    # nothing, so it carries none of that risk. Gating it anyway simply broke
    # it: "如果我要写一个美股量化策略，你觉得要从几个角度去写" names no ticker,
    # so no pack activated and the handler was unreachable.
    #
    # Its own gate is a three-way conjunction (strategy term AND advice term
    # AND NOT execution term) that rejects every software-engineering use of
    # 策略/建议 — "帮我写一个缓存策略", "数据库索引策略有什么建议",
    # "给我一些代码风格建议" all fall through untouched.
    for handler in _self_gated_handlers():
        if deterministic.get("success"):
            break
        deterministic = handler(message)

    # Tool-capable models should resolve symbols and fetch market data through
    # the audited tool loop. The deterministic market handlers can refresh a
    # large remote symbol universe, which blocks the REPL before the model ever
    # receives the request. Keep them as the fallback for text-only models.
    if not deterministic.get("success") and not config.model_has_tools:
        deterministic = _try_handle_market_overview(message)

    # The evidence gate refuses a market answer given without data, and a
    # model with no tools cannot fetch any — so whatever the gate will demand
    # data for is fetched here, in any wording ("How's NVDA this week?" as
    # well as "分析苹果股票").
    if not deterministic.get("success") and not config.model_has_tools:
        deterministic = _try_handle_market_snapshot_analysis(
            message, history=history, evidence_required=requires_financial_evidence(message))

    return deterministic


__all__ = [
    "BrokerRegistryFactory",
    "DeterministicRouterConfig",
    "run_deterministic_chain",
]
