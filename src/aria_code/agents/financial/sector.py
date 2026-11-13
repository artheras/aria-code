"""
agents/financial/sector.py — 行业轮动分析 Agent
================================================
分析标的所属行业的相对强弱：行业指数表现、资金流入/流出趋势、
板块轮动阶段，以判断标的是否处于顺风还是逆风环境。
数据源：yfinance 行业 ETF（XLK/XLF/XLE/…）+ akshare 行业指数（A股）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..base import BaseAgent, AgentResult

logger = logging.getLogger(__name__)

# 美股行业 ETF 映射 (GICS Sectors)
_US_SECTOR_ETFS: Dict[str, str] = {
    "Technology":             "XLK",
    "Health Care":            "XLV",
    "Financials":             "XLF",
    "Consumer Discretionary": "XLY",
    "Consumer Staples":       "XLP",
    "Industrials":            "XLI",
    "Energy":                 "XLE",
    "Materials":              "XLB",
    "Real Estate":            "XLRE",
    "Utilities":              "XLU",
    "Communication Services": "XLC",
}

# 默认参考基准
_BENCHMARK = "SPY"


class SectorAgent(BaseAgent):

    name        = "sector"
    description = "行业轮动 — 所属板块相对强弱、资金流入/流出趋势"

    _SYSTEM = (
        "You are a sector rotation analyst. Given a stock and its sector's "
        "recent performance relative to the broader market, assess:\n"
        "1. Is the sector in an uptrend or downtrend vs the S&P 500?\n"
        "2. Is money flowing INTO or OUT OF this sector recently?\n"
        "3. What stage of the sector rotation cycle is this sector in "
        "(early/mid/late recovery, contraction)?\n"
        "4. Does the sector tailwind/headwind support BUY/HOLD/SELL on the stock?\n"
        "Conclude: TAILWIND (sector supports bullish) / NEUTRAL / HEADWIND (sector drags bearish)"
    )

    async def fetch_data(self, symbol: str) -> Dict[str, Any]:
        data = await super().fetch_data(symbol)
        sector_data: Dict[str, Any] = {}

        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)

            # 1. Identify sector from stock info
            info   = ticker.info or {}
            sector = info.get("sector", "")
            industry = info.get("industry", "")
            sector_data["sector"]   = sector
            sector_data["industry"] = industry

            # 2. Get sector ETF for US stocks
            etf_sym = _US_SECTOR_ETFS.get(sector)
            if not etf_sym:
                # Try to guess from ticker pattern for A-shares
                if _is_a_share(symbol):
                    sector_data["market"] = "CN"
                    sector_data["etf_sym"] = None
                else:
                    sector_data["market"] = "US"
                    sector_data["etf_sym"] = None
            else:
                sector_data["etf_sym"] = etf_sym
                sector_data["market"] = "US"

            # 3. Fetch returns for sector ETF and benchmark
            comparisons: Dict[str, Dict] = {}
            for periods in [("1mo", 21), ("3mo", 63), ("6mo", 126)]:
                period_key, _ = periods[0], periods[1]
                comparisons[period_key] = {}

            syms_to_fetch = [_BENCHMARK]
            if etf_sym:
                syms_to_fetch.append(etf_sym)

            for fetch_sym in syms_to_fetch:
                try:
                    hist = yf.Ticker(fetch_sym).history(period="6mo")
                    if hist.empty:
                        continue
                    close = hist["Close"]
                    for period_key, days in [("1mo", 21), ("3mo", 63), ("6mo", len(close))]:
                        if len(close) >= days:
                            ret = (float(close.iloc[-1]) - float(close.iloc[-days])) / float(close.iloc[-days])
                            comparisons[period_key][fetch_sym] = round(ret * 100, 2)
                except Exception as e:
                    logger.debug("[sector] fetch %s: %s", fetch_sym, e)

            sector_data["comparisons"] = comparisons

            # 4. Stock's own recent performance vs sector
            try:
                hist = ticker.history(period="3mo")
                if not hist.empty and len(hist) >= 21:
                    close = hist["Close"]
                    stock_1mo = (float(close.iloc[-1]) - float(close.iloc[-21])) / float(close.iloc[-21]) * 100
                    sector_data["stock_1mo_return"] = round(stock_1mo, 2)
            except Exception as e:
                logger.debug("[sector] stock return %s: %s", symbol, e)

            # 5. A-share sector data via akshare
            if _is_a_share(symbol):
                try:
                    import akshare as ak
                    sector_df = ak.stock_board_industry_name_em()
                    if sector_df is not None and not sector_df.empty:
                        sector_data["cn_sectors_available"] = True
                except Exception:
                    pass

        except Exception as e:
            logger.debug("[sector] yfinance init %s: %s", symbol, e)

        data["sector_data"] = sector_data
        return data

    async def analyze(self, symbol: str, data: Dict[str, Any]) -> AgentResult:
        sd    = data.get("sector_data", {})
        quote = data.get("quote", {})
        price = quote.get("price", 0)

        if not sd:
            return AgentResult(
                agent=self.name, symbol=symbol,
                analysis=f"{symbol}: 未获取到行业数据。",
                confidence=0.3, signal="HOLD",
                key_points=["无行业数据"],
            )

        sector_block = _format_sector_stats(sd)
        rel_strength = _compute_relative_strength(sd)

        prompt = (
            f"Stock: {symbol}  Sector: {sd.get('sector', 'Unknown')}  "
            f"Industry: {sd.get('industry', 'Unknown')}\n\n"
            f"Sector Performance vs Market:\n{sector_block}\n\n"
            f"Relative Strength Assessment: {rel_strength}\n\n"
            "Analyze sector rotation dynamics and conclude:\n"
            "TAILWIND / NEUTRAL / HEADWIND"
        )

        analysis = await self._call_llm(self._SYSTEM, prompt, max_tokens=3000)
        if not analysis:
            analysis = _template_analysis(symbol, sd, rel_strength)

        signal, confidence = _derive_signal(analysis, sd, rel_strength)
        key_points         = _build_key_points(sd, rel_strength, signal)

        return AgentResult(
            agent=self.name, symbol=symbol,
            analysis=analysis,
            confidence=confidence,
            signal=signal,
            key_points=key_points,
            data_used={
                "sector": sd.get("sector"),
                "etf": sd.get("etf_sym"),
                "rel_strength": rel_strength,
            },
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_a_share(symbol: str) -> bool:
    import re
    return bool(re.match(r"^[036]\d{5}$", symbol))


def _format_sector_stats(sd: Dict) -> str:
    lines = []
    sector  = sd.get("sector", "Unknown")
    etf_sym = sd.get("etf_sym", "")
    comps   = sd.get("comparisons", {})
    lines.append(f"Sector: {sector}{f'  ETF: {etf_sym}' if etf_sym else ''}")

    if comps:
        lines.append(f"\n{'Period':<8} {'SPY':>8} {etf_sym or 'SECTOR':>8}  Rel.Strength")
        for period in ("1mo", "3mo", "6mo"):
            c = comps.get(period, {})
            spy = c.get(_BENCHMARK)
            etf = c.get(etf_sym) if etf_sym else None
            spy_str = f"{spy:+.1f}%" if spy is not None else "  N/A"
            etf_str = f"{etf:+.1f}%" if etf is not None else "  N/A"
            rel = ""
            if spy is not None and etf is not None:
                diff = etf - spy
                rel  = f"{diff:+.1f}% vs SPY"
            lines.append(f"{period:<8} {spy_str:>8} {etf_str:>8}  {rel}")

    stock_1mo = sd.get("stock_1mo_return")
    if stock_1mo is not None:
        lines.append(f"\nStock 1-month: {stock_1mo:+.1f}%")

    return "\n".join(lines)


def _compute_relative_strength(sd: Dict) -> str:
    etf_sym = sd.get("etf_sym")
    comps   = sd.get("comparisons", {})
    if not etf_sym or not comps:
        return "无法计算（无行业 ETF 数据）"

    scores = []
    for period in ("1mo", "3mo"):
        c   = comps.get(period, {})
        spy = c.get(_BENCHMARK)
        etf = c.get(etf_sym)
        if spy is not None and etf is not None:
            scores.append(etf - spy)

    if not scores:
        return "数据不足"
    avg_rs = sum(scores) / len(scores)
    if avg_rs > 3:
        return f"行业强于大盘 +{avg_rs:.1f}%（顺风）"
    if avg_rs > 0:
        return f"行业小幅跑赢 +{avg_rs:.1f}%（中性偏正）"
    if avg_rs > -3:
        return f"行业小幅落后 {avg_rs:.1f}%（中性偏负）"
    return f"行业明显弱于大盘 {avg_rs:.1f}%（逆风）"


def _derive_signal(analysis: str, sd: Dict, rel_str: str) -> tuple[str, float]:
    text = analysis.upper()
    if "TAILWIND" in text:
        return "BUY", 0.55
    if "HEADWIND" in text:
        return "SELL", 0.55

    # Fallback: use relative strength
    if "顺风" in rel_str or "跑赢" in rel_str:
        return "BUY", 0.45
    if "逆风" in rel_str or "落后" in rel_str:
        return "SELL", 0.45
    return "HOLD", 0.40


def _build_key_points(sd: Dict, rel_str: str, signal: str) -> List[str]:
    pts = []
    sec = sd.get("sector", "")
    if sec:
        pts.append(f"行业: {sec}")
    etf = sd.get("etf_sym")
    if etf:
        pts.append(f"对应 ETF: {etf}")
    pts.append(f"板块强弱: {rel_str[:40]}")
    stock_1mo = sd.get("stock_1mo_return")
    if stock_1mo is not None:
        pts.append(f"个股1月涨跌: {stock_1mo:+.1f}%")
    pts.append(f"行业信号: {signal}")
    return pts[:5]


def _template_analysis(symbol: str, sd: Dict, rel_str: str) -> str:
    sector = sd.get("sector", "Unknown")
    verdict = "TAILWIND" if "顺风" in rel_str else ("HEADWIND" if "逆风" in rel_str else "NEUTRAL")
    return (
        f"{symbol} 行业分析（模板）\n"
        f"所属行业: {sector}\n"
        f"行业强弱: {rel_str}\n"
        f"结论: {verdict}"
    )
