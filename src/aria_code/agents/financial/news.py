"""
agents/financial/news.py — 新闻与舆情分析 Agent
================================================
分析近期新闻标题、公告和媒体情绪，识别关键事件类型。
数据源：yfinance.news（美股/港股）、akshare公告（A股，可选）
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from ..base import BaseAgent, AgentResult

logger = logging.getLogger(__name__)

_EVENT_KEYWORDS = {
    "earnings":    ["earnings", "revenue", "profit", "EPS", "业绩", "净利润", "营收"],
    "upgrade":     ["upgrade", "outperform", "buy", "overweight", "上调", "买入"],
    "downgrade":   ["downgrade", "underperform", "sell", "underweight", "下调", "卖出"],
    "insider":     ["insider", "director", "CEO", "增持", "减持", "大股东"],
    "regulatory":  ["SEC", "CSRC", "证监会", "regulatory", "investigation", "调查"],
    "dividend":    ["dividend", "分红", "派息", "股息"],
    "merger":      ["merger", "acquisition", "takeover", "合并", "收购"],
}


class NewsAgent(BaseAgent):

    name        = "news"
    description = "新闻与舆情分析 — 近期重大事件、公告、媒体情绪"

    _SYSTEM = (
        "You are a financial news analyst. Analyze the provided news headlines "
        "and summaries for a stock. Identify the most impactful recent events, "
        "classify sentiment (POSITIVE / NEUTRAL / NEGATIVE), and assess likely "
        "price impact. Focus on material events: earnings, analyst changes, "
        "regulatory actions, M&A, and insider activity. "
        "Conclude with: POSITIVE / NEUTRAL / NEGATIVE"
    )

    async def fetch_data(self, symbol: str) -> Dict[str, Any]:
        data = await super().fetch_data(symbol)
        news_items: List[Dict] = []

        # 1. yfinance news (works for US / HK stocks)
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            raw = ticker.news or []
            now = datetime.now(timezone.utc).timestamp()
            for item in raw[:12]:
                pub = item.get("providerPublishTime", 0)
                age_days = (now - pub) / 86400 if pub else 999
                news_items.append({
                    "title":    item.get("title", ""),
                    "summary":  item.get("summary", "") or item.get("description", ""),
                    "source":   item.get("publisher", ""),
                    "age_days": round(age_days, 1),
                })
        except Exception as e:
            logger.debug("[news] yfinance fetch failed for %s: %s", symbol, e)

        # 2. akshare A-share announcements (optional)
        if not news_items and re.match(r"^[036]\d{5}$", symbol):
            try:
                import akshare as ak
                df = ak.stock_news_em(symbol=symbol)
                if df is not None and not df.empty:
                    for _, row in df.head(10).iterrows():
                        news_items.append({
                            "title":    str(row.get("新闻标题", "")),
                            "summary":  str(row.get("新闻内容", ""))[:200],
                            "source":   str(row.get("新闻来源", "")),
                            "age_days": 0,
                        })
            except Exception as e:
                logger.debug("[news] akshare fetch failed for %s: %s", symbol, e)

        data["news"] = news_items
        return data

    async def analyze(self, symbol: str, data: Dict[str, Any]) -> AgentResult:
        news  = data.get("news", [])
        quote = data.get("quote", {})
        price = quote.get("price", 0)

        if not news:
            return AgentResult(
                agent=self.name, symbol=symbol,
                analysis=(f"{symbol}: 未获取到近期新闻数据。" if str(self.lang).startswith("zh")
                          else f"{symbol}: no recent news."),
                confidence=0.3, signal="HOLD",
                key_points=["无近期新闻数据" if str(self.lang).startswith("zh") else "no recent news"],
            )

        events = _classify_events(news)
        news_block = _format_news(news[:8])

        prompt = (
            f"Stock: {symbol}  Price: {price}\n\n"
            f"Recent News ({len(news)} items):\n{news_block}\n\n"
            "Tasks:\n"
            "1. Identify the 2-3 most impactful recent events\n"
            "2. Classify overall media sentiment\n"
            "3. Assess potential price catalyst (short-term, 1-5 days)\n"
            "4. Conclude with: POSITIVE / NEUTRAL / NEGATIVE"
        )

        analysis = await self._call_llm(self._SYSTEM, prompt, max_tokens=3000)
        if not analysis:
            analysis = _template_analysis(symbol, news, events, lang=self.lang)

        signal     = _extract_signal(analysis, events)
        confidence = _estimate_confidence(news, events)
        key_points = _build_key_points(news, events, lang=self.lang)

        return AgentResult(
            agent=self.name, symbol=symbol,
            analysis=analysis,
            confidence=confidence,
            signal=signal,
            key_points=key_points,
            data_used={"news_count": len(news), "events": list(events.keys())},
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _classify_events(news: List[Dict]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in news:
        text = (item.get("title", "") + " " + item.get("summary", "")).lower()
        for event_type, keywords in _EVENT_KEYWORDS.items():
            if any(kw.lower() in text for kw in keywords):
                counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _format_news(news: List[Dict]) -> str:
    lines = []
    for i, item in enumerate(news, 1):
        age = f"{item['age_days']:.0f}d ago" if item.get("age_days", 999) < 30 else ""
        src = item.get("source", "")
        title = item.get("title", "").strip()
        lines.append(f"{i}. [{src}] {title} {age}".strip())
    return "\n".join(lines)


def _extract_signal(analysis: str, events: Dict[str, int]) -> str:
    text = analysis.upper()
    if "POSITIVE" in text:
        if events.get("upgrade", 0) > 0 or "STRONG" in text:
            return "BUY"
        return "BUY"
    if "NEGATIVE" in text:
        if events.get("downgrade", 0) > 0 or events.get("regulatory", 0) > 0:
            return "SELL"
        return "SELL"
    return "HOLD"


def _estimate_confidence(news: List[Dict], events: Dict[str, int]) -> float:
    base = 0.45
    recent = sum(1 for n in news if n.get("age_days", 999) <= 3)
    base += min(recent * 0.05, 0.15)
    if events.get("earnings") or events.get("upgrade") or events.get("downgrade"):
        base += 0.1
    return min(round(base, 2), 0.75)


_EVENT_EN = {"earnings": "earnings-related", "upgrade": "analyst upgrades", "downgrade": "analyst downgrades",
             "insider": "insider trades", "regulatory": "regulatory events", "dividend": "dividend news",
             "merger": "M&A news"}


def _build_key_points(news: List[Dict], events: Dict[str, int], lang: str = "zh") -> List[str]:
    zh = str(lang).lower().startswith("zh")
    points = []
    recent = [n for n in news if n.get("age_days", 999) <= 3]
    if recent:
        points.append(f"近3日 {len(recent)} 条新鲜新闻" if zh else f"{len(recent)} stories in the last 3 days")
    for etype, count in events.items():
        if not zh:
            points.append(f"{_EVENT_EN.get(etype, etype)} × {count}")
            continue
        label = {
            "earnings": "业绩/财报相关",
            "upgrade": "分析师升级评级",
            "downgrade": "分析师降级评级",
            "insider": "内部人士交易",
            "regulatory": "监管/调查事件",
            "dividend": "股息/分红公告",
            "merger": "并购/重组消息",
        }.get(etype, etype)
        points.append(f"{label} × {count}")
    return points[:5]


def _template_analysis(symbol: str, news: List[Dict], events: Dict[str, int], lang: str = "zh") -> str:
    recent = [n for n in news if n.get("age_days", 999) <= 7]
    if not str(lang).lower().startswith("zh"):
        tone = "neutral"
        if events.get("upgrade", 0) > events.get("downgrade", 0):
            tone = "positive"
        elif events.get("downgrade", 0) > 0 or events.get("regulatory", 0) > 0:
            tone = "negative"
        titles = "\n".join(f"  • {n['title'][:60]}" for n in recent[:3])
        return (
            f"{symbol} recent news tone: {tone}\n"
            f"{len(recent)} related stories in the last 7 days\n"
            f"Headlines:\n{titles or '  (no recent headlines)'}\n"
            f"Events: {', '.join(events.keys()) or 'none'}\n"
            f"Conclusion: {tone.upper() if tone != 'neutral' else 'NEUTRAL'}"
        )
    sentiment = "中性"
    if events.get("upgrade", 0) > events.get("downgrade", 0):
        sentiment = "偏正面"
    elif events.get("downgrade", 0) > 0 or events.get("regulatory", 0) > 0:
        sentiment = "偏负面"

    titles = "\n".join(f"  • {n['title'][:60]}" for n in recent[:3])
    return (
        f"{symbol} 近期新闻情绪：{sentiment}\n"
        f"近7日 {len(recent)} 条相关新闻\n"
        f"主要标题：\n{titles or '  （无最新标题）'}\n"
        f"事件分类：{', '.join(events.keys()) or '无特定事件'}\n"
        f"结论：{'POSITIVE' if sentiment == '偏正面' else ('NEGATIVE' if sentiment == '偏负面' else 'NEUTRAL')}"
    )
