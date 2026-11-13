"""
agents/financial/debate.py — 信号争议调解 Agent
================================================
当多个 Agent 出现真实分歧（看涨 vs 看跌）时，
DebateAgent 作为"裁判"对冲突进行分析，输出综合判断。
不独立运行，由 AgentTeam 在检测到分歧时自动触发。

2026-08 领域词汇修正：这个 agent 原本把金融的 BUY/HOLD/SELL 硬编码在
system prompt、信号解析和模板兜底里，但 AgentTeam 对**所有**领域都会在
信号冲突时触发它——实测跑 warehouse 团队（GOOD/WATCH/CONCERN/SEVERE）
时，它往结果里塞了一个 "HOLD"，正是 signal_scheme.py 开头警告过的那种
"借用金融词汇表达完全不同含义"。表决层没被污染（SignalScheme.vote()
会过滤掉不在本领域词表里的信号），但报告界面上会多出一张语义不搭的卡片，
而且裁判意见实际上没参与最终结论 —— 等于白跑一趟。

现在 team.py 会把当前领域的 SignalScheme 通过 data["signal_scheme"]
传进来，本文件所有词汇都从 scheme 里取。不传时默认 FINANCIAL_SCHEME，
所以单独调用 DebateAgent 的既有金融调用方行为完全不变。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..base import BaseAgent, AgentResult
from ..signal_scheme import FINANCIAL_SCHEME, SignalScheme

logger = logging.getLogger(__name__)


class DebateAgent(BaseAgent):

    name        = "debate"
    description = "信号争议调解 — 当多 Agent 信号冲突时自动触发，输出裁判视角"

    _SYSTEM_FINANCIAL = (
        "You are a senior investment committee chair mediating a dispute between "
        "analysts who have conflicting views on a stock. Your role is to:\n"
        "1. Identify the core disagreement\n"
        "2. Evaluate which side has stronger evidence\n"
        "3. Determine the dominant factor (macro vs technical vs fundamental)\n"
        "4. Provide a nuanced resolution that acknowledges both sides\n"
        "5. Conclude with a clear signal: {vocabulary} and the primary reason\n"
        "Be direct. Avoid empty hedging. Make a call."
    )

    _SYSTEM_GENERIC = (
        "You are the senior reviewer mediating a dispute between specialist "
        "analysts who reached conflicting conclusions about the same subject. "
        "Your role is to:\n"
        "1. Identify the core disagreement\n"
        "2. Evaluate which side has stronger evidence\n"
        "3. Determine which dimension dominates the overall assessment\n"
        "4. Provide a nuanced resolution that acknowledges both sides\n"
        "5. Conclude with a clear signal: {vocabulary} and the primary reason\n"
        "Be direct. Avoid empty hedging. Make a call."
    )

    def _system_prompt(self, scheme: SignalScheme) -> str:
        # 只有金融领域保留"投资委员会主席"这个具体身份设定；其它领域用中性
        # 措辞——让仓储/地产的裁判自称投资委员会主席，会把 LLM 往错误的
        # 推理框架上带（比如去权衡"要不要建仓"而不是"这个仓库健不健康"）。
        template = self._SYSTEM_FINANCIAL if scheme.name == "financial" else self._SYSTEM_GENERIC
        return template.format(vocabulary=" / ".join(_ranked_vocabulary(scheme)))

    async def fetch_data(self, symbol: str) -> Dict[str, Any]:
        return await super().fetch_data(symbol)

    async def analyze(self, symbol: str, data: Dict[str, Any]) -> AgentResult:
        conflicting: List[Dict] = data.get("conflicting", [])
        scheme: SignalScheme = data.get("signal_scheme") or FINANCIAL_SCHEME

        if not conflicting:
            return AgentResult(
                agent=self.name, symbol=symbol,
                analysis="无冲突结果可调解。",
                confidence=0.3, signal=scheme.neutral_default,
                key_points=["无需调解"],
            )

        debate_block = _format_conflict(conflicting, scheme)
        vocabulary = " / ".join(_ranked_vocabulary(scheme))
        subject_line = f"Stock: {symbol}" if scheme.name == "financial" else f"Subject: {symbol}"
        dominant_q = (
            "What is the dominant factor driving the stock right now? "
            if scheme.name == "financial"
            else "Which dimension dominates the overall assessment right now? "
        )

        prompt = (
            f"{subject_line}\n\n"
            f"Conflicting Analyst Views:\n{debate_block}\n\n"
            "Mediate this dispute. Which view is more compelling and why? "
            f"{dominant_q}"
            f"End with: Signal: {vocabulary} — [primary reason in one line]"
        )

        analysis = await self._call_llm(self._system_prompt(scheme), prompt, max_tokens=3000)
        if not analysis:
            analysis = _template_resolution(symbol, conflicting, scheme)

        signal     = _extract_signal(analysis, scheme)
        confidence = _estimate_confidence(conflicting)
        key_points = _build_key_points(conflicting, analysis, scheme)

        return AgentResult(
            agent=self.name, symbol=symbol,
            analysis=analysis,
            confidence=confidence,
            signal=signal,
            key_points=key_points,
            data_used={"conflict_count": len(conflicting)},
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ranked_vocabulary(scheme: SignalScheme) -> List[str]:
    """按分数从高到低列出该领域的信号词，供 prompt 展示。"""
    return [name for name, _ in sorted(scheme.scores.items(), key=lambda kv: -kv[1])]


def _format_conflict(results: List[Dict], scheme: SignalScheme = FINANCIAL_SCHEME) -> str:
    lines = []
    for r in results:
        agent   = r.get("agent", "unknown")
        signal  = r.get("signal", scheme.neutral_default)
        conf    = r.get("confidence", 0)
        pts     = r.get("key_points", [])
        summary = "; ".join(pts[:3]) if pts else r.get("analysis", "")[:150]
        lines.append(
            f"  [{agent.upper()}] Signal: {signal} (conf {conf:.0%})\n"
            f"    Key points: {summary}"
        )
    return "\n".join(lines)


def _extract_signal(analysis: str, scheme: SignalScheme = FINANCIAL_SCHEME) -> str:
    """从裁判文本里解析出本领域词汇表中的信号。

    长词优先匹配（STRONG_BUY 必须排在 BUY 前面，否则 "STRONG_BUY" 会被
    "BUY" 的前缀判定抢先命中；这是原实现手写四个 if 的顺序想表达的意思，
    改成按长度排序后对任意领域词汇表都成立，不用每加一个领域重写一遍）。
    """
    text = analysis.upper()
    vocabulary = sorted(scheme.scores, key=len, reverse=True)

    for marker in ("SIGNAL: ", "SIGNAL:", "CONCLUSION:", "CONCLUSION: "):
        idx = text.find(marker)
        if idx != -1:
            remainder = text[idx + len(marker):].strip()
            for name in vocabulary:
                if remainder.startswith(name.upper()):
                    return name

    # 没有显式 marker 时的兜底：只在文本里恰好出现一个方向的词汇时才采信，
    # 出现多个方向说明裁判没给出明确结论，退回中性值而不是猜。
    positives = [s for s in scheme.positive_signals if s.upper() in text]
    negatives = [s for s in scheme.negative_signals if s.upper() in text]
    if positives and not negatives:
        return max(positives, key=lambda s: scheme.scores.get(s, 0))
    if negatives and not positives:
        return min(negatives, key=lambda s: scheme.scores.get(s, 0))
    return scheme.neutral_default


def _estimate_confidence(results: List[Dict]) -> float:
    if not results:
        return 0.4
    confs = [r.get("confidence", 0.5) for r in results if r.get("confidence")]
    avg   = sum(confs) / len(confs) if confs else 0.5
    return round(min(avg * 0.9, 0.75), 2)


def _side_labels(scheme: SignalScheme) -> tuple[str, str]:
    """(正面阵营, 负面阵营) 的中文标签。金融叫多空，其它领域这么叫没有意义
    ——仓储团队里没有"看跌方"，只有"报告健康"和"报告有问题"的两拨 agent。"""
    if scheme.name == "financial":
        return "看涨方", "看跌方"
    return "正面评估", "负面评估"


def _split_sides(results: List[Dict], scheme: SignalScheme) -> tuple[List[Dict], List[Dict]]:
    positive = [r for r in results if r.get("signal") in scheme.positive_signals]
    negative = [r for r in results if r.get("signal") in scheme.negative_signals]
    return positive, negative


def _build_key_points(
    results: List[Dict], analysis: str, scheme: SignalScheme = FINANCIAL_SCHEME
) -> List[str]:
    positive, negative = _split_sides(results, scheme)
    pos_label, neg_label = _side_labels(scheme)
    points  = []
    if positive:
        points.append(f"{pos_label}: {', '.join(r['agent'] for r in positive)}")
    if negative:
        points.append(f"{neg_label}: {', '.join(r['agent'] for r in negative)}")
    points.append("DebateAgent 已介入调解")
    points.append(f"裁判结论: {_extract_signal(analysis, scheme)}")
    return points


def _template_resolution(
    symbol: str, results: List[Dict], scheme: SignalScheme = FINANCIAL_SCHEME
) -> str:
    positive, negative = _split_sides(results, scheme)
    pos_label, neg_label = _side_labels(scheme)

    best = max(scheme.scores, key=lambda s: scheme.scores[s])
    worst = min(scheme.scores, key=lambda s: scheme.scores[s])
    if len(positive) > len(negative):
        resolution = f"{best} — 多数{pos_label}信号占优"
    elif len(negative) > len(positive):
        resolution = f"{worst} — 多数{neg_label}信号占优"
    else:
        resolution = f"{scheme.neutral_default} — 双方力量均衡，建议观望"
    return (
        f"{symbol} 信号冲突调解报告\n"
        f"{pos_label}: {', '.join(r['agent'] for r in positive) or '无'}\n"
        f"{neg_label}: {', '.join(r['agent'] for r in negative) or '无'}\n"
        f"裁判结论: Signal: {resolution}"
    )
