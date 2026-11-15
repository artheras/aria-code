package quant

import "testing"

func TestDeflatedSharpeRatioAndValidation(t *testing.T) {
	dsr1 := DeflatedSharpeRatio(1.8, 500, 1, 0.0, 3.0, 0.5, 252)
	dsr100 := DeflatedSharpeRatio(1.8, 500, 100, 0.0, 3.0, 0.5, 252)
	if dsr1 <= 0.99 {
		t.Fatalf("expected dsr1 > 0.99, got %v", dsr1)
	}
	if dsr100 >= dsr1 {
		t.Fatalf("expected dsr100 < dsr1, got %v >= %v", dsr100, dsr1)
	}

	btl1 := MinimumBacktestLengthYears(1.5, 1, 252)
	btl50 := MinimumBacktestLengthYears(1.5, 50, 252)
	if btl50 <= btl1 || btl1 <= 0 {
		t.Fatalf("expected btl50 > btl1 > 0, got %v, %v", btl50, btl1)
	}

	ok := ValidateResearchBacktest(2.0, 1.6, 750, 5, true, 0.95, 0.40)
	if !ok.Passed || ok.Verdict != "approved" {
		t.Fatalf("expected approved report, got %+v", ok)
	}

	bad := ValidateResearchBacktest(3.0, 0.3, 40, 200, false, 0.95, 0.40)
	if bad.Passed || bad.Verdict != "rejected" || len(bad.Reasons) < 3 {
		t.Fatalf("expected rejected report with reasons, got %+v", bad)
	}
}
