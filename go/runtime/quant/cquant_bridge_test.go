//go:build cquant

package quant

import (
	"math"
	"testing"
)

func TestNativeMonteCarloVaRKernel(t *testing.T) {
	v95, cv95, v99, cv99, err := NativeMonteCarloVaR(0.0005, 0.015, 100000, 42)
	if err != nil {
		t.Fatalf("NativeMonteCarloVaR failed: %v", err)
	}
	if v95 <= 2.0 || v95 >= 3.0 {
		t.Fatalf("unexpected VaR95: %f", v95)
	}
	if cv95 < v95 || cv99 < v99 {
		t.Fatalf("expected CVaR >= VaR: 95=(%f,%f) 99=(%f,%f)", v95, cv95, v99, cv99)
	}
	if math.IsNaN(cv99) {
		t.Fatal("CVaR99 is NaN")
	}
}
