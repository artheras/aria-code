"""
agents/financial/catalyst.py — 催化剂检测 Agent
================================================
识别近期/即将到来的价格催化剂：财报日、股息除权、
分析师评级变化、大宗交易/大股东增减持。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..base import BaseAgent, AgentResult

logger = logging.getLogger(__name__)


class CatalystAgent(BaseAgent):

    name        = "catalyst"
    description = "催化剂检测 — 财报日、股息除权、分析师评级变化"

    _SYSTEM = (
        "You are an event-driven equity analyst. Analyze the upcoming and recent "
        "catalysts for a stock: earnings dates, ex-dividend dates, analyst rating "
        "changes, and insider activity. Assess whether these catalysts are "
        "likely to be POSITIVE (price-driving), NEUTRAL, or NEGATIVE. "
        "Focus on timing: catalysts within 14 days are high-impact. "
        "Conclude with: POSITIVE / NEUTRAL / NEGATIVE"
    )

    async def fetch_data(self, symbol: str) -> Dict[str, Any]:
        data = await super().fetch_data(symbol)
        catalysts: Dict[str, Any] = {}

        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)

            # 1. Earnings calendar
            try:
                cal = ticker.calendar
                if cal is not None:
                    if hasattr(cal, "to_dict"):
                        cal = cal.to_dict()
                    if isinstance(cal, dict):
                        for k in ("Earnings Date", "earnings_date", "earningsDate"):
                            v = cal.get(k)
                            if v:
                                catalysts["earnings_date"] = str(v[0] if isinstance(v, list) else v)
                                break
            except Exception as e:
                logger.debug("[catalyst] calendar fetch %s: %s", symbol, e)

            # 2. Recent analyst recommendations
            try:
                recs = ticker.recommendations
                if recs is not None and not recs.empty:
                    recs = recs.sort_index(ascending=False)
                    recent_recs = recs.head(5)
                    rec_list = []
                    for ts, row in recent_recs.iterrows():
                        rec_list.append({
                            "date":  str(ts)[:10],
                            "firm":  str(row.get("Firm", row.get("firm", ""))),
                            "grade": str(row.get("To Grade", row.get("toGrade", row.get("action", "")))),
                            "prev":  str(row.get("From Grade", row.get("fromGrade", ""))),
                            "action": str(row.get("Action", row.get("action", ""))),
                        })
                    catalysts["recommendations"] = rec_list
            except Exception as e:
                logger.debug("[catalyst] recs fetch %s: %s", symbol, e)

            # 3. Upcoming dividend
            try:
                info = ticker.info or {}
                ex_div = info.get("exDividendDate")
                div_rate = info.get("dividendRate") or info.get("trailingAnnualDividendRate")
                if ex_div:
                    from datetime import date
                    ex_date = datetime.fromtimestamp(ex_div, tz=timezone.utc).date()
                    days_to_exdiv = (ex_date - date.today()).days
                    catalysts["ex_dividend"] = {
                        "date": str(ex_date),
                        "days_away": days_to_exdiv,
                        "annual_rate": div_rate,
                    }
            except Exception as e:
                logger.debug("[catalyst] dividend fetch %s: %s", symbol, e)

            # 4. Short interest (proxy for contrarian catalyst)
            try:
                info = ticker.info or {}
                short_ratio = info.get("shortRatio")
                short_pct   = info.get("shortPercentOfFloat")
                if short_ratio or short_pct:
                    catalysts["short_interest"] = {
                        "ratio":   short_ratio,
                        "pct_float": short_pct,
                    }
            except Exception as e:
                logger.debug("[catalyst] short interest %s: %s", symbol, e)

        except Exception as e:
            logger.debug("[catalyst] yfinance init %s: %s", symbol, e)

        data["catalysts"] = catalysts
        return data

    async def analyze(self, symbol: str, data: Dict[str, Any]) -> AgentResult:
        catalysts = data.get("catalysts", {})
        quote     = data.get("quote", {})
        price     = quote.get("price", 0)

        if not catalysts:
            return AgentResult(
                agent=self.name, symbol=symbol,
                analysis=f"{symbol}: 未获取到催化剂数据。",
                confidence=0.3, signal="HOLD",
                key_points=["无近期催化剂数据"],
            )

        catalyst_block = _format_catalysts(catalysts)
        urgency        = _assess_urgency(catalysts)

        prompt = (
            f"Stock: {symbol}  Price: {price}\n\n"
            f"Catalysts:\n{catalyst_block}\n\n"
            "Evaluate:\n"
            "1. Most impactful upcoming catalyst and timing\n"
            "2. Analyst sentiment trend (upgrades vs downgrades)\n"
            "3. Event-driven trade setup (if any)\n"
            "4. Risk of negative surprise\n"
            "5. Conclude: POSITIVE / NEUTRAL / NEGATIVE"
        )

        analysis = await self._call_llm(self._SYSTEM, prompt, max_tokens=3000)
        if not analysis:
            analysis = _template_analysis(symbol, catalysts, urgency)

        signal, confidence = _derive_signal(analysis, catalysts, urgency)
        key_points         = _build_key_points(catalysts, urgency)

        return AgentResult(
            agent=self.name, symbol=symbol,
            analysis=analysis,
            confidence=confidence,
            signal=signal,
            key_points=key_points,
            data_used=catalysts,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_catalysts(c: Dict) -> str:
    lines = []

    if "earnings_date" in c:
        ed = c["earnings_date"]
        try:
            ed_dt = datetime.fromisoformat(str(ed).split()[0])
            days  = (ed_dt.date() - datetime.today().date()).days
            timing = f"（{days}天后）" if days >= 0 else f"（{-days}天前）"
        except Exception:
            timing = ""
        lines.append(f"财报日: {ed}{timing}")

    if "ex_dividend" in c:
        d = c["ex_dividend"]
        days_away = d.get("days_away", 999)
        rate      = d.get("annual_rate", "")
        rate_str  = f"  年化股息 {rate:.2f}" if rate else ""
        lines.append(f"除息日: {d.get('date','')}（{days_away}天后）{rate_str}")

    if "recommendations" in c:
        for r in c["recommendations"][:3]:
            action = r.get("action", "").upper()
            firm   = r.get("firm", "")
            grade  = r.get("grade", "")
            prev   = r.get("prev", "")
            change = f"{prev} → {grade}" if prev and prev != grade else grade
            lines.append(f"分析师评级: [{r.get('date','')}] {firm} {action} {change}")

    if "short_interest" in c:
        si = c["short_interest"]
        ratio = si.get("ratio")
        pct   = si.get("pct_float")
        parts = []
        if ratio: parts.append(f"空头比率 {ratio:.1f}x")
        if pct:   parts.append(f"流通股空仓 {pct*100:.1f}%")
        if parts: lines.append(f"空头数据: {', '.join(parts)}")

    return "\n".join(lines) or "无催化剂数据"


def _assess_urgency(c: Dict) -> str:
    if "earnings_date" in c:
        try:
            ed_dt = datetime.fromisoformat(str(c["earnings_date"]).split()[0])
            days  = (ed_dt.date() - datetime.today().date()).days
            if 0 <= days <= 7:
                return "high"
            if 0 <= days <= 14:
                return "medium"
        except Exception:
            pass
    exdiv = c.get("ex_dividend", {})
    if 0 <= exdiv.get("days_away", 999) <= 5:
        return "high"
    return "low"


def _derive_signal(analysis: str, c: Dict, urgency: str) -> tuple[str, float]:
    text = analysis.upper()
    recs = c.get("recommendations", [])
    upgrades   = sum(1 for r in recs if "UPGRAD" in r.get("action", "").upper())
    downgrades = sum(1 for r in recs if "DOWNGRAD" in r.get("action", "").upper())

    if "POSITIVE" in text:
        conf = 0.65 if urgency == "high" else 0.55
        return "BUY", conf
    if "NEGATIVE" in text:
        conf = 0.65 if urgency == "high" else 0.55
        return "SELL", conf

    if upgrades > downgrades:
        return "BUY", 0.5
    if downgrades > upgrades:
        return "SELL", 0.5
    return "HOLD", 0.4


def _build_key_points(c: Dict, urgency: str) -> List[str]:
    points = []
    if "earnings_date" in c:
        try:
            ed_dt = datetime.fromisoformat(str(c["earnings_date"]).split()[0])
            days  = (ed_dt.date() - datetime.today().date()).days
            points.append(f"财报日在 {days} 天后" if days >= 0 else f"财报 {-days} 天前已公布")
        except Exception:
            points.append(f"财报日: {c['earnings_date']}")
    if "ex_dividend" in c:
        d = c["ex_dividend"]
        points.append(f"除息日 {d.get('date','')}（{d.get('days_away','')}天）")
    recs = c.get("recommendations", [])
    if recs:
        latest = recs[0]
        points.append(f"最新评级: {latest.get('firm','')} {latest.get('grade','')}")
    if urgency == "high":
        points.append("⚡ 高优先级催化剂（7天内）")
    return points[:5]


def _template_analysis(symbol: str, c: Dict, urgency: str) -> str:
    parts = []
    if "earnings_date" in c:
        try:
            ed_dt = datetime.fromisoformat(str(c["earnings_date"]).split()[0])
            days  = (ed_dt.date() - datetime.today().date()).days
            parts.append(f"财报日在 {days} 天{'后' if days >= 0 else '前'}")
        except Exception:
            parts.append(f"财报日: {c['earnings_date']}")
    recs = c.get("recommendations", [])
    upgrades   = sum(1 for r in recs if "UPGRAD" in r.get("action", "").upper())
    downgrades = sum(1 for r in recs if "DOWNGRAD" in r.get("action", "").upper())
    if upgrades or downgrades:
        parts.append(f"近期评级: {upgrades}次升级 / {downgrades}次降级")
    if "ex_dividend" in c:
        d = c["ex_dividend"]
        parts.append(f"除息日: {d.get('date','')}（{d.get('days_away','')}天）")

    sentiment = "POSITIVE" if upgrades > downgrades else ("NEGATIVE" if downgrades > upgrades else "NEUTRAL")
    return (
        f"{symbol} 催化剂摘要：\n"
        + "\n".join(f"  • {p}" for p in parts)
        + f"\n结论：{sentiment}"
    )
