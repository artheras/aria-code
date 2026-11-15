use serde::{Deserialize, Serialize};

/// Canonical risk constraints at one governance layer.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RiskPolicy {
    pub name: String,
    pub max_position_weight: f64,
    pub max_leverage: f64,
    pub max_drawdown_pct: f64,
    pub approval_required_above_weight: f64,
    pub min_cash_reserve_pct: f64,
    pub allow_short: bool,
}

impl Default for RiskPolicy {
    fn default() -> Self {
        Self {
            name: "default".to_string(),
            max_position_weight: 0.50,
            max_leverage: 1.0,
            max_drawdown_pct: 0.20,
            approval_required_above_weight: 0.15,
            min_cash_reserve_pct: 0.05,
            allow_short: false,
        }
    }
}

/// Merge layered policies using strictest-wins semantics.
pub fn resolve_risk_policy(policies: &[RiskPolicy], name: &str) -> RiskPolicy {
    if policies.is_empty() {
        return RiskPolicy {
            name: name.to_string(),
            ..RiskPolicy::default()
        };
    }
    RiskPolicy {
        name: name.to_string(),
        max_position_weight: policies
            .iter()
            .map(|p| p.max_position_weight)
            .fold(f64::INFINITY, f64::min),
        max_leverage: policies
            .iter()
            .map(|p| p.max_leverage)
            .fold(f64::INFINITY, f64::min),
        max_drawdown_pct: policies
            .iter()
            .map(|p| p.max_drawdown_pct)
            .fold(f64::INFINITY, f64::min),
        approval_required_above_weight: policies
            .iter()
            .map(|p| p.approval_required_above_weight)
            .fold(f64::INFINITY, f64::min),
        min_cash_reserve_pct: policies
            .iter()
            .map(|p| p.min_cash_reserve_pct)
            .fold(f64::NEG_INFINITY, f64::max),
        allow_short: policies.iter().all(|p| p.allow_short),
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RiskCheckOutcome {
    pub allowed: bool,
    pub requires_approval: bool,
    pub violations: Vec<String>,
    pub warnings: Vec<String>,
    pub effective_policy: RiskPolicy,
}

/// Evaluate a proposed position/order against the resolved RiskPolicy.
pub fn check_order_compliance(
    policy: &RiskPolicy,
    target_weight: f64,
    gross_leverage: f64,
    current_drawdown_pct: f64,
    cash_reserve_pct: f64,
    is_short: bool,
) -> RiskCheckOutcome {
    let mut violations = Vec::new();
    let mut warnings = Vec::new();

    if is_short && !policy.allow_short {
        violations.push("Short selling is disabled by effective risk policy.".to_string());
    }
    if target_weight > policy.max_position_weight {
        violations.push(format!(
            "Target weight {:.2}% exceeds max_position_weight {:.2}%.",
            target_weight * 100.0,
            policy.max_position_weight * 100.0
        ));
    }
    if gross_leverage > policy.max_leverage {
        violations.push(format!(
            "Gross leverage {:.2}x exceeds max_leverage {:.2}x.",
            gross_leverage, policy.max_leverage
        ));
    }
    if current_drawdown_pct >= policy.max_drawdown_pct {
        violations.push(format!(
            "Current drawdown {:.2}% reached circuit-breaker {:.2}%.",
            current_drawdown_pct * 100.0,
            policy.max_drawdown_pct * 100.0
        ));
    }
    if cash_reserve_pct < policy.min_cash_reserve_pct {
        violations.push(format!(
            "Cash reserve {:.2}% is below min_cash_reserve_pct {:.2}%.",
            cash_reserve_pct * 100.0,
            policy.min_cash_reserve_pct * 100.0
        ));
    }

    let requires_approval =
        violations.is_empty() && target_weight > policy.approval_required_above_weight;
    if requires_approval {
        warnings.push(format!(
            "Target weight {:.2}% exceeds approval threshold {:.2}%.",
            target_weight * 100.0,
            policy.approval_required_above_weight * 100.0
        ));
    }

    RiskCheckOutcome {
        allowed: violations.is_empty(),
        requires_approval,
        violations,
        warnings,
        effective_policy: policy.clone(),
    }
}
