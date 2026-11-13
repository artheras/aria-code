"""
agents/financial/technical.py — 技术分析 Agent
===============================================
分析 K线形态、均线结构、MACD/RSI、布林带、关键价位。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..base import BaseAgent, AgentResult

logger = logging.getLogger(__name__)


class TechnicalAgent(BaseAgent):

    name        = "technical"
    description = "技术分析：图形形态、动量指标、关键价位"

    _SYSTEM = (
        "You are a quantitative technical analyst. Analyze price action, "
        "chart patterns, momentum indicators (RSI, MACD), moving averages, "
        "and Bollinger Bands. Provide concise, data-driven insights. "
        "Conclude with a clear directional bias: BULLISH / NEUTRAL / BEARISH."
    )

    async def fetch_data(self, symbol: str) -> Dict[str, Any]:
        data = await super().fetch_data(symbol)
        if self.data:
            try:
                h = self.data.history(symbol, days=120)
                if h and h.data is not None:
                    df = h.data
                    data["history"] = _compute_indicators(df)
            except Exception as e:
                logger.debug(f"[technical] fetch history {symbol}: {e}")
        return data

    async def analyze(self, symbol: str, data: Dict[str, Any]) -> AgentResult:
        quote   = data.get("quote", {})
        history = data.get("history", {})
        price   = quote.get("price", 0)

        # 提取指标用于 prompt
        indicators = _format_indicators(history)
        pattern    = history.get("pattern", "无特殊形态")

        prompt = (
            f"Stock: {symbol}  Current Price: {price}\n"
            f"Technical Indicators:\n{indicators}\n"
            f"Pattern: {pattern}\n\n"
            "Provide: 1) Trend assessment 2) Key support/resistance levels "
            "3) Signal strength 4) Short-term outlook (3-10 days) "
            "5) Conclusion: BULLISH / NEUTRAL / BEARISH"
        )

        analysis = await self._call_llm(self._SYSTEM, prompt, max_tokens=3000, quote=quote)
        if not analysis:
            analysis = _template_analysis(symbol, price, history, lang=self.lang)

        signal     = _extract_signal(analysis, history)
        confidence = history.get("signal_strength", 0.5)
        key_points = _extract_key_points(history, price, lang=self.lang)

        return AgentResult(
            agent=self.name, symbol=symbol,
            analysis=analysis, confidence=confidence,
            signal=signal, key_points=key_points,
            data_used={"price": price, "indicators": history},
        )


# ── 技术指标计算 ──────────────────────────────────────────────────────────────

def _compute_indicators(df) -> Dict[str, Any]:
    """从 OHLCV DataFrame 计算常用指标"""
    try:
        import numpy as np
        close = df["close"].values if "close" in df.columns else df.iloc[:, -1].values

        # MA
        ma5  = float(np.mean(close[-5:]))  if len(close) >= 5  else 0
        ma20 = float(np.mean(close[-20:])) if len(close) >= 20 else 0
        ma60 = float(np.mean(close[-60:])) if len(close) >= 60 else 0

        # RSI(14)
        delta = np.diff(close[-15:])
        gains = np.where(delta > 0, delta, 0)
        losses= np.where(delta < 0, -delta, 0)
        rs    = np.mean(gains[-14:]) / (np.mean(losses[-14:]) + 1e-9)
        rsi   = round(100 - 100 / (1 + rs), 1)

        # MACD
        ema12 = _ema(close, 12)
        ema26 = _ema(close, 26)
        macd  = ema12 - ema26
        signal= _ema_arr(macd[-50:] if len(macd) >= 50 else macd, 9)
        macd_val  = round(float(macd[-1]), 4)
        signal_val= round(float(signal[-1]), 4)
        hist_val  = round(macd_val - signal_val, 4)

        # 布林带
        std20 = float(np.std(close[-20:])) if len(close) >= 20 else 0
        bb_up = round(ma20 + 2 * std20, 2)
        bb_lo = round(ma20 - 2 * std20, 2)

        # 信号强度
        price   = float(close[-1])
        ma_bull = price > ma5 > ma20
        strength = 0.5
        if ma_bull and macd_val > signal_val:
            strength = 0.75
        elif not ma_bull and macd_val < signal_val:
            strength = 0.25

        # 形态检测（简单）
        pattern = _detect_simple_pattern(close)

        return {
            "price":   round(price, 2),
            "ma5":     round(ma5, 2),
            "ma20":    round(ma20, 2),
            "ma60":    round(ma60, 2),
            "rsi":     rsi,
            "macd":    macd_val,
            "macd_signal": signal_val,
            "macd_hist":   hist_val,
            "bb_upper":    bb_up,
            "bb_lower":    bb_lo,
            "signal_strength": strength,
            "pattern": pattern,
        }
    except Exception as e:
        logger.debug(f"compute_indicators 失败: {e}")
        return {}


def _ema(arr, period):
    import numpy as np
    k = 2 / (period + 1)
    ema = np.zeros(len(arr))
    ema[0] = arr[0]
    for i in range(1, len(arr)):
        ema[i] = arr[i] * k + ema[i-1] * (1 - k)
    return ema

def _ema_arr(arr, period):
    return _ema(arr, period)

def _detect_simple_pattern(close) -> str:
    if len(close) < 3:
        return "数据不足"
    o, h, c = close[-3], close[-2], close[-1]
    if c > o and c > h and (c - h) > abs(c - o) * 2:
        return "锤子线"
    if c > o and o < close[-4] and c > close[-4]:
        return "阳线吞噬"
    if abs(c - o) / (max(close[-3:]) - min(close[-3:]) + 1e-9) < 0.1:
        return "十字星"
    return "无特殊形态"


def _format_indicators(history: Dict) -> str:
    if not history:
        return "  (无指标数据)"
    return (
        f"  MA5={history.get('ma5',0):.2f}  MA20={history.get('ma20',0):.2f}  "
        f"MA60={history.get('ma60',0):.2f}\n"
        f"  RSI={history.get('rsi',50):.1f}  "
        f"MACD={history.get('macd',0):.4f} Signal={history.get('macd_signal',0):.4f}\n"
        f"  BB Upper={history.get('bb_upper',0):.2f}  BB Lower={history.get('bb_lower',0):.2f}"
    )


def _extract_signal(analysis: str, history: Dict) -> str:
    import re as _re
    text = analysis.upper()

    # Unambiguous strong signals always win
    if "STRONG_BUY" in text or "强烈买入" in text:
        return "STRONG_BUY"
    if "STRONG_SELL" in text or "强烈卖出" in text:
        return "STRONG_SELL"

    # The LLM prompt asks the model to conclude with BULLISH/NEUTRAL/BEARISH.
    # Use the LAST occurrence — it's the conclusion, not mid-text context like
    # "a potential BULLISH reversal" appearing before "currently BEARISH".
    matches = _re.findall(r'\b(BULLISH|BEARISH|NEUTRAL)\b', text)
    if matches:
        last = matches[-1]
        if last == "BULLISH":
            return "BUY"
        if last == "BEARISH":
            return "SELL"
        # NEUTRAL → fall through to quantitative check below

    # Explicit Chinese directional signals
    if "看多" in analysis:
        return "BUY"
    if "看空" in analysis:
        return "SELL"

    # Quantitative fallback: score RSI + MACD crossover
    rsi  = history.get("rsi", 50)
    macd = history.get("macd", 0)
    msig = history.get("macd_signal", 0)
    score = 0
    if rsi < 30:    score += 2   # oversold — strong mean-reversion signal
    elif rsi < 45:  score -= 1
    elif rsi > 70:  score -= 2   # overbought
    elif rsi > 55:  score += 1
    if macd > msig: score += 1
    elif macd < msig: score -= 1
    if score >= 2:  return "BUY"
    if score <= -2: return "SELL"
    return "HOLD"


# Pattern values stay as _detect_simple_pattern returns them; this is how
# they read in English.
_PATTERN_EN = {"锤子线": "hammer", "阳线吞噬": "bullish engulfing", "十字星": "doji", "数据不足": "not enough data"}


def _extract_key_points(history: Dict, price: float, lang: str = "zh") -> List[str]:
    """The analyst's one-line findings, in the team's language.

    They were Chinese only, so an English /team listed "距MA20 -0.5%" and
    "MACD死叉，空头压力" under "Technical".
    """
    zh = str(lang).lower().startswith("zh")
    points = []
    rsi = history.get("rsi", 50)
    ma20 = history.get("ma20", 0)
    if rsi < 35:
        points.append(f"RSI超卖({rsi:.0f})，有反弹机会" if zh else f"RSI oversold ({rsi:.0f}), room to rebound")
    elif rsi > 70:
        points.append(f"RSI超买({rsi:.0f})，注意回调风险" if zh else f"RSI overbought ({rsi:.0f}), pullback risk")
    if ma20 > 0:
        diff_pct = (price - ma20) / ma20 * 100
        points.append(f"距MA20 {diff_pct:+.1f}%" if zh else f"{diff_pct:+.1f}% vs MA20")
    macd = history.get("macd", 0)
    sig  = history.get("macd_signal", 0)
    if macd > sig and history.get("macd_hist", 0) > 0:
        points.append("MACD金叉，多头动能" if zh else "MACD bullish crossover")
    elif macd < sig:
        points.append("MACD死叉，空头压力" if zh else "MACD bearish crossover")
    pattern = history.get("pattern", "")
    if pattern and pattern != "无特殊形态":
        points.append(f"K线形态: {pattern}" if zh else f"Candle pattern: {_PATTERN_EN.get(pattern, pattern)}")
    return points


def _template_analysis(symbol: str, price: float, history: Dict, lang: str = "zh") -> str:
    rsi  = history.get("rsi", 50)
    ma20 = history.get("ma20", 0)
    macd = history.get("macd", 0)
    sig  = history.get("macd_signal", 0)
    if not str(lang).lower().startswith("zh"):
        up, bull = price > ma20, macd > sig
        return (
            f"{symbol} technicals (template):\n"
            f"• Price {price}, {'uptrend' if up else 'downtrend'} (MA20={ma20:.2f})\n"
            f"• RSI={rsi:.0f}, {'oversold' if rsi < 35 else 'overbought' if rsi > 70 else 'normal range'}\n"
            f"• MACD momentum {'bullish' if bull else 'bearish'}\n"
            f"• Overall {'BULLISH' if bull and up else 'NEUTRAL'}"
        )
    trend = "上升趋势" if price > ma20 else "下降趋势"
    momentum = "偏多" if macd > sig else "偏空"
    return (
        f"{symbol} 技术面分析（模板）:\n"
        f"• 当前价格 {price}，处于 {trend}（MA20={ma20:.2f}）\n"
        f"• RSI={rsi:.0f}，{'超卖' if rsi<35 else '超买' if rsi>70 else '正常区间'}\n"
        f"• MACD 动能{momentum}\n"
        f"• 整体技术面{'BULLISH' if momentum=='偏多' and trend=='上升趋势' else 'NEUTRAL'}"
    )
