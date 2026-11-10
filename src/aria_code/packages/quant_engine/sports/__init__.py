"""
packages/quant_engine/sports — 足球量化分析模块
================================================
v2.0.0 — 2026-06

模块组成:
  elo          — World Football Elo 动态评分系统（二次曲线攻防，GD K 因子）
  dixon_coles  — Dixon-Coles 比分预测（NB 分布，大比分悬殊自动启用）
  form         — 近期状态分析（指数衰减权重）
  h2h          — 历史交锋分析与调整
  predictor    — 统一预测引擎 v2（动态权重、赛事情境、ρ 校准）
  tracker      — 预测追踪、Brier Score、自动 Elo 同步、动态场均进球

快速使用:
    from packages.quant_engine.sports import quick_predict, get_elo

    # 预测（含 NB 分布 + 动态权重）
    result = quick_predict("germany", "curacao", league="wc")
    print(f"德国赢率: {result['home_win']*100:.1f}%")   # ~85%

    # Elo 评分
    elo = get_elo()
    print(elo.get_rating("germany"))    # ~1912（赛后更新）

    # 自动同步已结束 WC 比赛 Elo
    from packages.quant_engine.sports import sync_elo_from_wc
    sync_elo_from_wc(api_get_fn)
"""

from .elo          import EloRatingSystem, get_elo, ranking_to_elo
from .dixon_coles  import (
    compute_match_probabilities,
    predict_scoreline_matrix,
    tau_correction,
    estimate_rho_from_results,
    format_dc_result,
)
from .form         import analyze_form, parse_api_results, form_bar, momentum_label
from .h2h          import analyze_h2h
from .predictor    import FootballPredictor, get_predictor, quick_predict
from .tracker      import (
    record_prediction,
    record_result,
    get_accuracy_stats,
    sync_elo_from_wc,
    fetch_wc_league_avg,
    fetch_wc_rho,
    auto_calibrate,
    backfill_score_metrics,
)
from .calibrator   import (
    get_calibrated_params,
    get_team_goal_bias,
    get_all_team_biases,
    calibration_summary,
    save_calibration,
)

__all__ = [
    "EloRatingSystem", "get_elo", "ranking_to_elo",
    "compute_match_probabilities", "predict_scoreline_matrix",
    "tau_correction", "estimate_rho_from_results", "format_dc_result",
    "analyze_form", "parse_api_results", "form_bar", "momentum_label",
    "analyze_h2h",
    "FootballPredictor", "get_predictor", "quick_predict",
    "record_prediction", "record_result", "get_accuracy_stats",
    "sync_elo_from_wc", "fetch_wc_league_avg", "fetch_wc_rho", "auto_calibrate",
    "get_calibrated_params", "get_team_goal_bias", "get_all_team_biases",
    "calibration_summary", "save_calibration",
]

__version__ = "2.0.0"

# Optional explicit backend modules never override this distribution.
from .._namespace import extend as _extend_namespace
_extend_namespace(__path__, "sports")
