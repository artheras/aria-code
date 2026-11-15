package main

import "testing"

func TestResolveRiskPolicyStrictestWins(t *testing.T) {
	account := RiskPolicy{
		Name:                        "account",
		MaxPositionWeight:           0.40,
		MaxLeverage:                 1.5,
		MaxDrawdownPct:              0.25,
		ApprovalRequiredAboveWeight: 0.20,
		MinCashReservePct:           0.05,
		AllowShort:                  true,
	}
	execution := RiskPolicy{
		Name:                        "execution",
		MaxPositionWeight:           0.15,
		MaxLeverage:                 1.0,
		MaxDrawdownPct:              0.15,
		ApprovalRequiredAboveWeight: 0.10,
		MinCashReservePct:           0.10,
		AllowShort:                  false,
	}
	eff := ResolveRiskPolicy("effective", account, execution)
	if eff.MaxPositionWeight != 0.15 {
		t.Fatalf("expected MaxPositionWeight 0.15, got %v", eff.MaxPositionWeight)
	}
	if eff.MinCashReservePct != 0.10 {
		t.Fatalf("expected MinCashReservePct 0.10, got %v", eff.MinCashReservePct)
	}
	if eff.AllowShort {
		t.Fatalf("expected AllowShort false")
	}

	blocked := CheckOrderCompliance(eff, 0.20, 1.2, 0.16, 0.05, true)
	if blocked.Allowed {
		t.Fatalf("expected order to be blocked")
	}
	if len(blocked.Violations) != 5 {
		t.Fatalf("expected 5 violations, got %d: %v", len(blocked.Violations), blocked.Violations)
	}

	approval := CheckOrderCompliance(eff, 0.12, 1.0, 0.02, 0.20, false)
	if !approval.Allowed || !approval.RequiresApproval {
		t.Fatalf("expected allowed=true and requires_approval=true, got %+v", approval)
	}
}
