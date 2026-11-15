use serde::{Deserialize, Serialize};

use crate::black_scholes::norm_cdf;
use crate::risk_policy::{check_order_compliance, RiskPolicy};

const EULER_GAMMA: f64 = 0.577_215_664_901_532_9;

/// Acklam's rational approximation for the standard normal inverse CDF.
#[allow(clippy::excessive_precision)]
pub fn norm_ppf(p: f64) -> f64 {
    if p <= 0.0 || p >= 1.0 {
        panic!("p must be in (0, 1)");
    }
    let a = [
        -3.969683028665376e+01,
        2.209460984245205e+02,
        -2.759285104469687e+02,
        1.383577518672690e+02,
        -3.066479806614716e+01,
        2.506628277459239e+00,
    ];
    let b = [
        -5.447609879822406e+01,
        1.615858368580409e+02,
        -1.556989798598866e+02,
        6.680131188771972e+01,
        -1.328068155288572e+01,
    ];
    let c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e+00,
        -2.549732539343734e+00,
        4.374664141464968e+00,
        2.938163982698783e+00,
    ];
    let d = [
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e+00,
        3.754408661907416e+00,
    ];

    let p_low = 0.02425;
    let p_high = 1.0 - p_low;

    if p < p_low {
        let q = (-2.0 * p.ln()).sqrt();
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0);
    }
    if p <= p_high {
        let q = p - 0.5;
        let r = q * q;
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0);
    }
    let q = (-2.0 * (1.0 - p).ln()).sqrt();
    -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
        / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
}

/// Expected maximum Sharpe ratio under N independent random trials.
pub fn expected_max_sharpe(n_trials: usize, sharpe_std: f64) -> f64 {
    if n_trials <= 1 {
        return 0.0;
    }
    let n = n_trials as f64;
    let z1 = norm_ppf(1.0 - 1.0 / n);
    let z2 = norm_ppf(1.0 - 1.0 / (n * std::f64::consts::E));
    sharpe_std * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)
}

/// Probability that observed annualized Sharpe exceeds the multiple-testing hurdle (DSR in [0, 1]).
pub fn deflated_sharpe_ratio(
    sharpe: f64,
    n_obs: usize,
    n_trials: usize,
    skew: f64,
    kurtosis: f64,
    sharpe_std: f64,
    periods_per_year: usize,
) -> f64 {
    if n_obs <= 1 {
        return 0.0;
    }
    let scale = (periods_per_year.max(1) as f64).sqrt();
    let sr_period = sharpe / scale;
    let sr0_period = expected_max_sharpe(n_trials.max(1), sharpe_std) / scale;
    let variance = (1.0 - skew * sr_period + ((kurtosis - 1.0) / 4.0) * sr_period * sr_period)
        / ((n_obs - 1) as f64);
    let z = (sr_period - sr0_period) / variance.max(1e-9).sqrt();
    (norm_cdf(z) * 1_000_000.0).round() / 1_000_000.0
}

/// Minimum backtest length (in years) needed to avoid false discovery at 95% confidence.
pub fn minimum_backtest_length_years(
    target_sharpe: f64,
    n_trials: usize,
    periods_per_year: usize,
) -> f64 {
    if target_sharpe <= 0.0 {
        return f64::INFINITY;
    }
    let sr0 = expected_max_sharpe(n_trials.max(1), 0.5);
    if target_sharpe <= sr0 {
        return f64::INFINITY;
    }
    let scale = (periods_per_year.max(1) as f64).sqrt();
    let sr_daily = target_sharpe / scale;
    let sr0_daily = sr0 / scale;
    let z_alpha = norm_ppf(0.95);
    let min_obs = 1.0 + (z_alpha / (sr_daily - sr0_daily).max(1e-9)).powi(2);
    ((min_obs / (periods_per_year.max(1) as f64)) * 100.0).round() / 100.0
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResearchValidationRequest {
    pub is_sharpe: f64,
    pub oos_sharpe: f64,
    pub n_obs: usize,
    pub n_trials: usize,
    pub max_drawdown_pct: f64,
    pub skew: f64,
    pub kurtosis: f64,
    pub pit_passed: bool,
    pub risk_policy: Option<RiskPolicy>,
    pub target_weight: f64,
    pub dsr_threshold: f64,
    pub min_oos_retention: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResearchValidationReport {
    pub passed: bool,
    pub verdict: String,
    pub dsr: f64,
    pub min_btl_years: f64,
    pub oos_retention: f64,
    pub reasons: Vec<String>,
    pub warnings: Vec<String>,
}

/// Audit a completed backtest independently of the strategy/engine that ran it.
pub fn validate_research_backtest(req: &ResearchValidationRequest) -> ResearchValidationReport {
    let mut reasons = Vec::new();
    let mut warnings = Vec::new();

    if !req.pit_passed {
        reasons.push("Data failed Point-in-Time / quality gate.".to_string());
    }
    if req.n_obs < 60 {
        reasons.push(format!(
            "Insufficient sample length ({} < 60 observations).",
            req.n_obs
        ));
    }
    if req.oos_sharpe <= 0.0 {
        reasons.push(format!(
            "Out-of-sample Sharpe must be positive (got {:.3}).",
            req.oos_sharpe
        ));
    }

    let oos_retention = if req.is_sharpe > 0.0 {
        ((req.oos_sharpe / req.is_sharpe) * 10_000.0).round() / 10_000.0
    } else {
        0.0
    };
    if req.is_sharpe > 0.0 && oos_retention < req.min_oos_retention {
        reasons.push(format!(
            "Severe IS->OOS degradation (retention {:.2} < {:.2}).",
            oos_retention, req.min_oos_retention
        ));
    }

    let dsr = deflated_sharpe_ratio(
        req.oos_sharpe,
        req.n_obs,
        req.n_trials,
        req.skew,
        req.kurtosis,
        0.5,
        252,
    );
    if dsr < req.dsr_threshold {
        reasons.push(format!(
            "Deflated Sharpe Ratio {:.3} below threshold {:.2} (n_trials={}).",
            dsr, req.dsr_threshold, req.n_trials
        ));
    }

    let min_btl = minimum_backtest_length_years(req.oos_sharpe, req.n_trials, 252);
    if let Some(policy) = &req.risk_policy {
        let compliance = check_order_compliance(
            policy,
            req.target_weight,
            1.0,
            req.max_drawdown_pct,
            1.0,
            false,
        );
        reasons.extend(compliance.violations);
        warnings.extend(compliance.warnings);
    }

    let verdict = if !reasons.is_empty() {
        "rejected"
    } else if !warnings.is_empty() {
        "conditional"
    } else {
        "approved"
    };

    ResearchValidationReport {
        passed: reasons.is_empty(),
        verdict: verdict.to_string(),
        dsr,
        min_btl_years: min_btl,
        oos_retention,
        reasons,
        warnings,
    }
}
