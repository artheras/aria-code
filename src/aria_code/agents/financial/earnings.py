"""
agents/financial/earnings.py — 财报解读 Agent
=============================================
分析最近一期财报：EPS/营收 beat or miss、同比/环比变化、
指引调整及市场反应。在财报发布后 5 天内尤为有效。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..base import BaseAgent, AgentResult

logger = logging.getLogger(__name__)


class EarningsAgent(BaseAgent):

    name        = "earnings"
    description = "财报解读 — EPS/营收 beat or miss、同比增速、指引变化"

    _SYSTEM = (
        "You are a senior equity research analyst specializing in corporate earnings and quarterly report teardowns. "
        "Your task is to provide a rigorous, comprehensive, institutional-grade earnings report breakdown.\n"
        "Structure your analysis thoroughly with clear markdown headings and structured data tables:\n"
        "## 1. Executive Summary & Headline Surprises (EPS & Revenue Beat/Miss metrics)\n"
        "## 2. Key Financial Performance Table (Quarter-over-Quarter and Year-over-Year comparison of Revenue, Gross Margin, Operating Margin, Net Income, EPS, and Free Cash Flow)\n"
        "## 3. Segment & Regional Performance Breakdown (Primary revenue drivers, deceleration or acceleration vectors)\n"
        "## 4. Forward Guidance & Management Commentary (Full-year/Next-quarter outlook, CapEx revisions, margins trajectory, and execution risks)\n"
        "## 5. Market Reaction & Valuation Implications (Price reaction, earnings quality, forward multiples vs historical bands)\n"
        "## 6. Strategic Conclusion & Verdict\n"
        "Conclude with explicit verdict: STRONG_BEAT / BEAT / IN_LINE / MISS / STRONG_MISS.\n"
        "Signal mapping: BEAT/STRONG_BEAT→BUY, IN_LINE→HOLD, MISS/STRONG_MISS→SELL."
    )

    async def fetch_data(self, symbol: str) -> Dict[str, Any]:
        data = await super().fetch_data(symbol)
        earnings: Dict[str, Any] = {}

        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)

            # 1. Quarterly earnings history
            try:
                qe = ticker.quarterly_earnings
                if qe is not None and not qe.empty:
                    rows = []
                    for ts, row in qe.sort_index(ascending=False).head(4).iterrows():
                        rows.append({
                            "period": str(ts)[:10],
                            "actual_eps":   _safe_float(row.get("Earnings")),
                            "est_eps":      _safe_float(row.get("Estimate", row.get("EPS Estimate"))),
                            "revenue":      _safe_float(row.get("Revenue")),
                            "rev_estimate": _safe_float(row.get("Revenue Estimate")),
                        })
                    earnings["quarterly_eps"] = rows
            except Exception as e:
                logger.debug("[earnings] quarterly_earnings %s: %s", symbol, e)

            # 2. Earnings dates
            try:
                ed = ticker.earnings_dates
                if ed is not None and not ed.empty:
                    recent = ed.sort_index(ascending=False).head(2)
                    dates = []
                    for ts, row in recent.iterrows():
                        reported_eps = _safe_float(row.get("Reported EPS", row.get("EPS")))
                        est_eps      = _safe_float(row.get("EPS Estimate"))
                        if reported_eps is not None:
                            beat = reported_eps - est_eps if est_eps is not None else None
                            dates.append({
                                "date": str(ts)[:10],
                                "reported_eps": reported_eps,
                                "estimated_eps": est_eps,
                                "beat_by": round(beat, 4) if beat is not None else None,
                                "pct_surprise": round(beat / abs(est_eps) * 100, 1)
                                if (beat is not None and est_eps and est_eps != 0) else None,
                            })
                    earnings["recent_reports"] = dates
            except Exception as e:
                logger.debug("[earnings] earnings_dates %s: %s", symbol, e)

            # 3. Revenue trend (quarterly income)
            try:
                qs = ticker.quarterly_financials
                if qs is not None and not qs.empty:
                    rev_row = None
                    for label in ("Total Revenue", "Revenue", "Net Revenue"):
                        if label in qs.index:
                            rev_row = qs.loc[label]
                            break
                    if rev_row is not None:
                        revs = []
                        for ts, val in rev_row.sort_index(ascending=False).head(4).items():
                            revs.append({"period": str(ts)[:10], "revenue": _safe_float(val)})
                        earnings["revenue_trend"] = revs
            except Exception as e:
                logger.debug("[earnings] quarterly_financials %s: %s", symbol, e)

            # 4. Stock price reaction (compare price before/after earnings)
            try:
                recent = earnings.get("recent_reports", [])
                if recent:
                    from datetime import date
                    report_date_str = recent[0].get("date", "")
                    if report_date_str:
                        hist = ticker.history(start=report_date_str, period="5d")
                        if not hist.empty and len(hist) >= 2:
                            open_price  = float(hist["Open"].iloc[0])
                            close_price = float(hist["Close"].iloc[-1])
                            earnings["price_reaction_pct"] = round(
                                (close_price - open_price) / open_price * 100, 2
                            )
            except Exception as e:
                logger.debug("[earnings] price_reaction %s: %s", symbol, e)

        except Exception as e:
            logger.debug("[earnings] yfinance init %s: %s", symbol, e)

        data["earnings"] = earnings
        return data

    async def analyze(self, symbol: str, data: Dict[str, Any]) -> AgentResult:
        earnings = data.get("earnings", {})
        quote    = data.get("quote", {})
        price    = quote.get("price", 0)

        if not earnings:
            return AgentResult(
                agent=self.name, symbol=symbol,
                analysis=(f"{symbol}: 未获取到财报数据。" if str(self.lang).startswith("zh")
                          else f"{symbol}: no earnings data."),
                confidence=0.3, signal="HOLD",
                key_points=["无财报数据" if str(self.lang).startswith("zh") else "no earnings data"],
            )

        earnings_block    = _format_earnings(earnings)
        most_recent       = earnings.get("recent_reports", [{}])[0] if earnings.get("recent_reports") else {}
        beat_miss_summary = _assess_beat_miss(most_recent, earnings)

        prompt = (
            f"Stock: {symbol}  Price: {price}\n\n"
            f"Earnings Data:\n{earnings_block}\n\n"
            f"Initial Assessment: {beat_miss_summary}\n\n"
            "Please provide a comprehensive, deep-dive earnings analysis following the 6-section structure above. "
            "Include structural markdown comparison tables and granular percentage changes. "
            "Do not output superficial summaries; deliver rigorous institutional analysis."
        )

        analysis = await self._call_llm(self._SYSTEM, prompt, max_tokens=4096)
        if not analysis:
            analysis = _template_analysis(symbol, earnings, most_recent, beat_miss_summary, lang=self.lang)

        signal, confidence = _derive_signal(analysis, most_recent, earnings)
        key_points         = _build_key_points(earnings, most_recent, signal, lang=self.lang)

        return AgentResult(
            agent=self.name, symbol=symbol,
            analysis=analysis,
            confidence=confidence,
            signal=signal,
            key_points=key_points,
            data_used={
                "beat_miss": beat_miss_summary,
                "pct_surprise": most_recent.get("pct_surprise"),
                "price_reaction_pct": earnings.get("price_reaction_pct"),
            },
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(val) -> Optional[float]:
    try:
        v = float(val)
        return None if (v != v) else v  # NaN check
    except (TypeError, ValueError):
        return None


def _format_earnings(e: Dict) -> str:
    lines = []
    recent = e.get("recent_reports", [])
    if recent:
        lines.append("Recent Reports:")
        for r in recent[:2]:
            act = r.get("reported_eps")
            est = r.get("estimated_eps")
            sup = r.get("pct_surprise")
            lines.append(
                f"  {r.get('date','')}  EPS: actual {act}  est {est}  "
                f"surprise {sup:+.1f}%" if sup else
                f"  {r.get('date','')}  EPS: actual {act}  est {est}"
            )

    rx = e.get("price_reaction_pct")
    if rx is not None:
        lines.append(f"Post-earnings price reaction: {rx:+.1f}%")

    qt = e.get("quarterly_eps", [])
    if qt:
        lines.append("Quarterly EPS (last 4):")
        for q in qt[:4]:
            ep = q.get("actual_eps")
            lines.append(f"  {q.get('period','')}  EPS: {ep}")

    rev = e.get("revenue_trend", [])
    if rev:
        lines.append("Revenue Trend (last 4 quarters):")
        prev = None
        for r in rev[:4]:
            rv = r.get("revenue")
            growth = ""
            if rv and prev:
                g = (rv - prev) / abs(prev) * 100
                growth = f"  {g:+.1f}% QoQ"
            lines.append(f"  {r.get('period','')}  {_fmt_num(rv)}{growth}")
            prev = rv

    return "\n".join(lines) or "无财报数据"


def _fmt_num(v) -> str:
    if v is None: return "N/A"
    if abs(v) >= 1e9:  return f"${v/1e9:.2f}B"
    if abs(v) >= 1e6:  return f"${v/1e6:.1f}M"
    return f"{v:.4f}"


def _assess_beat_miss(most_recent: Dict, earnings: Dict) -> str:
    sup = most_recent.get("pct_surprise")
    if sup is None:
        return "无充分数据判断"
    if sup >= 10:  return f"大幅超预期 +{sup:.1f}%"
    if sup >= 3:   return f"超预期 +{sup:.1f}%"
    if sup >= -3:  return f"基本符合预期 {sup:+.1f}%"
    if sup >= -10: return f"略低预期 {sup:.1f}%"
    return f"大幅低于预期 {sup:.1f}%"


def _derive_signal(analysis: str, most_recent: Dict, earnings: Dict) -> tuple[str, float]:
    text  = analysis.upper()
    sup   = most_recent.get("pct_surprise")
    rx    = earnings.get("price_reaction_pct")

    if "STRONG_BEAT" in text:
        return "BUY", 0.75
    if "STRONG_MISS" in text:
        return "SELL", 0.75
    if "BEAT" in text and "MISS" not in text:
        conf = 0.65 if (rx and rx > 2) else 0.55
        return "BUY", conf
    if "MISS" in text and "BEAT" not in text:
        conf = 0.65 if (rx and rx < -2) else 0.55
        return "SELL", conf

    # Fallback to raw surprise
    if sup is not None:
        if sup >= 10:  return "BUY",  0.70
        if sup >= 3:   return "BUY",  0.55
        if sup <= -10: return "SELL", 0.70
        if sup <= -3:  return "SELL", 0.55

    return "HOLD", 0.40


def _build_key_points(earnings: Dict, most_recent: Dict, signal: str, lang: str = "zh") -> List[str]:
    zh = str(lang).lower().startswith("zh")
    pts = []
    sup = most_recent.get("pct_surprise")
    if sup is not None:
        if zh:
            pts.append(f"EPS 超预期 {sup:+.1f}%" if sup >= 0 else f"EPS 低于预期 {sup:.1f}%")
        else:
            pts.append(f"EPS beat by {sup:+.1f}%" if sup >= 0 else f"EPS missed by {sup:.1f}%")
    rx = earnings.get("price_reaction_pct")
    if rx is not None:
        pts.append(f"财报后股价反应: {rx:+.1f}%" if zh else f"post-earnings move {rx:+.1f}%")
    rev = earnings.get("revenue_trend", [])
    if len(rev) >= 2:
        v0 = rev[0].get("revenue")
        v1 = rev[1].get("revenue")
        if v0 and v1:
            qoq = (v0 - v1) / abs(v1) * 100
            pts.append(f"营收环比 {qoq:+.1f}%" if zh else f"revenue {qoq:+.1f}% q/q")
    pts.append(f"财报信号: {signal}" if zh else f"earnings signal: {signal}")
    return pts[:5]


def _template_analysis(symbol: str, earnings: Dict, most_recent: Dict, summary: str, lang: str = "zh") -> str:
    sup = most_recent.get("pct_surprise")
    if sup is not None:
        if sup >= 10:   verdict = "STRONG_BEAT"
        elif sup >= 3:  verdict = "BEAT"
        elif sup >= -3: verdict = "IN_LINE"
        elif sup >= -10:verdict = "MISS"
        else:           verdict = "STRONG_MISS"
    else:
        verdict = "IN_LINE"

    if not str(lang).lower().startswith("zh"):
        return (
            f"{symbol} earnings (template):\n"
            f"First read: {summary}\n"
            f"Rating: {verdict}\n"
            f"Suggested signal: {'BUY' if 'BEAT' in verdict else ('SELL' if 'MISS' in verdict else 'HOLD')}"
        )
    return (
        f"{symbol} 财报解读（模板）：\n"
        f"初步判断：{summary}\n"
        f"财报评级：{verdict}\n"
        f"建议信号：{'BUY' if 'BEAT' in verdict else ('SELL' if 'MISS' in verdict else 'HOLD')}"
    )
