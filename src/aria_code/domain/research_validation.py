"""Independent Research & Backtest Validation Gate.

Separates backtest execution (generating return series) from validation
(auditing statistical significance, overfitting risk, PIT data integrity,
and RiskPolicy compliance) using pure-Python math so CLI startup remains fast.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from aria_code.domain.risk_policy import RiskPolicy, check_order_compliance

_EULER_GAMMA = 0.5772156649015329


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Acklam's rational approximation for the inverse standard normal CDF."""
    if not (0.0 < p < 1.0):
        raise ValueError(f"p must be in (0, 1), got {p}")
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00)
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
    )


def expected_max_sharpe(n_trials: int, sharpe_std: float = 1.0) -> float:
    """Expected maximum Sharpe ratio under N independent random trials."""
    if n_trials <= 1:
        return 0.0
    z1 = _norm_ppf(1.0 - 1.0 / n_trials)
    z2 = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return sharpe_std * ((1.0 - _EULER_GAMMA) * z1 + _EULER_GAMMA * z2)


def deflated_sharpe_ratio(
    sharpe: float,
    n_obs: int,
    *,
    n_trials: int = 1,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    sharpe_std: float = 0.5,
    periods_per_year: int = 252,
) -> float:
    """Probability that observed annualized Sharpe exceeds the multiple-testing hurdle (DSR in [0, 1])."""
    if n_obs <= 1:
        return 0.0
    scale = math.sqrt(max(1, periods_per_year))
    sr_period = sharpe / scale
    sr0_period = expected_max_sharpe(max(1, n_trials), sharpe_std=sharpe_std) / scale
    variance = max(1e-9, (1.0 - skew * sr_period + ((kurtosis - 1.0) / 4.0) * (sr_period ** 2)) / (n_obs - 1.0))
    z = (sr_period - sr0_period) / math.sqrt(variance)
    return round(_norm_cdf(z), 6)


def minimum_backtest_length_years(
    target_sharpe: float,
    *,
    n_trials: int = 1,
    periods_per_year: int = 252,
) -> float:
    """Minimum backtest length (in years) needed to avoid false discovery at 95% confidence."""
    if target_sharpe <= 0:
        return float("inf")
    sr0 = expected_max_sharpe(max(1, n_trials), sharpe_std=0.5)
    if target_sharpe <= sr0:
        return float("inf")
    # Daily Sharpe conversion when annual Sharpe is provided.
    sr_daily = target_sharpe / math.sqrt(periods_per_year)
    sr0_daily = sr0 / math.sqrt(periods_per_year)
    z_alpha = _norm_ppf(0.95)
    min_obs = 1.0 + (z_alpha / max(1e-9, sr_daily - sr0_daily)) ** 2
    return round(min_obs / periods_per_year, 2)


@dataclass
class ResearchValidationReport:
    passed: bool
    verdict: str  # "approved" | "conditional" | "rejected"
    dsr: float
    min_btl_years: float
    oos_retention: float
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def validate_research_backtest(
    *,
    is_sharpe: float,
    oos_sharpe: float,
    n_obs: int,
    n_trials: int = 1,
    max_drawdown_pct: float = 0.0,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    pit_passed: bool = True,
    risk_policy: Optional[RiskPolicy] = None,
    target_weight: float = 0.0,
    dsr_threshold: float = 0.95,
    min_oos_retention: float = 0.40,
) -> ResearchValidationReport:
    """Audit a completed backtest independently of the strategy/engine that ran it."""
    reasons: List[str] = []
    warnings: List[str] = []

    if not pit_passed:
        reasons.append("Data failed Point-in-Time / quality gate.")
    if n_obs < 60:
        reasons.append(f"Insufficient sample length ({n_obs} < 60 observations).")
    if oos_sharpe <= 0:
        reasons.append(f"Out-of-sample Sharpe must be positive (got {oos_sharpe:.3f}).")

    oos_retention = round(oos_sharpe / is_sharpe, 4) if is_sharpe > 0 else 0.0
    if is_sharpe > 0 and oos_retention < min_oos_retention:
        reasons.append(
            f"Severe IS->OOS degradation (retention {oos_retention:.2f} < {min_oos_retention:.2f})."
        )

    dsr = deflated_sharpe_ratio(
        oos_sharpe,
        n_obs,
        n_trials=n_trials,
        skew=skew,
        kurtosis=kurtosis,
    )
    if dsr < dsr_threshold:
        reasons.append(f"Deflated Sharpe Ratio {dsr:.3f} below threshold {dsr_threshold:.2f} (n_trials={n_trials}).")

    min_btl = minimum_backtest_length_years(oos_sharpe, n_trials=n_trials)
    if risk_policy is not None:
        compliance = check_order_compliance(
            risk_policy,
            target_weight=target_weight,
            current_drawdown_pct=max_drawdown_pct,
        )
        reasons.extend(compliance.violations)
        warnings.extend(compliance.warnings)

    if reasons:
        verdict = "rejected"
    elif warnings:
        verdict = "conditional"
    else:
        verdict = "approved"

    return ResearchValidationReport(
        passed=not reasons,
        verdict=verdict,
        dsr=dsr,
        min_btl_years=min_btl,
        oos_retention=oos_retention,
        reasons=reasons,
        warnings=warnings,
    )
