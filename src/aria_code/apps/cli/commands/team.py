"""Team command parsing and execution helpers."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime
import io
import logging
import sys
from pathlib import Path
from typing import Any, Callable


def _detect_lang(text: str) -> str:
    if not text:
        return "zh"
    zh_chars = sum(1 for c in text if '一' <= c <= '鿿')
    return "zh" if zh_chars / max(len(text), 1) > 0.15 else "en"

logger = logging.getLogger(__name__)

DEFAULT_TEAM_AGENTS = ["macro", "fundamental", "technical", "risk"]
FULL_TEAM_AGENTS = ["macro", "fundamental", "technical", "risk", "news", "catalyst", "sector"]


@dataclass(frozen=True)
class TeamArgs:
    symbols_raw: list[str]
    agent_names: list[str] | None = None
    use_full_team: bool = False
    use_pipeline: bool = False


@dataclass(frozen=True)
class TeamAnalysisResult:
    symbol: str
    team_result: Any
    data_bundle: Any = None
    quality_notes: list[str] | None = None
    quality_assessment: dict[str, Any] | None = None
    captured_noise: str = ""


@dataclass(frozen=True)
class SavedTeamReport:
    path: Path
    metadata_path: Path | None = None


def parse_team_args(args: str) -> TeamArgs:
    parts = args.strip().split()
    agent_names = None
    symbols_raw: list[str] = []
    use_full_team = False
    use_pipeline = False
    idx = 0
    while idx < len(parts):
        part = parts[idx]
        if part == "--agents" and idx + 1 < len(parts):
            agent_names = [agent.strip() for agent in parts[idx + 1].split(",") if agent.strip()]
            idx += 2
        elif part.startswith("--agents="):
            agent_names = [agent.strip() for agent in part.split("=", 1)[1].split(",") if agent.strip()]
            idx += 1
        elif part == "--full":
            use_full_team = True
            idx += 1
        elif part == "--pipeline":
            use_pipeline = True
            idx += 1
        else:
            symbols_raw.append(part)
            idx += 1
    if use_full_team and not agent_names:
        agent_names = list(FULL_TEAM_AGENTS)
    return TeamArgs(symbols_raw=symbols_raw, agent_names=agent_names, use_full_team=use_full_team, use_pipeline=use_pipeline)


def resolve_team_symbols(args: TeamArgs, config: dict[str, Any], limit: int = 3) -> list[str]:
    if not args.symbols_raw or args.symbols_raw[0].lower() == "watchlist":
        return [str(symbol).upper() for symbol in config.get("watchlist", ["AAPL", "MSFT", "NVDA"])[:limit]]
    return [symbol.upper() for symbol in args.symbols_raw[:limit]]


def team_agent_names(args: TeamArgs) -> list[str]:
    return args.agent_names or list(DEFAULT_TEAM_AGENTS)


def compact_market_cap(value: Any, currency: str = "USD") -> str:
    try:
        cap = float(value)
        if cap <= 0:
            return "-"
        if cap >= 1e12:
            return f"{currency} {cap / 1e12:.2f}T"
        if cap >= 1e9:
            return f"{currency} {cap / 1e9:.1f}B"
        if cap >= 1e6:
            return f"{currency} {cap / 1e6:.0f}M"
        return f"{currency} {cap:,.0f}"
    except Exception:
        return "-"


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _as_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _fmt_num(value: Any, digits: int = 2) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:.{digits}f}"


def _fmt_pct(value: Any, digits: int = 2) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:+.{digits}f}%"


def _fmt_compact_number(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 1e12:
        return f"{sign}{number / 1e12:.2f}T"
    if number >= 1e9:
        return f"{sign}{number / 1e9:.2f}B"
    if number >= 1e6:
        return f"{sign}{number / 1e6:.2f}M"
    if number >= 1e3:
        return f"{sign}{number / 1e3:.1f}K"
    return f"{sign}{number:.0f}"


def _dedupe_missing(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value not in (None, "", [], {})))


def team_quote_snapshot(data_bundle: Any) -> dict[str, Any]:
    quote = getattr(data_bundle, "quote", {}) or {}
    fundamentals = getattr(data_bundle, "fundamentals", {}) or {}
    technical = getattr(data_bundle, "technical", {}) or {}
    quality = dict(getattr(data_bundle, "quality", {}) or {})
    snapshot = {
        "symbol": getattr(data_bundle, "symbol", "") or quote.get("symbol"),
        "price": quote.get("price") or quote.get("current_price") or quote.get("regular_market_price"),
        "change_pct": quote.get("change_pct") or quote.get("change_percent") or quote.get("pct_change"),
        "currency": quote.get("currency") or quote.get("currency_symbol") or "USD",
        "change": quote.get("change"),
        "volume": quote.get("volume") or technical.get("volume"),
        "high": quote.get("high"),
        "low": quote.get("low"),
        "open": quote.get("open"),
        "prev_close": quote.get("prev_close"),
        "name": quote.get("name") or fundamentals.get("name"),
        "sector": fundamentals.get("sector"),
        "industry": fundamentals.get("industry"),
        "market_cap": quote.get("market_cap") or quote.get("marketCap") or fundamentals.get("market_cap"),
        "pe_ratio": _first_present(fundamentals.get("pe_ratio"), fundamentals.get("pe_ttm"), quote.get("pe_ttm")),
        "fwd_pe": fundamentals.get("fwd_pe"),
        "eps": _first_present(fundamentals.get("eps"), fundamentals.get("fwd_eps")),
        "roe": fundamentals.get("roe"),
        "revenue_growth": fundamentals.get("revenue_growth"),
        "analyst_target": fundamentals.get("analyst_target"),
        "recommendation": fundamentals.get("recommendation"),
        "beta": fundamentals.get("beta"),
        "rsi": technical.get("rsi"),
        "macd_hist": technical.get("macd_hist"),
        "ma20": technical.get("ma20"),
        "ma60": technical.get("ma60"),
        "support": technical.get("support") or technical.get("supports"),
        "resistance": technical.get("resistance") or technical.get("resistances"),
        "history_bars": technical.get("history_bars"),
        "provider_chain": getattr(data_bundle, "provider_chain", []) or [],
        "missing_fields": getattr(data_bundle, "missing_fields", []) or [],
        "warnings": getattr(data_bundle, "warnings", []) or [],
        "errors": getattr(data_bundle, "errors", []) or [],
        "quality": quality,
        "stale": bool(quality.get("stale", False)),
        "status": getattr(data_bundle, "status", "data_unavailable"),
        "timestamp": getattr(data_bundle, "timestamp", ""),
    }
    display_missing = list(snapshot["missing_fields"])
    if snapshot.get("volume") in (None, "", [], {}):
        display_missing.append("volume")
    if snapshot.get("analyst_target") in (None, "", [], {}):
        display_missing.append("analyst_target")
    if snapshot.get("beta") in (None, "", [], {}):
        display_missing.append("risk_metrics")
    snapshot["missing_fields"] = _dedupe_missing(display_missing)
    if snapshot["missing_fields"] and snapshot["status"] in {"complete", "ok"}:
        snapshot["status"] = "partial"
    quality_missing = _dedupe_missing(list(quality.get("missing_fields") or []) + snapshot["missing_fields"])
    if quality_missing:
        quality["missing_fields"] = quality_missing
        if quality.get("status") in {"complete", "ok"}:
            quality["status"] = "partial"
    snapshot["quality"] = quality
    return snapshot


def build_team_market_context(data_bundle: Any) -> dict[str, Any]:
    """Build the deterministic real-data block passed into synthesis."""
    if not data_bundle:
        return {}
    snapshot = team_quote_snapshot(data_bundle)
    quote = getattr(data_bundle, "quote", {}) or {}
    fundamentals = getattr(data_bundle, "fundamentals", {}) or {}
    technical = getattr(data_bundle, "technical", {}) or {}
    quality = snapshot.get("quality") or {}
    lines = [
        f"data_status={snapshot.get('status') or 'unknown'}",
        f"providers={', '.join(snapshot.get('provider_chain') or []) or 'unknown'}",
        f"missing={', '.join(snapshot.get('missing_fields') or []) or 'none'}",
        f"price={snapshot.get('currency') or 'USD'} {snapshot.get('price')}",
        f"change_pct={snapshot.get('change_pct')}",
        f"volume={snapshot.get('volume')}",
        f"market_cap={compact_market_cap(snapshot.get('market_cap'), snapshot.get('currency') or 'USD')}",
        f"pe={snapshot.get('pe_ratio')}",
        f"forward_pe={snapshot.get('fwd_pe')}",
        f"eps={snapshot.get('eps')}",
        f"roe={snapshot.get('roe')}",
        f"revenue_growth={snapshot.get('revenue_growth')}",
        f"analyst_target={snapshot.get('analyst_target')}",
        f"rsi={snapshot.get('rsi')}",
        f"macd_hist={snapshot.get('macd_hist')}",
        f"ma20={snapshot.get('ma20')}",
        f"ma60={snapshot.get('ma60')}",
        f"stale={bool(snapshot.get('stale'))}",
    ]
    return {
        "quote": quote,
        "fundamentals": fundamentals,
        "technical": technical,
        "data_quality": quality,
        "market_snapshot": snapshot,
        "market_data_block": "\n".join(lines),
    }


def build_team_agent_data(data_bundle: Any) -> dict[str, dict[str, Any]]:
    """Adapt one normalized data bundle to each agent's expected input shape."""
    if not data_bundle:
        return {}
    quote = dict(getattr(data_bundle, "quote", {}) or {})
    technical = dict(getattr(data_bundle, "technical", {}) or {})
    fundamentals = dict(getattr(data_bundle, "fundamentals", {}) or {})
    history = dict(getattr(data_bundle, "history", {}) or {})
    prepared: dict[str, dict[str, Any]] = {}

    if quote and technical:
        prepared["technical"] = {"quote": quote, "history": technical}
    if quote and fundamentals:
        prepared["fundamental"] = {"quote": quote, "fundamentals": fundamentals}

    rows = history.get("data")
    if quote and isinstance(rows, list) and rows:
        try:
            import pandas as pd
            from agents.financial.risk import _compute_risk

            metrics = _compute_risk(pd.DataFrame(rows))
            if metrics:
                prepared["risk"] = {"quote": quote, "risk_metrics": metrics}
        except Exception as exc:
            logger.debug("team prefetched risk calculation failed: %s", exc)
    return prepared


def build_team_terminal_summary(data_bundle: Any, lang: str = "zh") -> str:
    """Return compact Rich markup summary for the /team panel, in the UI language."""
    zh = str(lang).lower().startswith("zh")
    L = (lambda a, b: a) if zh else (lambda a, b: b)
    if not data_bundle:
        return f"[#57606a]{L('数据', 'Data')}:[/#57606a] unavailable"
    snapshot = team_quote_snapshot(data_bundle)
    currency = snapshot.get("currency") or "USD"
    price = snapshot.get("price")
    price_text = f"{currency} {_fmt_num(price)}" if price is not None else "unavailable"
    cap_text = compact_market_cap(snapshot.get("market_cap"), currency)
    change_text = _fmt_pct(snapshot.get("change_pct"))
    volume_text = _fmt_compact_number(snapshot.get("volume"))
    rsi_text = _fmt_num(snapshot.get("rsi"), 1)
    macd_text = _fmt_num(snapshot.get("macd_hist"), 4)
    ma20_text = _fmt_num(snapshot.get("ma20"))
    ma60_text = _fmt_num(snapshot.get("ma60"))
    providers = ", ".join(snapshot.get("provider_chain") or []) or "unknown"
    missing = ", ".join(snapshot.get("missing_fields") or []) or "none"
    status = snapshot.get("status") or "unknown"
    stale = "yes" if snapshot.get("stale") else "no"
    return "\n".join([
        f"[bold]{snapshot.get('name') or snapshot.get('symbol') or ''}[/bold]  "
        f"{L('价格', 'Price')} {price_text} ({change_text}) · {L('市值', 'Mkt cap')} {cap_text} · "
        f"{L('成交量', 'Volume')} {volume_text}",
        f"{L('技术面', 'Technicals')} RSI {rsi_text} · MACD hist {macd_text} · MA20 {ma20_text} · MA60 {ma60_text}",
        f"[#57606a]{L('数据', 'Data')}:[/#57606a] {providers} · status {status} · stale {stale} · missing {missing}",
    ])


def clean_team_synthesis_text(text: str) -> str:
    """Strip raw Markdown markers that render poorly inside terminal panels."""
    import re
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    # Headings too: "## 团队分析汇总" showed with its hashes inside the panel.
    cleaned = re.sub(r"(?m)^\s*#{1,6}\s+", "", cleaned)
    # Quote markers and emoji variation selectors: "> ⚠️ 1 of 4 analysts…"
    # broke the panel — Rich measures "⚠️" one cell short, and the line wrapped
    # into "> ⚠" and a stray "that did.".
    cleaned = re.sub(r"(?m)^\s*>\s?", "", cleaned).replace("\ufe0f", "")
    cleaned = re.sub(r"(?m)^\s*-\s*$", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


class ConfiguredTeamProvider:
    """The model Aria is configured for, behind the agents' stream() interface.

    The team was always handed an Ollama provider at localhost:11434 with the
    configured model's name — "google/gemini-2.5-pro" sent to a local Ollama
    that is not running — so every model-driven analyst failed on a cloud
    setup. This streams through the same provider the chat and /review use.
    """

    def __init__(self, config: dict[str, Any], model: str):
        self.app_config = config
        self.model = model
        self.name = "configured"

    async def stream(self, messages, tools=None, temperature=None, max_tokens=None, cancel_event=None):
        from aria_code.apps.cli.providers.base import ConfiguredProvider, event_kind

        plain = [m if isinstance(m, dict) else {"role": m.role, "content": m.content} for m in messages]
        async for event in ConfiguredProvider(self.app_config, self.model).stream(plain, tools=[]):
            kind = event_kind(event)
            if kind == "LLMToken":
                yield {"type": "token", "text": event.text}
            elif kind == "LLMDone":
                if not event.success:
                    yield {"type": "error", "message": event.error or "the model call failed"}
                yield {"type": "done"}
                return

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        text, error = "", ""
        async for event in self.stream(messages, max_tokens=max_tokens):
            if event.get("type") == "token":
                text += event.get("text", "")
            elif event.get("type") == "error":
                error = event.get("message", "")
        return {"text": text, "content": text, "error": error, "tool_calls": []}


def build_team_llm_provider(config: dict[str, Any]) -> Any:
    model = config.get("model", "qwen2.5:7b")
    try:
        from aria_code.apps.cli.providers.chat_routing import first_round_route

        if first_round_route(model, config, None) != "ollama":
            return ConfiguredTeamProvider(config, model)
    except Exception as exc:
        logger.debug("team route check failed, using Ollama: %s", exc)
    try:
        from providers.llm.base import ProviderConfig
        from providers.llm.ollama import OllamaProvider

        url = config.get("ollama_url", "http://localhost:11434")
        return OllamaProvider(ProviderConfig(name="ollama", model=model, base_url=url))
    except Exception as exc:
        logger.debug("team LLM provider init failed: %s", exc)
        return None


async def fetch_team_data_bundle(symbol: str) -> Any:
    from packages.aria_services.data import DataService
    try:
        from datasources.router import get_router
        router = get_router()
    except Exception as exc:
        logger.debug("team data router unavailable, using market client only: %s", exc)
        router = False

    return await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: DataService(router=router).bundle(symbol),
    )


async def run_team_analysis(
    *,
    symbol: str,
    args: TeamArgs,
    config: dict[str, Any],
    sanitize_result: Callable[[Any, Any], list[str]] | None = None,
    lang: str = "zh",
    on_agent_done: Callable[[str, Any], None] | None = None,
    on_token: Callable[[str], None] | None = None,
    on_thought: Callable[[str], None] | None = None,
    on_tool_start: Callable[[str, dict], None] | None = None,
    on_tool_end: Callable[[str, Any], None] | None = None,
) -> TeamAnalysisResult:
    from agents.team import run_team
    try:
        from datasources.router import get_router
        data_router = get_router()
    except Exception as exc:
        logger.debug("team data router unavailable for agents: %s", exc)
        data_router = None

    llm_provider = build_team_llm_provider(config)
    data_bundle = None
    try:
        data_bundle = await fetch_team_data_bundle(symbol)
    except Exception as exc:
        logger.debug("team data bundle fetch failed: %s", exc)

    noisy_loggers = ["agents.base", "agents.team", "datasources.router", "data_cleaner"]
    saved_levels = {name: logging.getLogger(name).level for name in noisy_loggers}
    for name in noisy_loggers:
        logging.getLogger(name).setLevel(logging.ERROR)

    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    # The team runs inside a stdout redirect (to swallow noisy agent logs).
    # Restore the real stdout just for the on_agent_done callback so its
    # streaming tree output is actually visible to the user.
    _real_stdout = sys.stdout

    def _agent_done_proxy(name: str, result: Any) -> None:
        if on_agent_done is None:
            return
        try:
            with contextlib.redirect_stdout(_real_stdout):
                on_agent_done(name, result)
        except Exception as exc:
            logger.debug("on_agent_done render failed: %s", exc)

    try:
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
            team_result = await run_team(
                symbol=symbol,
                agents=args.agent_names,
                llm_provider=llm_provider,
                data_router=data_router,
                on_token=on_token,
                on_thought=on_thought,
                on_tool_start=on_tool_start,
                on_tool_end=on_tool_end,
                on_agent_done=_agent_done_proxy if on_agent_done else None,
                on_synthesis_start=None,
                lang=lang,
                market_context=build_team_market_context(data_bundle),
                agent_data=build_team_agent_data(data_bundle),
                use_pipeline=args.use_pipeline,
            )
    finally:
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level or logging.NOTSET)

    quality_notes = sanitize_result(team_result, data_bundle) if sanitize_result else []
    from packages.aria_services.research_quality import assess_team_report

    assessment = assess_team_report(team_result, data_bundle)
    gate_notes = [*assessment.blocking_reasons, *assessment.warnings]
    quality_notes = list(dict.fromkeys([*quality_notes, *gate_notes]))
    if assessment.decision.value == "blocked":
        team_result.confidence = min(float(getattr(team_result, "confidence", 0) or 0), 0.20)
    elif assessment.decision.value == "partial":
        team_result.confidence = min(float(getattr(team_result, "confidence", 0) or 0), 0.55)
    captured_noise = (captured_stdout.getvalue() + captured_stderr.getvalue()).strip()
    if captured_noise:
        logger.debug("captured /team noisy output for %s: %s", symbol, captured_noise[:3000])

    return TeamAnalysisResult(
        symbol=symbol,
        team_result=team_result,
        data_bundle=data_bundle,
        quality_notes=quality_notes,
        quality_assessment=assessment.to_dict(),
        captured_noise=captured_noise,
    )


async def run_deep_cli(
    *,
    symbol: str,
    args: TeamArgs,
    config: dict[str, Any],
    lang: str = "zh",
    on_agent_done: Callable[[str, Any], None] | None = None,
):
    """Run the deep analysis pipeline (P0–P3) reusing the team provider plumbing."""
    from agents.deep.pipeline import DeepAnalysisPipeline
    from datasources.router import get_router

    llm_provider = build_team_llm_provider(config)
    noisy = ["agents.base", "agents.team", "agents.deep", "datasources.router", "data_cleaner"]
    saved = {name: logging.getLogger(name).level for name in noisy}
    for name in noisy:
        logging.getLogger(name).setLevel(logging.ERROR)

    _real_stdout = sys.stdout

    def _agent_done_proxy(name: str, result: Any) -> None:
        if on_agent_done is None:
            return
        try:
            with contextlib.redirect_stdout(_real_stdout):
                on_agent_done(name, result)
        except Exception as exc:
            logger.debug("deep on_agent_done render failed: %s", exc)

    cap_out, cap_err = io.StringIO(), io.StringIO()
    try:
        pipe = DeepAnalysisPipeline(
            llm_provider=llm_provider, data_router=get_router(), lang=lang,
        )
        with contextlib.redirect_stdout(cap_out), contextlib.redirect_stderr(cap_err):
            result = await pipe.run(
                symbol,
                agents=args.agent_names,
                on_agent_done=_agent_done_proxy if on_agent_done else None,
            )
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level or logging.NOTSET)
    return result


def _agent_result_summary(team_result: Any) -> list[dict[str, Any]]:
    return [
        {
            "agent": getattr(result, "agent", ""),
            "success": bool(getattr(result, "success", False)),
            "signal": getattr(result, "signal", None),
            "confidence": getattr(result, "confidence", None),
            "error": getattr(result, "error", None),
            "key_points": getattr(result, "key_points", None),
            "degraded": bool(getattr(result, "degraded", False)),
            "provenance": list(getattr(result, "provenance", None) or []),
            "limitations": list(getattr(result, "limitations", None) or []),
        }
        for result in getattr(team_result, "results", []) or []
    ]


def build_team_report_markdown(
    *,
    symbol: str,
    team_result: Any,
    data_bundle: Any = None,
    quality_notes: list[str] | None = None,
    created_at: datetime | None = None,
) -> str:
    from packages.aria_services.research_quality import assess_team_report

    created = created_at or datetime.now()
    quality_notes = quality_notes or []
    quote_snapshot = team_quote_snapshot(data_bundle) if data_bundle else {}
    currency = quote_snapshot.get("currency") or "USD"
    price = quote_snapshot.get("price")
    cap = quote_snapshot.get("market_cap")
    providers = quote_snapshot.get("provider_chain") or []
    missing = quote_snapshot.get("missing_fields") or []
    warnings = quote_snapshot.get("warnings") or []
    errors = quote_snapshot.get("errors") or []
    quality = quote_snapshot.get("quality") or {}
    stale = bool(quote_snapshot.get("stale"))
    assessment = assess_team_report(team_result, data_bundle)

    lines = [
        f"# {symbol} 多 Agent 研究报告",
        f"> 生成时间: {created:%Y-%m-%d %H:%M}  |  最终信号: **{team_result.final_signal}**"
        f"  |  置信度: {team_result.confidence:.0%}  |  耗时: {team_result.elapsed_sec:.1f}s",
        "",
        "## 数据质量",
        "",
        f"- 数据状态: `{quote_snapshot.get('status', 'unknown')}`",
        f"- 是否过期: `{'yes' if stale else 'no'}`",
        f"- 当前参考价: `{currency} {price}`" if price is not None else "- 当前参考价: `unavailable`",
        f"- 涨跌幅: `{_fmt_pct(quote_snapshot.get('change_pct'))}`",
        f"- 成交量: `{_fmt_compact_number(quote_snapshot.get('volume'))}`",
        f"- 市值: `{compact_market_cap(cap, currency)}`" if cap else "- 市值: `unavailable`",
        f"- PE / Forward PE / EPS: `{_fmt_num(quote_snapshot.get('pe_ratio'))}` / `{_fmt_num(quote_snapshot.get('fwd_pe'))}` / `{_fmt_num(quote_snapshot.get('eps'))}`",
        f"- ROE / 营收增长: `{_fmt_num(quote_snapshot.get('roe'))}%` / `{_fmt_num(quote_snapshot.get('revenue_growth'))}%`",
        f"- 技术指标: `RSI {_fmt_num(quote_snapshot.get('rsi'), 1)} · MACD hist {_fmt_num(quote_snapshot.get('macd_hist'), 4)} · MA20 {_fmt_num(quote_snapshot.get('ma20'))} · MA60 {_fmt_num(quote_snapshot.get('ma60'))}`",
        f"- 分析师目标价: `{currency} {_fmt_num(quote_snapshot.get('analyst_target'))}`" if quote_snapshot.get("analyst_target") is not None else "- 分析师目标价: `unavailable`",
        f"- 数据源链: `{', '.join(providers) if providers else 'unknown'}`",
        f"- 缺失字段: `{', '.join(missing) if missing else 'none'}`",
        "",
        "## 完成度门禁",
        "",
        f"- 判定: `{assessment.decision.value}`",
        f"- Agent 覆盖: `{assessment.agent_coverage:.0%}`",
        f"- 核心 Agent 覆盖: `{assessment.core_coverage:.0%}`",
        f"- 降级 Agent: `{', '.join(assessment.degraded_agents) or 'none'}`",
        f"- 失败 Agent: `{', '.join(assessment.failed_agents) or 'none'}`",
    ]
    gate_notes = [*assessment.blocking_reasons, *assessment.warnings]
    if gate_notes:
        lines.append(f"- 门禁说明: `{' ; '.join(gate_notes)}`")
    if quality_notes:
        lines.append(f"- 输出校验: `{'; '.join(quality_notes)}`")
    if warnings:
        lines.append(f"- 数据警告: `{'; '.join(str(w) for w in warnings[:5])}`")
    if errors:
        lines.append(f"- 数据错误: `{'; '.join(str(e) for e in errors[:5])}`")
    if quality:
        lines.append(f"- 质量摘要: `{quality.get('status', quote_snapshot.get('status', 'unknown'))}`")
    lines += ["", "---", ""]

    for result in getattr(team_result, "results", []) or []:
        if getattr(result, "success", False):
            mode = " · DEGRADED" if getattr(result, "degraded", False) else ""
            lines += [
                f"## {result.agent.upper()} ({result.signal}, {result.confidence:.0%}{mode})",
                "",
                result.analysis or "*(无分析文本)*",
                "",
            ]
            provenance = list(getattr(result, "provenance", None) or [])
            limitations = list(getattr(result, "limitations", None) or [])
            if provenance:
                lines += [f"> Provenance: {', '.join(provenance)}", ""]
            if limitations:
                lines += [f"> Limitations: {'; '.join(limitations)}", ""]
        else:
            lines += [
                f"## {result.agent.upper()} (UNUSABLE)",
                "",
                f"> {result.error or 'agent_failed'}",
                "",
                result.analysis or "*(该 Agent 未产生可用分析文本)*",
                "",
            ]
    lines += ["---", "", "## 综合结论", "", clean_team_synthesis_text(team_result.synthesis or "*(无综合结论)*"), ""]
    return "\n".join(lines)


def save_team_report(
    *,
    symbol: str,
    team_result: Any,
    data_bundle: Any = None,
    quality_notes: list[str] | None = None,
    created_at: datetime | None = None,
) -> SavedTeamReport:
    from aria_code.artifacts import create_user_artifact, write_artifact_metadata, write_artifact_raw_data
    from packages.aria_services.research_quality import assess_team_report

    created = created_at or datetime.now()
    quality_notes = quality_notes or []
    artifact = create_user_artifact("reports/team", symbol, f"{symbol}_team_report", ".md")
    markdown = build_team_report_markdown(
        symbol=symbol,
        team_result=team_result,
        data_bundle=data_bundle,
        quality_notes=quality_notes,
        created_at=created,
    )
    artifact.path.write_text(markdown, encoding="utf-8")

    quote_snapshot = team_quote_snapshot(data_bundle) if data_bundle else {}
    quality = quote_snapshot.get("quality") or {}
    agent_results = _agent_result_summary(team_result)
    assessment = assess_team_report(team_result, data_bundle)
    write_artifact_metadata(artifact, {
        "kind": "team_report",
        "status": assessment.metadata_status,
        "symbol": symbol,
        "created_at": created.isoformat(timespec="seconds"),
        "data": {
            "agent_count": len(agent_results),
            "successful_agents": sum(1 for result in agent_results if result.get("success")),
            "failed_agents": [result.get("agent") for result in agent_results if not result.get("success")],
            "quote": quote_snapshot,
            "quality": quality,
            "quality_notes": quality_notes,
            "quality_gate": assessment.to_dict(),
        },
        "verdict": {
            "final_signal": getattr(team_result, "final_signal", None),
            "confidence": getattr(team_result, "confidence", None),
            "elapsed_sec": getattr(team_result, "elapsed_sec", None),
        },
    })
    write_artifact_raw_data(artifact, {
        "symbol": symbol,
        "agents": agent_results,
        "synthesis": getattr(team_result, "synthesis", None),
        "data_bundle": {
            "quote": getattr(data_bundle, "quote", {}) if data_bundle else {},
            "history": getattr(data_bundle, "history", {}) if data_bundle else {},
            "fundamentals": getattr(data_bundle, "fundamentals", {}) if data_bundle else {},
            "technical": getattr(data_bundle, "technical", {}) if data_bundle else {},
            "provider_chain": getattr(data_bundle, "provider_chain", []) if data_bundle else [],
            "missing_fields": getattr(data_bundle, "missing_fields", []) if data_bundle else [],
            "warnings": getattr(data_bundle, "warnings", []) if data_bundle else [],
            "errors": getattr(data_bundle, "errors", []) if data_bundle else [],
            "quality": getattr(data_bundle, "quality", {}) if data_bundle else {},
            "status": getattr(data_bundle, "status", None) if data_bundle else None,
        },
    })
    return SavedTeamReport(path=artifact.path, metadata_path=artifact.metadata_path)
