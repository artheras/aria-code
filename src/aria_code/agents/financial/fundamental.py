"""
agents/financial/fundamental.py — 基本面分析 Agent
====================================================
分析：PE/PB/ROE、营收增速、竞争壁垒、估值水位。
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from ..base import BaseAgent, AgentResult


class FundamentalAgent(BaseAgent):
    name        = "fundamental"
    description = "基本面分析：估值/ROE/竞争优势/财务健康"

    _SYSTEM = (
        "You are a fundamental equity analyst. Evaluate: PE/PB valuation levels "
        "(vs historical range and sector peers), ROE quality, revenue growth, "
        "balance sheet health, and competitive moat. "
        "Be data-driven. End with: UNDERVALUED / FAIRLY_VALUED / OVERVALUED."
    )

    async def fetch_data(self, symbol: str) -> Dict[str, Any]:
        data = await super().fetch_data(symbol)
        if self.data:
            try:
                f = self.data.fundamentals(symbol)
                if f:
                    data["fundamentals"] = {
                        "pe_ttm":          f.pe_ttm,
                        "pb":              f.pb,
                        "roe":             f.roe,
                        "revenue_growth":  f.revenue_growth,
                        "dividend_yield":  f.dividend_yield,
                        "source":          f.source,
                    }
            except Exception:
                pass
        return data

    async def analyze(self, symbol: str, data: Dict[str, Any]) -> AgentResult:
        quote = data.get("quote", {})
        fund  = data.get("fundamentals", {})
        price = _num_or_none(quote.get("price"))
        # Accept both schemas: the agent's own fetch_data (pe_ttm/pb) AND the
        # shared team data bundle (pe_ratio/pb_ratio from finnhub/yahoo).
        pe    = _num_or_none(
            fund.get("pe_ttm") or fund.get("pe_ratio") or fund.get("pe")
            or quote.get("pe_ttm") or quote.get("pe_ratio")
        )
        pb    = _num_or_none(
            fund.get("pb") or fund.get("pb_ratio")
            or quote.get("pb") or quote.get("pb_ratio")
        )
        roe   = _num_or_none(fund.get("roe"))
        rev_g = _num_or_none(fund.get("revenue_growth") or fund.get("rev_growth"))
        div_y = _num_or_none(fund.get("dividend_yield"))

        fund_str = (
            f"  PE(TTM): {_fmt_num(pe, 1, 'x')}\n"
            f"  PB:      {_fmt_num(pb, 2, 'x')}\n"
            f"  ROE:     {_fmt_num(roe, 1, '%')}\n"
            f"  Revenue growth: {_fmt_num(rev_g, 1, '%')}\n"
            f"  Dividend yield: {_fmt_num(div_y, 2, '%')}"
        ) if pe or pb else "  (fundamental data unavailable)"

        prompt = (
            f"Stock: {symbol}  Price: {price}\n"
            f"Fundamentals:\n{fund_str}\n\n"
            "Evaluate:\n"
            "1. Valuation (PE/PB vs historical/sector)\n"
            "2. Profitability quality (ROE trend)\n"
            "3. Growth outlook (revenue/earnings)\n"
            "4. Balance sheet and dividend\n"
            "5. Competitive moat\n"
            "Conclusion: UNDERVALUED / FAIRLY_VALUED / OVERVALUED\n\n"
            "Data limits: use only the financial fields supplied above. No historical series, peer "
            "panel or factor validation report is connected here; do not claim those comparisons "
            "or tests were run. If a valuation or financial field is missing, identify it as unavailable."
        )

        analysis  = await self._call_llm(self._SYSTEM, prompt, max_tokens=500, quote=quote)
        if not analysis:
            analysis = _template_fundamental(symbol, pe, pb, roe, rev_g, lang=self.lang)

        signal     = _extract_signal(analysis, pe or 0)
        confidence = _calc_confidence(pe or 0, pb or 0, roe or 0)
        key_points = _extract_key_points(analysis)

        return AgentResult(
            agent=self.name, symbol=symbol,
            analysis=analysis, confidence=confidence,
            signal=signal, key_points=key_points,
            data_used={"pe": pe, "pb": pb, "roe": roe},
        )


def _num_or_none(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
        return out if out != 0 else None
    except (TypeError, ValueError):
        return None


def _fmt_num(value: Optional[float], digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "数据不足"
    return f"{value:.{digits}f}{suffix}"


def _extract_signal(text: str, pe: float = 0) -> str:
    t = text.upper()
    if "UNDERVALUED" in t or "低估" in t:   return "BUY"
    if "OVERVALUED"  in t or "高估" in t:   return "SELL"
    # PE 辅助判断
    if pe > 0:
        if pe < 15:   return "BUY"
        if pe > 50:   return "SELL"
    return "HOLD"


def _calc_confidence(pe: float, pb: float, roe: float) -> float:
    if pe <= 0 and pb <= 0 and roe == 0:
        return 0.4   # 无数据，低置信度
    score = 0.5
    if pe > 0:
        if   pe < 15: score += 0.15
        elif pe < 25: score += 0.05
        elif pe > 50: score -= 0.15
    if roe > 20: score += 0.15
    elif roe > 10: score += 0.05
    return round(min(0.95, max(0.2, score)), 2)


def _extract_key_points(text: str) -> List[str]:
    points = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith(("1.", "2.", "3.", "4.", "5.", "•", "-", "·")) and len(line) > 5:
            points.append(line.lstrip("1234567890.-•· "))
    return points[:4]


def _template_fundamental(symbol: str, pe: Optional[float], pb: Optional[float],
                           roe: Optional[float], rev_g: Optional[float], lang: str = "zh") -> str:
    if not str(lang).lower().startswith("zh"):
        if not pe or pe <= 0:
            valuation, conclusion = "not enough data", "DATA_LIMITED"
        elif pe < 15:
            valuation, conclusion = "undervalued", "UNDERVALUED"
        elif pe > 40:
            valuation, conclusion = "overvalued", "OVERVALUED"
        else:
            valuation, conclusion = "fairly valued", "FAIRLY_VALUED"
        return (
            f"{symbol} fundamentals (template):\n"
            f"• Valuation: PE={_fmt_num(pe, 1, 'x')}  PB={_fmt_num(pb, 2, 'x')}  → {valuation}\n"
            f"• Profitability: ROE={_fmt_num(roe, 1, '%')}  revenue growth={_fmt_num(rev_g, 1, '%')}\n"
            "• Missing figures are not zeros; check against the filings and peers\n"
            f"• Conclusion: {conclusion}"
        )
    if not pe or pe <= 0:
        valuation, conclusion = "数据不足", "DATA_LIMITED"
    elif pe < 15:
        valuation, conclusion = "低估", "UNDERVALUED"
    elif pe > 40:
        valuation, conclusion = "高估", "OVERVALUED"
    else:
        valuation, conclusion = "合理", "FAIRLY_VALUED"
    return (
        f"{symbol} 基本面分析（模板）:\n"
        f"• 估值：PE={_fmt_num(pe, 1, 'x')}  PB={_fmt_num(pb, 2, 'x')}  → {valuation}\n"
        f"• 盈利能力：ROE={_fmt_num(roe, 1, '%')}  营收增速={_fmt_num(rev_g, 1, '%')}\n"
        "• 基本面数据不足时，不应把缺失值当作 0；建议结合财报和行业对比复核\n"
        f"• 结论: {conclusion}"
    )
