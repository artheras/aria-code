package main

import (
	"strings"
	"testing"
)

func TestFinanceQuoteMustMatchTheServiceResult(t *testing.T) {
	e := newTurnEvidence(t.TempDir())
	e.finished("mcp__arthera-finance__financial_quote", nil, toolResult{
		Success: true,
		Data: map[string]any{
			"server":  "arthera-finance",
			"tool":    "financial_quote",
			"content": `{"success":true,"source":"yfinance","data":{"price":210.5,"change_pct":-2.4,"currency":"USD"}}`,
		},
	})
	if !e.financeFigures["210.5"] || !e.financeFigures["-2.4"] {
		t.Fatal(e.financeFigures)
	}
	matched := claimFindings("AAPL is $210.50, 涨跌幅 -2.4%.", DeliveryReport{}, e.financeFigures)
	if len(matched) != 0 {
		t.Fatal(matched)
	}
	missed := claimFindings("AAPL is $199. 持仓 10.", DeliveryReport{}, e.financeFigures)
	text := strings.Join(missed, "\n")
	if len(missed) != 2 || !strings.Contains(text, "199") || !strings.Contains(text, "10") {
		t.Fatal(missed)
	}
}

func TestAPriceWithoutAFinanceResultIsUnverified(t *testing.T) {
	findings := claimFindings("AAPL is $210.50.", DeliveryReport{}, nil)
	if len(findings) != 1 || !strings.Contains(findings[0], "210.50") {
		t.Fatal(findings)
	}
	if findings := claimFindings("Lunch is $12.", DeliveryReport{}, nil); len(findings) != 0 {
		t.Fatal(findings)
	}
	e := newTurnEvidence(t.TempDir())
	e.financeFigures = map[string]bool{"210.5": true}
	answer := annotateClaims("AAPL is $199.", e)
	if !strings.Contains(answer, "AAPL is $199.") || !strings.Contains(answer, claimNoteHeader) || !strings.Contains(answer, "199") {
		t.Fatal(answer)
	}
}

func TestAFailedFinanceCallDoesNotAuthorizeNumbers(t *testing.T) {
	e := newTurnEvidence(t.TempDir())
	e.finished("mcp__arthera-finance__financial_quote", nil, toolResult{
		Success: true,
		Data: map[string]any{
			"server":  "arthera-finance",
			"content": `{"success":false,"error":"no quote","data":{"price":210.5}}`,
		},
	})
	if len(e.financeFigures) != 0 {
		t.Fatal(e.financeFigures)
	}
	e.finished("mcp__aria__aria_report_chart", nil, toolResult{
		Success: true,
		Data:    map[string]any{"server": "aria", "content": `{"price":1}`},
	})
	if len(e.financeFigures) != 0 {
		t.Fatal(e.financeFigures)
	}
}

func TestPositionQuantityIsKeptFromTheService(t *testing.T) {
	numbers := financeNumbers(`{"positions":[{"symbol":"AAPL","quantity":12,"market_value":2526}]}`)
	if !numbers["12"] || !numbers["2526"] {
		t.Fatal(numbers)
	}
	findings := claimFindings("AAPL 持仓 12，市值 2526。", DeliveryReport{}, numbers)
	if len(findings) != 0 {
		t.Fatal(findings)
	}
}
