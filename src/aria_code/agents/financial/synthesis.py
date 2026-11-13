"""
agents/financial/synthesis.py — 综合汇总 Agent
===============================================
汇总所有 agent 结果，输出可操作的投资建议。
加权规则：置信度平方作为权重，高置信度信号影响力更大。
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..base import BaseAgent, AgentResult

# 每个 agent 的基础重要性权重（反映信息价值，可在 .ariarc 中覆盖）
_AGENT_WEIGHTS: Dict[str, float] = {
    "technical":   1.2,   # 短期动量信息量最高
    "fundamental": 1.1,   # 价值锚
    "risk":        1.0,
    "macro":       0.9,   # 宏观慢变量，不应过度主导短期判断
    "news":        1.0,
    "catalyst":    1.1,   # 催化剂改变时机，提高权重
    "earnings":    1.1,
    "sector":      0.8,
    "debate":      0.5,   # 调解结果已融入其他 agent，避免重复计入
}

_SIGNAL_SCORE: Dict[str, float] = {
    "STRONG_BUY":  2.0,
    "BUY":         1.0,
    "HOLD":        0.0,
    "SELL":        -1.0,
    "STRONG_SELL": -2.0,
}


class SynthesisAgent(BaseAgent):
    name        = "synthesis"
    description = "综合汇总：整合多 Agent 结论，输出操作建议（置信度加权投票）"

    _SYSTEM = (
        "You are the chief investment officer and head of portfolio strategy. "
        "Synthesize multi-agent research outputs (macro, fundamental, technical, earnings, risk, catalysts) "
        "into an executive, institutional-grade investment recommendation.\n"
        "Structure your synthesis with clear sections:\n"
        "## 1. Executive Investment Thesis & Actionable Decision\n"
        "## 2. Multi-Dimensional Scorecard (Markdown table summarizing Agent, Signal, Confidence, Key Catalyst/Headwind)\n"
        "## 3. Bull vs. Bear Case Scenarios & Target Price Bounds\n"
        "## 4. Portfolio Allocation & Risk Sizing Guidelines (Position weight, entry timing, stop-loss & take-profit levels)\n"
        "## 5. Horizon Catalysts & Invalidation Criteria (What would trigger a thesis change)\n"
        "End explicitly with: FINAL: BUY / HOLD / SELL / STRONG_BUY / STRONG_SELL."
    )

    async def analyze(self, symbol: str, data: Dict[str, Any]) -> AgentResult:
        agent_results: List[Dict] = data.get("agent_results", [])
        consensus_signal = str(data.get("consensus_signal") or "HOLD").upper()
        consensus_conf = data.get("consensus_confidence")
        consensus_conf_num = (
            _safe_float(consensus_conf)
            if consensus_conf is not None else _calc_weighted_confidence(agent_results)
        )

        # Check if any agent had real price data
        _quote = data.get("quote", {})
        _snapshot = data.get("market_snapshot", {}) or {}
        _price = _snapshot.get("price") or (_quote.get("price", 0) if _quote else 0)
        _data_available = bool(_safe_float(_price))
        _market_block = data.get("market_data_block") or _format_market_block(data)
        _target = _snapshot.get("analyst_target")
        _stop = _risk_stop(_snapshot, _snapshot.get("currency") or "USD")

        summary_parts = []
        for r in agent_results:
            if not r.get("error"):
                agent_name = r["agent"].upper()
                signal     = r.get("signal", "HOLD")
                conf       = r.get("confidence", 0.5)
                points     = r.get("key_points", [])
                pts_str    = "; ".join(points[:2]) if points else r.get("analysis", "")[:80]
                summary_parts.append(
                    f"[{agent_name}] Signal={signal} ({conf:.0%}): {pts_str}"
                )

        summary = "\n".join(summary_parts) if summary_parts else "No agent data"

        if _data_available:
            _price_instruction = (
                "3. Entry strategy using ONLY the supplied current price, MA20/MA60, RSI, MACD and analyst_target.\n"
                "4. Target/stop rules: use analyst_target only if supplied; otherwise write Target: N/A (analyst target missing). "
                "Use stated support first; use MA60 only when price is still above it. If price is already below MA60, call MA60 a recovery level, not a stop. Do not invent prices.\n"
                f"FINAL: {consensus_signal} | Target: "
                f"{'$' + str(_target) if _safe_float(_target) else 'N/A (analyst target missing)'} | "
                f"Stop: {_stop}"
            )
        else:
            _price_instruction = (
                "3. Entry strategy qualitative only because current price is missing.\n"
                "4. Do not give target or stop prices.\n"
                f"FINAL: {consensus_signal} | Target: N/A (price missing) | Stop: N/A"
            )

        prompt = (
            f"Symbol: {symbol}\n"
            f"Consensus signal: {consensus_signal}\n"
            f"Consensus confidence: {consensus_conf_num:.0%}\n"
            f"Real price data available: {'YES — price=' + str(_price) if _data_available else 'NO — do not invent prices'}\n"
            f"Verified market data:\n{_market_block}\n\n"
            f"Agent Analyses:\n{summary}\n\n"
            "Deliver an in-depth institutional investment synthesis following the 5-section executive framework above. "
            "Include complete markdown scorecard tables, scenario targets, and actionable risk sizing rules.\n"
            + _price_instruction
        )

        analysis  = await self._call_llm(self._SYSTEM, prompt, max_tokens=4096, quote=_quote)
        if not analysis:
            analysis = _template_synthesis(symbol, agent_results, data)
        elif _data_available:
            analysis = (
                analysis
                .replace("N/A (no real data)", "N/A (analyst target missing)")
                .replace("no real data", "analyst target missing")
                .replace("信心 ≤40%", f"置信度 {consensus_conf_num:.0%}")
            )

        signal     = consensus_signal or _extract_final_signal(analysis)
        confidence = consensus_conf_num
        key_points = _extract_key_points(analysis)

        return AgentResult(
            agent=self.name, symbol=symbol,
            analysis=analysis, confidence=confidence,
            signal=signal, key_points=key_points,
        )


def _extract_final_signal(text: str) -> str:
    import re
    m = re.search(r"FINAL[:\s]+([A-Z_]+)", text.upper())
    if m:
        raw = m.group(1)
        if "STRONG_BUY" in raw:  return "STRONG_BUY"
        if "BUY" in raw:         return "BUY"
        if "STRONG_SELL" in raw: return "STRONG_SELL"
        if "SELL" in raw:        return "SELL"
    return "HOLD"


def _calc_weighted_confidence(results: List[Dict]) -> float:
    """
    Confidence = weighted average where weight = agent_base_weight × confidence².
    High-confidence agents dominate; low-confidence agents contribute minimally.
    """
    valid = [r for r in results if not r.get("error")]
    if not valid:
        return 0.5
    total_w, weighted_sum = 0.0, 0.0
    for r in valid:
        conf      = max(0.0, min(1.0, float(r.get("confidence", 0.5))))
        base_w    = _AGENT_WEIGHTS.get(r.get("agent", ""), 1.0)
        w         = base_w * (conf ** 2)          # confidence² amplifies strong signals
        weighted_sum += conf * w
        total_w      += w
    return round(weighted_sum / total_w, 2) if total_w else 0.5


def _calc_weighted_signal_score(results: List[Dict]) -> float:
    """Weighted signal score used by the template fallback."""
    valid = [r for r in results if not r.get("error")]
    if not valid:
        return 0.0
    total_w, score_sum = 0.0, 0.0
    for r in valid:
        conf   = max(0.0, min(1.0, float(r.get("confidence", 0.5))))
        base_w = _AGENT_WEIGHTS.get(r.get("agent", ""), 1.0)
        w      = base_w * (conf ** 2)
        score  = _SIGNAL_SCORE.get(r.get("signal", "HOLD"), 0.0)
        score_sum += score * w
        total_w   += w
    return score_sum / total_w if total_w else 0.0


def _extract_key_points(text: str) -> List[str]:
    points = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith(("1.", "2.", "3.", "4.", "•", "-", "·")) and len(line) > 8:
            points.append(line.lstrip("1234567890.-•· "))
    return points[:4]


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _fmt_price(value: Any, currency: str = "USD") -> str:
    number = _safe_float(value)
    return f"{currency} {number:.2f}" if number is not None else "N/A"


def _first_level(value: Any) -> float | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            level = _first_level(item)
            if level is not None:
                return level
        return None
    if isinstance(value, dict):
        for key in ("price", "level", "value"):
            level = _safe_float(value.get(key))
            if level is not None:
                return level
        return None
    text = str(value).replace("USD", "").replace("$", "").split(",")[0].strip()
    return _safe_float(text)


def _risk_stop(snapshot: Dict[str, Any], currency: str = "USD") -> str:
    support = _first_level(snapshot.get("support") or snapshot.get("supports"))
    if support is not None:
        return f"below support {_fmt_price(support, currency)}"

    ma60 = _safe_float(snapshot.get("ma60"))
    price = _safe_float(snapshot.get("price"))
    if ma60 is None:
        return "N/A (technical stop missing)"
    if price is not None and price < ma60:
        return f"N/A (already below MA60; recovery level {_fmt_price(ma60, currency)})"
    return f"below MA60 {_fmt_price(ma60, currency)}"


def _format_market_block(data: Dict[str, Any]) -> str:
    snapshot = data.get("market_snapshot", {}) or {}
    quote = data.get("quote", {}) or {}
    fundamentals = data.get("fundamentals", {}) or {}
    technical = data.get("technical", {}) or {}
    quality = data.get("data_quality", {}) or {}
    return "\n".join([
        f"data_status={quality.get('status') or snapshot.get('status') or 'unknown'}",
        f"providers={', '.join(snapshot.get('provider_chain') or quality.get('providers') or []) or 'unknown'}",
        f"missing={', '.join(snapshot.get('missing_fields') or quality.get('missing_fields') or []) or 'none'}",
        f"price={snapshot.get('price') or quote.get('price')}",
        f"market_cap={snapshot.get('market_cap') or quote.get('market_cap') or fundamentals.get('market_cap')}",
        f"pe={snapshot.get('pe_ratio') or fundamentals.get('pe_ratio') or fundamentals.get('pe_ttm')}",
        f"analyst_target={snapshot.get('analyst_target') or fundamentals.get('analyst_target')}",
        f"rsi={snapshot.get('rsi') or technical.get('rsi')}",
        f"macd_hist={snapshot.get('macd_hist') or technical.get('macd_hist')}",
        f"ma20={snapshot.get('ma20') or technical.get('ma20')}",
        f"ma60={snapshot.get('ma60') or technical.get('ma60')}",
    ])


def _template_synthesis(symbol: str, results: List[Dict], data: Dict[str, Any] | None = None) -> str:
    data = data or {}
    avg_s  = _calc_weighted_signal_score(results)
    final  = str(data.get("consensus_signal") or ("BUY" if avg_s > 0.4 else ("SELL" if avg_s < -0.4 else "HOLD"))).upper()
    conf   = float(data.get("consensus_confidence") or _calc_weighted_confidence(results))
    snapshot = data.get("market_snapshot", {}) or {}
    currency = snapshot.get("currency") or "USD"
    price = _fmt_price(snapshot.get("price"), currency)
    target = _fmt_price(snapshot.get("analyst_target"), currency) if _safe_float(snapshot.get("analyst_target")) else "N/A (analyst target missing)"
    stop = _risk_stop(snapshot, currency)
    providers = ", ".join(snapshot.get("provider_chain") or []) or "unknown"
    missing = ", ".join(snapshot.get("missing_fields") or []) or "none"

    agent_lines = "\n".join(
        f"  • {r['agent'].upper()} ({r.get('confidence', 0.5):.0%}): "
        f"{r.get('signal','?')} — " + "; ".join(r.get("key_points", [])[:1])
        for r in results if not r.get("error")
    )
    return (
        f"{symbol} 综合分析（真实数据加权汇总）\n"
        f"当前价: {price}  数据源: {providers}  缺失: {missing}\n"
        f"{agent_lines}\n\n"
        f"加权信号得分: {avg_s:+.2f}  综合置信度: {conf:.0%}\n"
        f"FINAL: {final} | Target: {target} | Stop: {stop}"
    )
