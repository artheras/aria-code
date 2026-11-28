package quant

import (
	"testing"
)

func TestRunSignalBacktestEmptyAndUptrend(t *testing.T) {
	empty := RunSignalBacktest(nil, SignalBacktestConfig{InitialCapital: 50000})
	if empty.FinalEquity != 50000 || empty.TotalTrades != 0 {
		t.Fatalf("unexpected empty backtest result: %+v", empty)
	}

	bars := []SignalBarInput{
		{Date: "2026-01-01", Open: 100, High: 101, Low: 99, Close: 100, Signal: 1.0},
		{Date: "2026-01-02", Open: 100, High: 106, Low: 100, Close: 105, Signal: 1.0},
		{Date: "2026-01-03", Open: 105, High: 112, Low: 104, Close: 110, Signal: 1.0},
		{Date: "2026-01-04", Open: 110, High: 115, Low: 109, Close: 114, Signal: 0.0},
	}
	res := RunSignalBacktest(bars, SignalBacktestConfig{
		InitialCapital: 100000,
		CommissionBps:  5,
		SlippageBps:    2,
		AllowShort:     false,
		PeriodsPerYear: 252,
	})
	if res.FinalEquity <= 100000 || res.TotalReturnPct <= 0 {
		t.Fatalf("expected positive return on uptrend, got %+v", res)
	}
	if res.TotalTrades < 2 || res.TotalCommission <= 0 {
		t.Fatalf("expected at least entry+exit trades and commission, got %+v", res)
	}
	if len(res.EquityCurve) != len(bars) {
		t.Fatalf("expected %d equity points, got %d", len(bars), len(res.EquityCurve))
	}
}
