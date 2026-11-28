package quant

import (
	"math"
	"testing"
)

func TestShrinkCovarianceDiagonalAndBoundedMinVariance(t *testing.T) {
	cov := [][]float64{
		{0.04, 0.02, 0.01},
		{0.02, 0.09, 0.03},
		{0.01, 0.03, 0.16},
	}

	shrunk, err := ShrinkCovarianceDiagonal(cov, 0.5)
	if err != nil {
		t.Fatalf("ShrinkCovarianceDiagonal failed: %v", err)
	}
	if math.Abs(shrunk[0][0]-0.04) > 1e-9 || math.Abs(shrunk[0][1]-0.01) > 1e-9 {
		t.Fatalf("unexpected shrunk matrix: %+v", shrunk)
	}

	w, err := OptimizeBoundedMinVariance(shrunk, 0.15, 0.60)
	if err != nil {
		t.Fatalf("OptimizeBoundedMinVariance failed: %v", err)
	}
	sum := 0.0
	for i, wi := range w {
		if wi < 0.15-1e-5 || wi > 0.60+1e-5 {
			t.Fatalf("weight[%d]=%f out of [0.15, 0.60]", i, wi)
		}
		sum += wi
	}
	if math.Abs(sum-1.0) > 1e-5 {
		t.Fatalf("weights do not sum to 1: %f", sum)
	}
	// Lowest variance asset (0) should receive highest allocation.
	if w[0] <= w[2] {
		t.Fatalf("expected lowest-variance asset 0 (%f) > asset 2 (%f)", w[0], w[2])
	}
}

func TestOptimizeRiskParityAndMinVariance(t *testing.T) {
	cov := [][]float64{
		{0.04, 0.00},
		{0.00, 0.16},
	}
	rp, err := OptimizeRiskParity(cov)
	if err != nil {
		t.Fatalf("OptimizeRiskParity failed: %v", err)
	}
	if math.Abs(rp[0]+rp[1]-1.0) > 1e-5 {
		t.Fatalf("risk parity weights sum != 1: %+v", rp)
	}
	// For diagonal 0.04 vs 0.16 (vol 0.2 vs 0.4), risk parity is 2/3 vs 1/3.
	if math.Abs(rp[0]-0.6667) > 0.02 || math.Abs(rp[1]-0.3333) > 0.02 {
		t.Fatalf("unexpected risk parity weights: %+v", rp)
	}

	mv, err := OptimizeMinVariance(cov)
	if err != nil {
		t.Fatalf("OptimizeMinVariance failed: %v", err)
	}
	if mv[0] <= mv[1] {
		t.Fatalf("expected asset 0 weight > asset 1 weight, got %+v", mv)
	}
}
