package main

import "testing"

func TestQuoteBriefCopiesThePriceTheServiceReturned(t *testing.T) {
	brief := serviceBrief("mcp__arthera-finance__financial_quote", map[string]any{
		"server":  "arthera-finance",
		"tool":    "financial_quote",
		"content": `{"success":true,"data":{"name":"宁德时代","price":182.4,"change_pct":1.2,"stale":false}}`,
	})
	if brief["kind"] != "quote" || brief["name"] != "宁德时代" || brief["price"] != 182.4 || brief["change_pct"] != 1.2 || brief["stale"] != false {
		t.Fatal(brief)
	}
}

func TestBacktestBriefTurnsFractionsIntoPercent(t *testing.T) {
	brief := serviceBrief("backtest_strategy", map[string]any{
		"symbol": "NVDA", "total_return": 0.084, "max_drawdown": -0.031,
	})
	if brief["kind"] != "backtest" || brief["return_pct"] != 8.4 || brief["drawdown_pct"] != 3.1 {
		t.Fatal(brief)
	}
}

func TestAPercentFieldIsNotScaledAgain(t *testing.T) {
	brief := serviceBrief("run_portfolio_backtest", map[string]any{
		"metrics": map[string]any{"total_return_pct": 8.4, "max_drawdown_pct": 3.1},
	})
	if brief["return_pct"] != 8.4 || brief["drawdown_pct"] != 3.1 {
		t.Fatal(brief)
	}
}

func TestAnOrdinaryToolHasNoBrief(t *testing.T) {
	if brief := serviceBrief("read_file", map[string]any{"content": "x"}); brief != nil {
		t.Fatal(brief)
	}
}

func TestADiffBecomesLineCounts(t *testing.T) {
	brief := serviceBrief("write_file", map[string]any{
		"diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n-old\n+new\n+extra\n",
	})
	if brief["kind"] != "edit" || brief["added"] != 2 || brief["removed"] != 1 {
		t.Fatal(brief)
	}
}
