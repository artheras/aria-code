package quant

import (
	"math"
	"testing"
)

func TestDCFValuationAndSensitivity(t *testing.T) {
	px := 100.0
	res, err := DCFValuation(1000, []float64{0.10, 0.08, 0.06, 0.05, 0.04}, 0.09, 0.025, 2000, 100, &px)
	if err != nil {
		t.Fatalf("DCFValuation failed: %v", err)
	}
	if len(res.Projections) != 5 {
		t.Fatalf("expected 5 projections, got %d", len(res.Projections))
	}
	if len(res.SensitivityMatrix) != 5 {
		t.Fatalf("expected 5x5 sensitivity matrix, got %d rows", len(res.SensitivityMatrix))
	}
	if res.EnterpriseValue <= res.PVExplicitFCF || res.ImpliedSharePrice <= 0 {
		t.Fatalf("unexpected DCF values: %+v", res)
	}
	if res.UpsideVsCurrentPrice == nil || math.IsNaN(*res.UpsideVsCurrentPrice) {
		t.Fatal("expected upside_vs_current_price")
	}
	if _, err := DCFValuation(1000, []float64{0.05}, 0.04, 0.05, 0, 10, nil); err == nil {
		t.Fatal("expected error when wacc <= terminal_growth")
	}
}

func TestCalculateRiskMetricsAndVaR(t *testing.T) {
	equity := []float64{100, 102, 99, 101, 105, 103, 98, 102, 107, 106, 110}
	res, err := CalculateRiskMetrics(equity, false, 252, 0.02)
	if err != nil {
		t.Fatalf("CalculateRiskMetrics failed: %v", err)
	}
	if res.Observations != 10 {
		t.Fatalf("expected 10 return observations, got %d", res.Observations)
	}
	if res.MaxDrawdownPct <= 0 || res.HistoricalVaR95Pct <= 0 || res.MonteCarloVaR95Pct <= 0 {
		t.Fatalf("expected positive risk metrics, got %+v", res)
	}
	if res.HistoricalCVaR95Pct < res.HistoricalVaR95Pct {
		t.Fatalf("CVaR95 (%v) should be >= VaR95 (%v)", res.HistoricalCVaR95Pct, res.HistoricalVaR95Pct)
	}
}

func TestComputeWACCAndPeerCompsValuation(t *testing.T) {
	wacc, err := ComputeWACC(0.04, 1.2, 0.05, 0.06, 0.25, 800, 200)
	if err != nil {
		t.Fatalf("ComputeWACC failed: %v", err)
	}
	// ke = 0.04 + 1.2*0.05 = 0.10; kd_at = 0.06*0.75 = 0.045; wacc = 0.8*0.10 + 0.2*0.045 = 0.089
	if math.Abs(wacc.CostOfEquity-0.10) > 1e-4 || math.Abs(wacc.WACC-0.089) > 1e-4 {
		t.Fatalf("unexpected WACC result: %+v", wacc)
	}

	f := func(v float64) *float64 { return &v }
	comps, err := PeerCompsValuation(
		[]PeerMultipleInput{
			{Symbol: "P1", EVEBITDA: f(10), PE: f(18)},
			{Symbol: "P2", EVEBITDA: f(12), PE: f(20)},
			{Symbol: "P3", EVEBITDA: f(14), PE: f(24)},
		},
		100, 50, 500, 200, 10,
	)
	if err != nil {
		t.Fatalf("PeerCompsValuation failed: %v", err)
	}
	if comps.EVEBITDA == nil || comps.EVEBITDA.Median != 12 {
		t.Fatalf("expected EV/EBITDA median 12, got %+v", comps.EVEBITDA)
	}
	// (12 * 100 - 200) / 10 = 100
	if comps.EVEBITDA.ImpliedSharePriceMid == nil || *comps.EVEBITDA.ImpliedSharePriceMid != 100 {
		t.Fatalf("expected implied share price mid 100, got %+v", comps.EVEBITDA)
	}
}
