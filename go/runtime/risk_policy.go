package main

import "fmt"

// RiskPolicy defines canonical risk constraints at one governance layer.
type RiskPolicy struct {
	Name                        string  `json:"name"`
	MaxPositionWeight           float64 `json:"max_position_weight"`
	MaxLeverage                 float64 `json:"max_leverage"`
	MaxDrawdownPct              float64 `json:"max_drawdown_pct"`
	ApprovalRequiredAboveWeight float64 `json:"approval_required_above_weight"`
	MinCashReservePct           float64 `json:"min_cash_reserve_pct"`
	AllowShort                  bool    `json:"allow_short"`
}

// DefaultRiskPolicy returns conservative baseline institutional risk limits.
func DefaultRiskPolicy(name string) RiskPolicy {
	if name == "" {
		name = "default"
	}
	return RiskPolicy{
		Name:                        name,
		MaxPositionWeight:           0.50,
		MaxLeverage:                 1.0,
		MaxDrawdownPct:              0.20,
		ApprovalRequiredAboveWeight: 0.15,
		MinCashReservePct:           0.05,
		AllowShort:                  false,
	}
}

// ResolveRiskPolicy merges layered policies using strictest-wins semantics.
func ResolveRiskPolicy(name string, policies ...RiskPolicy) RiskPolicy {
	if len(policies) == 0 {
		return DefaultRiskPolicy(name)
	}
	eff := policies[0]
	eff.Name = name
	if eff.Name == "" {
		eff.Name = "effective"
	}
	for _, p := range policies[1:] {
		if p.MaxPositionWeight < eff.MaxPositionWeight {
			eff.MaxPositionWeight = p.MaxPositionWeight
		}
		if p.MaxLeverage < eff.MaxLeverage {
			eff.MaxLeverage = p.MaxLeverage
		}
		if p.MaxDrawdownPct < eff.MaxDrawdownPct {
			eff.MaxDrawdownPct = p.MaxDrawdownPct
		}
		if p.ApprovalRequiredAboveWeight < eff.ApprovalRequiredAboveWeight {
			eff.ApprovalRequiredAboveWeight = p.ApprovalRequiredAboveWeight
		}
		if p.MinCashReservePct > eff.MinCashReservePct {
			eff.MinCashReservePct = p.MinCashReservePct
		}
		eff.AllowShort = eff.AllowShort && p.AllowShort
	}
	return eff
}

// RiskCheckOutcome captures the compliance verdict for a proposed order.
type RiskCheckOutcome struct {
	Allowed          bool       `json:"allowed"`
	RequiresApproval bool       `json:"requires_approval"`
	Violations       []string   `json:"violations"`
	Warnings         []string   `json:"warnings"`
	EffectivePolicy  RiskPolicy `json:"effective_policy"`
}

// CheckOrderCompliance evaluates a proposed position/order against the resolved RiskPolicy.
func CheckOrderCompliance(
	policy RiskPolicy,
	targetWeight float64,
	grossLeverage float64,
	currentDrawdownPct float64,
	cashReservePct float64,
	isShort bool,
) RiskCheckOutcome {
	var violations []string
	var warnings []string

	if isShort && !policy.AllowShort {
		violations = append(violations, "Short selling is disabled by effective risk policy.")
	}
	if targetWeight > policy.MaxPositionWeight {
		violations = append(violations, fmt.Sprintf(
			"Target weight %.2f%% exceeds max_position_weight %.2f%%.",
			targetWeight*100, policy.MaxPositionWeight*100,
		))
	}
	if grossLeverage > policy.MaxLeverage {
		violations = append(violations, fmt.Sprintf(
			"Gross leverage %.2fx exceeds max_leverage %.2fx.",
			grossLeverage, policy.MaxLeverage,
		))
	}
	if currentDrawdownPct >= policy.MaxDrawdownPct {
		violations = append(violations, fmt.Sprintf(
			"Current drawdown %.2f%% reached circuit-breaker %.2f%%.",
			currentDrawdownPct*100, policy.MaxDrawdownPct*100,
		))
	}
	if cashReservePct < policy.MinCashReservePct {
		violations = append(violations, fmt.Sprintf(
			"Cash reserve %.2f%% is below min_cash_reserve_pct %.2f%%.",
			cashReservePct*100, policy.MinCashReservePct*100,
		))
	}

	requiresApproval := len(violations) == 0 && targetWeight > policy.ApprovalRequiredAboveWeight
	if requiresApproval {
		warnings = append(warnings, fmt.Sprintf(
			"Target weight %.2f%% exceeds approval threshold %.2f%%.",
			targetWeight*100, policy.ApprovalRequiredAboveWeight*100,
		))
	}

	return RiskCheckOutcome{
		Allowed:          len(violations) == 0,
		RequiresApproval: requiresApproval,
		Violations:       violations,
		Warnings:         warnings,
		EffectivePolicy:  policy,
	}
}
