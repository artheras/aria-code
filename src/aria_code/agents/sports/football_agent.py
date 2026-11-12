"""
agents/sports/football_agent.py — Football Analysis Agent
===========================================================
LLM-powered football match analysis using Elo + Dixon-Coles prediction + form data.

Prediction comes from packages.quant_engine.sports.predictor (Elo ratings,
Dixon-Coles with negative-binomial tail for lopsided matches, dynamic
Elo/DC mixing weights, recency-form and head-to-head adjustments, and
self-calibration), not football_data_client.predict_match — that function
is a much simpler independent-Poisson model over a static, hand-maintained
attack/defense lookup table with a flat home-advantage constant, no
Dixon-Coles low-score correlation correction, and no Elo. Verified the two
disagree by double digits on some matchups (13.1pp on a real fixture) rather
than assuming the richer model is worth switching to on faith.

football_data_client itself is left untouched — aria_feishu_bot.py calls
predict_match directly and has its own tested formatting/localization
pinned to that function's exact output, which is out of scope here. Only
get_team_stats() (recent-form text, not part of either prediction model)
is still sourced from football_data_client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MatchPrediction:
    home_team:    str
    away_team:    str
    league:       str
    home_win:     float
    draw:         float
    away_win:     float
    btts:         float
    lambda_home:  float
    lambda_away:  float
    most_likely:  str
    top_scores:   List[Dict]
    implied_odds: Dict[str, float]
    analysis:     str = ""
    key_factors:  List[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        probs = {"home": self.home_win, "draw": self.draw, "away": self.away_win}
        winner = max(probs, key=probs.get)
        dominant = probs[winner] > 0.50
        if winner == "home":
            return f"主队{'强势' if dominant else '略有'}优势 ({self.home_win:.0%})"
        elif winner == "away":
            return f"客队{'强势' if dominant else '略有'}优势 ({self.away_win:.0%})"
        else:
            return f"势均力敌，平局概率较高 ({self.draw:.0%})"


class FootballAgent:
    """
    Football analysis combining Poisson prediction + LLM interpretation.
    Can run standalone (no LLM) or enhanced with LLM narrative.
    """

    name = "football"
    description = "足球赛事分析与预测 — 泊松模型 + 近期战绩 + xG数据"

    def __init__(self, llm_call=None):
        self._llm = llm_call  # Optional: async fn(prompt) -> str

    async def predict(
        self,
        home_team: str,
        away_team: str,
        league: str = "pl",
        with_llm: bool = True,
        neutral_venue: bool = False,
    ) -> MatchPrediction:
        from aria_code.clients.football_data_client import get_team_stats
        from packages.quant_engine.sports.predictor import quick_predict

        # neutral_venue defaults False: club leagues (pl/bl1/sa/pd/fl1, the
        # default `league="pl"`) really do have a home team playing at home.
        # Pass True for a genuinely neutral-site fixture (e.g. a World Cup
        # group match at a third country's stadium).
        raw = quick_predict(home_team, away_team, league, neutral_venue=neutral_venue)
        raw["most_likely_score"] = (
            raw["top_scorelines"][0]["score"] if raw.get("top_scorelines") else "0-0"
        )
        h_stats = get_team_stats(league, home_team)
        a_stats = get_team_stats(league, away_team)

        key_factors = []
        if h_stats:
            key_factors.append(f"{home_team} 近5场: {h_stats['form']} (场均进球 {h_stats['avg_gf']})")
        if a_stats:
            key_factors.append(f"{away_team} 近5场: {a_stats['form']} (场均进球 {a_stats['avg_gf']})")
        key_factors.append(
            f"模型: {raw['model']}  Elo差: {raw['elo_diff']:+.0f}"
            f"  (DC权重{raw['w_dc']:.0%}/Elo权重{raw['w_elo']:.0%})"
        )

        analysis = ""
        if with_llm and self._llm:
            prompt = _build_analysis_prompt(raw, h_stats, a_stats)
            try:
                analysis = await self._llm(prompt)
            except Exception as exc:
                logger.warning("LLM analysis failed: %s", exc)
                analysis = _fallback_analysis(raw)
        else:
            analysis = _fallback_analysis(raw)

        return MatchPrediction(
            home_team=home_team,
            away_team=away_team,
            league=league,
            home_win=raw["home_win"],
            draw=raw["draw"],
            away_win=raw["away_win"],
            btts=raw["btts"],
            lambda_home=raw["lambda_home"],
            lambda_away=raw["lambda_away"],
            most_likely=raw["most_likely_score"],
            top_scores=raw["top_scorelines"],
            implied_odds=raw["implied_odds"],
            analysis=analysis,
            key_factors=key_factors,
        )


def _fallback_analysis(raw: Dict) -> str:
    hw, d, aw = raw["home_win"], raw["draw"], raw["away_win"]
    lh, la = raw["lambda_home"], raw["lambda_away"]
    ht, at = raw["home_team"], raw["away_team"]

    lines = []
    if hw > aw + 0.15:
        lines.append(f"泊松模型显示 **{ht}** 主场优势明显（胜率 {hw:.0%}）")
    elif aw > hw + 0.15:
        lines.append(f"泊松模型显示 **{at}** 客场表现更强（胜率 {aw:.0%}）")
    else:
        lines.append(f"双方实力相当，平局可能性较大（{d:.0%}）")

    lines.append(f"预期进球：{ht} {lh:.1f} / {at} {la:.1f}")
    top = raw["top_scorelines"][:3]
    score_str = " | ".join(f"{s['score']}({s['prob']}%)" for s in top)
    lines.append(f"高概率比分：{score_str}")
    btts = raw["btts"]
    lines.append(f"双方均进球概率：{btts:.0%}")

    return "\n".join(lines)


def _build_analysis_prompt(raw: Dict, h_stats: Optional[Dict], a_stats: Optional[Dict]) -> str:
    top_scores = "、".join(
        f"{s['score']}({s['prob']}%)"
        for s in raw.get("top_scorelines", [])[:5]
    )
    return f"""你是一位专业足球分析师。根据以下泊松预测模型数据，用中文分析这场比赛：

【比赛】{raw['home_team']} vs {raw['away_team']}

【预测概率】
主队胜: {raw['home_win']:.1%}  平局: {raw['draw']:.1%}  客队胜: {raw['away_win']:.1%}
预期进球: 主队 {raw['lambda_home']:.2f} / 客队 {raw['lambda_away']:.2f}
最可能比分: {raw['most_likely_score']}
候选比分（按模型概率降序）: {top_scores}
双方均进球: {raw['btts']:.1%}

【近期战绩】
{_format_stats(raw['home_team'], h_stats)}
{_format_stats(raw['away_team'], a_stats)}

请提供:
1. 比赛走势分析（3-4句）
2. 关键影响因素（2-3条）
3. 预测建议（1句话结论）

规则:
- 必须按“候选比分”概率顺序讨论比分，不要把低概率比分排到第一。
- 不要编造射正率、历史交锋、最近5场客场等输入数据之外的具体数字。
- 如果给出多个准确比分，直接引用候选比分及概率。

简洁专业，不超过200字。"""


def _format_stats(team: str, stats: Optional[Dict]) -> str:
    if not stats:
        return f"{team}: 数据不足"
    return (
        f"{team}: 近{stats['last_n']}场 {stats['w']}胜{stats['d']}平{stats['l']}负, "
        f"进{stats['gf']}球失{stats['ga']}球, 近5场战绩: {stats['form']}"
    )
