package quant

import (
	"math"
	"testing"
)

func TestBlackScholesPricingAndGreeks(t *testing.T) {
	spec := OptionSpec{
		S:      100.0,
		K:      100.0,
		T:      1.0,
		R:      0.05,
		Sigma:  0.20,
		IsCall: true,
		Q:      0.0,
	}
	greeks := CalculateGreeks(spec)
	// Standard benchmark price for S=100, K=100, T=1, r=0.05, vol=0.2 is approx 10.4506
	if math.Abs(greeks.Price-10.4506) > 0.01 {
		t.Fatalf("expected call price ~10.4506, got %f", greeks.Price)
	}
	if greeks.Delta <= 0.5 || greeks.Delta >= 0.7 {
		t.Fatalf("expected delta ~0.63, got %f", greeks.Delta)
	}
	if greeks.Gamma <= 0 || greeks.Vega <= 0 {
		t.Fatalf("expected positive gamma and vega, got gamma=%f, vega=%f", greeks.Gamma, greeks.Vega)
	}

	// Put-Call Parity Test: C - P = S - K * exp(-rT)
	putSpec := spec
	putSpec.IsCall = false
	putGreeks := CalculateGreeks(putSpec)

	parityLHS := greeks.Price - putGreeks.Price
	parityRHS := spec.S - spec.K*math.Exp(-spec.R*spec.T)
	if math.Abs(parityLHS-parityRHS) > 1e-4 {
		t.Fatalf("put-call parity violation: LHS=%f, RHS=%f", parityLHS, parityRHS)
	}
}

func TestImpliedVolatilityRecovery(t *testing.T) {
	spec := OptionSpec{
		S:      150.0,
		K:      155.0,
		T:      0.5,
		R:      0.04,
		Sigma:  0.28,
		IsCall: true,
		Q:      0.01,
	}
	g := CalculateGreeks(spec)
	iv, err := ImpliedVolatility(g.Price, spec)
	if err != nil {
		t.Fatalf("implied vol calculation failed: %v", err)
	}
	if math.Abs(iv-0.28) > 0.001 {
		t.Fatalf("expected recovered IV ~0.28, got %f", iv)
	}
}

func TestAmericanCRRPrice(t *testing.T) {
	spec := OptionSpec{
		S:      100.0,
		K:      110.0,
		T:      1.0,
		R:      0.08,
		Sigma:  0.30,
		IsCall: false, // American Put has early exercise premium
		Q:      0.0,
	}
	euroPrice := CalculateGreeks(spec).Price
	amerPrice := PriceAmericanCRR(spec, 200)

	if amerPrice < euroPrice-1e-6 {
		t.Fatalf("American put price (%f) must be >= European put price (%f)", amerPrice, euroPrice)
	}
}

func TestPortfolioOptimization(t *testing.T) {
	cov := [][]float64{
		{0.04, 0.015, 0.001},
		{0.015, 0.035, 0.002},
		{0.001, 0.002, 0.01},
	}
	returns := []float64{0.14, 0.12, 0.05}

	rpWeights, err := OptimizeRiskParity(cov)
	if err != nil {
		t.Fatalf("risk parity failed: %v", err)
	}
	sumRP := 0.0
	for _, w := range rpWeights {
		sumRP += w
	}
	if math.Abs(sumRP-1.0) > 1e-4 {
		t.Fatalf("risk parity weights sum to %f, expected 1.0", sumRP)
	}

	mvWeights, err := OptimizeMinVariance(cov)
	if err != nil {
		t.Fatalf("min variance failed: %v", err)
	}
	sumMV := 0.0
	for _, w := range mvWeights {
		sumMV += w
	}
	if math.Abs(sumMV-1.0) > 1e-4 {
		t.Fatalf("min variance weights sum to %f, expected 1.0", sumMV)
	}

	mvVol := PortfolioVolatility(mvWeights, cov)
	rpVol := PortfolioVolatility(rpWeights, cov)
	if mvVol > rpVol {
		t.Fatalf("min variance portfolio volatility (%f) should be <= risk parity volatility (%f)", mvVol, rpVol)
	}
	_ = PortfolioReturn(mvWeights, returns)
}
