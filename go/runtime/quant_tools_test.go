package main

import (
	"context"
	"math"
	"testing"
)

func TestPriceOptionTool(t *testing.T) {
	ctx := context.Background()

	// 1. European Call with Greeks
	res := priceOptionTool(ctx, map[string]any{
		"s":       100.0,
		"k":       100.0,
		"t":       1.0,
		"r":       0.05,
		"sigma":   0.20,
		"is_call": true,
	})
	if !res.Success {
		t.Fatalf("priceOptionTool failed: %s", res.Error)
	}
	greeks, ok := res.Data["greeks"].(map[string]any)
	if !ok {
		t.Fatalf("expected greeks map, got %#v", res.Data)
	}
	price := greeks["price"].(float64)
	if math.Abs(price-10.4506) > 0.01 {
		t.Fatalf("expected price ~10.45, got %f", price)
	}

	// 2. American Option Price + Implied Volatility
	resAmer := priceOptionTool(ctx, map[string]any{
		"s":            100.0,
		"k":            105.0,
		"t":            0.5,
		"r":            0.04,
		"sigma":        0.25,
		"is_call":      false,
		"american":     true,
		"market_price": 8.50,
	})
	if !resAmer.Success {
		t.Fatalf("priceOptionTool with american failed: %s", resAmer.Error)
	}
	amerPrice, ok := resAmer.Data["american_price"].(float64)
	if !ok || amerPrice <= 0 {
		t.Fatalf("expected valid american price, got %v", resAmer.Data["american_price"])
	}
	iv, ok := resAmer.Data["implied_volatility"].(float64)
	if !ok || iv <= 0 {
		t.Fatalf("expected valid implied vol, got %v", resAmer.Data["implied_volatility"])
	}
}

func TestOptimizePortfolioTool(t *testing.T) {
	ctx := context.Background()

	res := optimizePortfolioTool(ctx, map[string]any{
		"assets":  []any{"AAPL", "NVDA", "TLT"},
		"returns": []any{0.15, 0.25, 0.04},
		"cov_matrix": []any{
			[]any{0.04, 0.02, 0.001},
			[]any{0.02, 0.09, 0.002},
			[]any{0.001, 0.002, 0.015},
		},
	})
	if !res.Success {
		t.Fatalf("optimizePortfolioTool failed: %s", res.Error)
	}
	rp, ok := res.Data["risk_parity"].(map[string]any)
	if !ok {
		t.Fatalf("expected risk_parity allocation, got %#v", res.Data)
	}
	rpWeights := rp["weights"].([]float64)
	if len(rpWeights) != 3 {
		t.Fatalf("expected 3 weights, got %d", len(rpWeights))
	}
	sum := 0.0
	for _, w := range rpWeights {
		sum += w
	}
	if math.Abs(sum-1.0) > 1e-4 {
		t.Fatalf("expected weights sum to 1.0, got %f", sum)
	}
}
