"""
agents/financial/macro.py — 宏观环境 Agent
===========================================
分析：利率环境、汇率、行业周期、政策面、大盘趋势。
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..base import BaseAgent, AgentResult


class MacroAgent(BaseAgent):
    name        = "macro"
    description = "宏观分析：利率/汇率/政策/大盘趋势"

    _SYSTEM = (
        "You are a macro strategist covering China and global markets. "
        "Focus on: interest rate environment, USD/CNY trend, sector cycle, "
        "regulatory policy, and market sentiment. Be concise and data-driven. "
        "End with: TAILWIND / NEUTRAL / HEADWIND for the given stock."
    )

    async def fetch_data(self, symbol: str) -> Dict[str, Any]:
        data = await super().fetch_data(symbol)
        # 尝试拉取指数数据作为大盘参考
        if self.data:
            for idx in ["000001", "399006"]:  # 上证 + 创业板
                try:
                    q = self.data.quote(idx)
                    if q:
                        data.setdefault("indices", {})[idx] = q.to_dict()
                except Exception:
                    pass
        return data

    async def analyze(self, symbol: str, data: Dict[str, Any]) -> AgentResult:
        quote   = data.get("quote", {})
        indices = data.get("indices", {})

        idx_summary = ""
        for code, q in indices.items():
            name = {"000001": "上证", "399006": "创业板"}.get(code, code)
            idx_summary += f"  {name}: {q.get('price',0):.2f} {q.get('change_pct',0):+.2f}%\n"

        prompt = (
            f"Symbol: {symbol}\n"
            f"Market Indices:\n{idx_summary or '  (unavailable)'}\n\n"
            "Analyze the macroeconomic backdrop for this stock:\n"
            "1. Current rate/liquidity environment (PBOC stance)\n"
            "2. Sector regulatory environment (any recent policies)\n"
            "3. Market sentiment and positioning\n"
            "4. Key macro risks to watch\n"
            "Conclusion: TAILWIND / NEUTRAL / HEADWIND"
        )

        analysis = await self._call_llm(self._SYSTEM, prompt, max_tokens=3000, quote=quote)
        if not analysis:
            analysis = _template_macro(symbol, indices, lang=self.lang)

        signal     = _extract_signal(analysis)
        key_points = _extract_key_points(analysis)

        return AgentResult(
            agent=self.name, symbol=symbol,
            analysis=analysis, confidence=0.65,
            signal=signal, key_points=key_points,
            data_used={"indices": indices},
        )


def _extract_signal(text: str) -> str:
    t = text.upper()
    if "TAILWIND" in t or "看多" in t:   return "BUY"
    if "HEADWIND" in t or "看空" in t:   return "SELL"
    return "HOLD"


def _extract_key_points(text: str) -> List[str]:
    points = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith(("1.", "2.", "3.", "4.", "•", "-", "·")) and len(line) > 5:
            points.append(line.lstrip("1234567890.-•· "))
    return points[:4]


_IDX_NAMES = {"000001": "上证", "399006": "创业板"}

def _template_macro(symbol: str, indices: Dict, lang: str = "zh") -> str:
    """Without a model: the index moves, and nothing claimed beyond them.

    This stated "当前货币政策偏宽松，流动性充裕" (policy loose, liquidity
    ample) as fact — a macro view with no data behind it.
    """
    zh = str(lang).lower().startswith("zh")
    idx_str = ("、" if zh else ", ").join(
        f"{_IDX_NAMES.get(k, k) if zh else k} {v.get('change_pct', 0):+.2f}%"
        for k, v in indices.items()
    ) if indices else ("指数数据不可用" if zh else "index data unavailable")
    if not zh:
        return (
            f"{symbol} macro (template):\n"
            f"• Market indices: {idx_str}\n"
            "• Policy and liquidity were not assessed (no model analysis)\n"
            "• Conclusion: NEUTRAL"
        )
    return (
        f"{symbol} 宏观分析（模板）:\n"
        f"• 市场指数: {idx_str}\n"
        "• 未评估货币政策与流动性（无模型分析）\n"
        "• 结论: NEUTRAL"
    )
