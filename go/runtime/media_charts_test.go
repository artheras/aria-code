package main

import (
	"bytes"
	"context"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// sampleRows are n daily rows in financial_market_history's shape.
func sampleRows(n int, start float64, drift float64) []any {
	rows := make([]any, n)
	day := time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC)
	price := start
	for i := range rows {
		open := price
		price = price * (1 + drift + 0.01*math.Sin(float64(i)/3))
		rows[i] = map[string]any{"date": day.Format("2006-01-02"), "open": open, "high": math.Max(open, price) * 1.01,
			"low": math.Min(open, price) * 0.99, "close": price, "volume": float64(1_000_000 + i*1000)}
		day = day.AddDate(0, 0, 1)
	}
	return rows
}

func validSVG(t *testing.T, svg string) {
	t.Helper()
	d := xml.NewDecoder(strings.NewReader(svg))
	for {
		if _, err := d.Token(); err == io.EOF {
			break
		} else if err != nil {
			t.Fatalf("invalid SVG: %v", err)
		}
	}
	if strings.Contains(svg, "NaN") || strings.Contains(svg, "Inf") {
		t.Fatal("SVG has NaN or Inf coordinates")
	}
}

func chartHome(t *testing.T) string {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("ARIA_HOME", filepath.Join(home, ".aria"))
	t.Chdir(home)
	return home
}

func TestParseBars(t *testing.T) {
	rows := sampleRows(5, 100, 0.01)
	bars, err := parseBars(map[string]any{"success": true, "data": rows})
	if err != nil || len(bars) != 5 || !bars[0].OHLC || bars[0].Date != "2026-01-02" {
		t.Fatal(bars, err)
	}
	// Newest-first input is put oldest first; capitalised keys and strings are read.
	reversed := []any{
		map[string]any{"Date": "2026-02-02", "Close": "11.5"},
		map[string]any{"Date": "2026-02-01", "Close": 10.0, "Open": 9.0, "High": 8.0, "Low": 7.0},
		map[string]any{"Date": "2026-01-31", "close": -1},
	}
	bars, err = parseBars(reversed)
	if err != nil || len(bars) != 2 || bars[0].Close != 10 || bars[1].Close != 11.5 || bars[0].OHLC {
		t.Fatal("an inconsistent high/low must not draw a candle", bars, err)
	}
	for _, bad := range []any{
		map[string]any{"success": false, "error": "No history returned"},
		"rows", []any{map[string]any{"close": 1}},
	} {
		if _, err := parseBars(bad); err == nil {
			t.Fatal("accepted", bad)
		}
	}
}

func TestIndicators(t *testing.T) {
	values := []float64{1, 2, 3, 4, 5}
	if got := sma(values, 3); !math.IsNaN(got[1]) || got[2] != 2 || got[4] != 4 {
		t.Fatal(got)
	}
	if got := ema([]float64{10, 20}, 3); got[1] != 15 {
		t.Fatal(got)
	}
	up := make([]float64, 30)
	for i := range up {
		up[i] = float64(i + 1)
	}
	if got := rsi(up, 14); !math.IsNaN(got[13]) || got[14] != 100 {
		t.Fatal("a rising series has RSI 100", got[13], got[14])
	}
	// Wilder's RSI against a hand-checked value: alternating +2/-1 moves.
	zig := []float64{10}
	for i := 1; i < 40; i++ {
		if i%2 == 1 {
			zig = append(zig, zig[i-1]+2)
		} else {
			zig = append(zig, zig[i-1]-1)
		}
	}
	if got := rsi(zig, 14)[39]; got < 60 || got > 72 {
		t.Fatal("RSI of +2/-1 moves should sit near 2/3", got)
	}
	line, signal, hist := macd(up)
	if line[0] != 0 || line[29] <= 0 || math.Abs(hist[29]-(line[29]-signal[29])) > 1e-12 {
		t.Fatal(line[29], signal[29], hist[29])
	}
	if ticks, step := niceTicks(0, 97, 5); step != 20 || ticks[0] != 0 || ticks[len(ticks)-1] != 80 {
		t.Fatal(ticks, step)
	}
	if compactLabel(3.1e12) != "3.1T" || compactLabel(45_250_000) != "45.25M" || compactLabel(31.2) != "31.20" || compactLabel(0.01234) != "0.0123" {
		t.Fatal(compactLabel(3.1e12), compactLabel(45_250_000), compactLabel(31.2), compactLabel(0.01234))
	}
	if amountLabel(1234567.4) != "1,234,567" || amountLabel(-12.5) != "-12.50" || tickLabel(-1e-17, 1) != "0" {
		t.Fatal(amountLabel(1234567.4), amountLabel(-12.5), tickLabel(-1e-17, 1))
	}
	if tickLabel(2_500_000, 500_000) != "2.5M" || tickLabel(1.25, 0.05) != "1.25" || tickLabel(120, 20) != "120" {
		t.Fatal(tickLabel(2_500_000, 500_000), tickLabel(1.25, 0.05), tickLabel(120, 20))
	}
}

func TestPriceCharts(t *testing.T) {
	bars, _ := parseBars(sampleRows(300, 50, 0.002))
	svg := priceChartSVG("AAPL", bars, 126, false)
	validSVG(t, svg)
	if strings.Count(svg, `opacity="0.5"`) != 126 || !strings.Contains(svg, "MA50") {
		t.Fatal("expected 126 volume bars and the MA legend")
	}
	if strings.Contains(svg, "RSI") {
		t.Fatal("the plain chart has no indicator panels")
	}
	full := priceChartSVG("AAPL", bars, 126, true)
	validSVG(t, full)
	if !strings.Contains(full, "RSI 14") || !strings.Contains(full, "MACD 12·26·9") || !strings.Contains(full, "prefers-color-scheme: dark") {
		t.Fatal("indicator panels or dark theme missing")
	}
	// Closes only: a line instead of candles, no volume panel, still valid.
	var closesOnly []any
	for i := 0; i < 40; i++ {
		closesOnly = append(closesOnly, map[string]any{"close": 10 + float64(i%7)})
	}
	plain, _ := parseBars(closesOnly)
	line := priceChartSVG("X", plain, 126, true)
	validSVG(t, line)
	if strings.Contains(line, "Volume") || strings.Contains(line, "<g class=\"ac-up\"") {
		t.Fatal("no candles or volume without OHLCV")
	}
}

func TestComparisonAndAllocation(t *testing.T) {
	a := sampleRows(200, 100, 0.001)
	b := sampleRows(150, 20, -0.001)[:150]
	// b starts 50 days later: both are indexed to 100 on b's first date.
	shift := sampleRows(200, 20, -0.001)[50:]
	series, err := parseSeries(map[string]any{"MSFT": a, "AAPL": map[string]any{"data": shift}})
	if err != nil || len(series) != 2 || series[0].symbol != "AAPL" {
		t.Fatal(series, err)
	}
	svg, err := comparisonChartSVG(series, 1000, "")
	if err != nil {
		t.Fatal(err)
	}
	validSVG(t, svg)
	if !strings.Contains(svg, "From 2026-02-21") || !strings.Contains(svg, "AAPL ") {
		t.Fatal("the shared window should start at the later series' first date")
	}
	if _, err := comparisonChartSVG([]namedSeries{{"A", mustBars(t, a[:100])}, {"B", mustBars(t, b[120:])}}, 126, ""); err == nil {
		t.Fatal("series that share no dates must be refused")
	}

	var positions []any
	for i := 0; i < 11; i++ {
		positions = append(positions, map[string]any{"symbol": fmt.Sprintf("S%02d", i), "quantity": float64(i + 1), "last_price": 10.0})
	}
	positions = append(positions, map[string]any{"symbol": "ZERO", "market_value": 0}, map[string]any{"ticker": "MV", "market_value": "500"})
	slices, err := parsePositions(map[string]any{"positions": positions})
	if err != nil || len(slices) != 8 || slices[0].label != "MV" || !strings.HasPrefix(slices[7].label, "Other (5)") {
		t.Fatal(slices, err)
	}
	pie := allocationChartSVG(slices, "")
	validSVG(t, pie)
	single := allocationChartSVG([]allocSlice{{"ONLY", 10}}, "Mine")
	validSVG(t, single)
	if !strings.Contains(single, "100.0%") {
		t.Fatal("a single holding is the whole ring")
	}
}

func mustBars(t *testing.T, rows []any) []priceBar {
	t.Helper()
	bars, err := parseBars(rows)
	if err != nil {
		t.Fatal(err)
	}
	return bars
}

func TestChartTools(t *testing.T) {
	home := chartHome(t)
	ctx := context.Background()
	rows := sampleRows(260, 80, 0.001)
	r := mcpReportChart(ctx, map[string]any{"symbol": "aapl", "rows": rows})
	if r["success"] != true || r["rows"] != 126 || !strings.HasPrefix(r["path"].(string), filepath.Join(home, ".aria", "artifacts", "chart", "AAPL_chart_")) {
		t.Fatal(r)
	}
	data, _ := json.Marshal(map[string]any{"success": true, "data": rows})
	os.WriteFile(filepath.Join(home, "hist.json"), data, 0o644)
	named := filepath.Join(home, "ind.svg")
	if r := mcpReportIndicatorChart(ctx, map[string]any{"symbol": "AAPL", "data_path": "hist.json", "days": 60, "output_path": named}); r["success"] != true || r["path"] != named {
		t.Fatal(r)
	}
	svg, _ := os.ReadFile(named)
	validSVG(t, string(svg))

	// Without rows the registered financial-service is asked; here a stand-in.
	var asked []string
	defer func(orig func(context.Context, string) ([]priceBar, string, error)) { fetchHistory = orig }(fetchHistory)
	fetchHistory = func(_ context.Context, symbol string) ([]priceBar, string, error) {
		asked = append(asked, symbol)
		if symbol == "FAIL" {
			return nil, "", errors.New("source_unavailable")
		}
		bars, err := parseBars(rows)
		return bars, "stand-in", err
	}
	if r := mcpReportChart(ctx, map[string]any{"symbol": "600519"}); r["success"] != true || r["source"] != "stand-in" {
		t.Fatal(r)
	}
	if r := mcpReportComparisonChart(ctx, map[string]any{"symbols": []any{"SPY", "QQQ", "spy"}}); r["success"] != true || len(asked) != 3 {
		t.Fatal(r, asked)
	}
	if r := mcpReportComparisonChart(ctx, map[string]any{"series": map[string]any{"A": rows, "B": rows}, "title": "A vs B"}); r["success"] != true {
		t.Fatal(r)
	}
	if r := mcpReportAllocationChart(ctx, map[string]any{"positions": []any{map[string]any{"symbol": "A", "market_value": 3.0}, map[string]any{"symbol": "B", "market_value": 1.0}}}); r["success"] != true || r["holdings"] != 2 {
		t.Fatal(r)
	}
	for name, call := range map[string]map[string]any{
		"no symbol":       {"rows": rows},
		"bad symbol":      {"symbol": "A B;rm"},
		"both inputs":     {"symbol": "A", "rows": rows, "data_path": "hist.json"},
		"outside path":    {"symbol": "A", "data_path": "/etc/hosts"},
		"days":            {"symbol": "A", "rows": rows, "days": 5},
		"service failure": {"symbol": "FAIL"},
		"existing output": {"symbol": "A", "rows": rows, "output_path": named},
		"wrong extension": {"symbol": "A", "rows": rows, "output_path": filepath.Join(home, "x.png")},
	} {
		if r := mcpReportChart(ctx, call); r["success"] != false {
			t.Fatal(name, "accepted", r)
		}
	}
	if r := mcpReportComparisonChart(ctx, map[string]any{"symbols": []any{"SPY"}}); r["success"] != false {
		t.Fatal(r)
	}
	if r := mcpReportAllocationChart(ctx, map[string]any{}); r["success"] != false {
		t.Fatal(r)
	}
}

func TestFetchHistoryNeedsTheService(t *testing.T) {
	chartHome(t)
	_, _, err := fetchHistory(context.Background(), "AAPL")
	if err == nil || !strings.Contains(err.Error(), "/finance setup") {
		t.Fatal(err)
	}
}

func TestEquityReport(t *testing.T) {
	home := chartHome(t)
	ctx := context.Background()
	rows := sampleRows(300, 120, 0.001)
	r := mcpReportGenerate(ctx, map[string]any{
		"symbol": "MSFT", "rows": rows,
		"fundamentals": map[string]any{"pe_ratio": 31.2, "sector": "Technology | Software", "nested": map[string]any{"x": 1}},
		"analysis":     "Momentum is **positive**; signal: BUY.\n\n<script>alert(1)</script>",
	})
	if r["success"] != true || r["format"] != "html" || r["as_of"] != "2026-10-28" {
		t.Fatal(r)
	}
	page, _ := os.ReadFile(r["path"].(string))
	for _, want := range []string{"MSFT equity report", `class="kpis"`, "52-week range", "<svg", "MACD 12·26·9", "pe_ratio", "Technology", "Recent sessions", "<strong>positive</strong>", "caller-supplied rows"} {
		if !bytes.Contains(page, []byte(want)) {
			t.Fatal("missing", want)
		}
	}
	if bytes.Contains(page, []byte("<script>")) || bytes.Contains(page, []byte("nested")) || bytes.Contains(page, []byte("ARIA-REPORT-CHARTS")) {
		t.Fatal("raw HTML, nested values or the placeholder leaked into the report")
	}
	t.Setenv("ARIA_BROWSER", fakePDFBrowser(t))
	if r := mcpReportGenerate(ctx, map[string]any{"symbol": "MSFT", "rows": rows, "format": "pdf"}); r["success"] != true {
		t.Fatal(r)
	} else if data, _ := os.ReadFile(r["path"].(string)); !bytes.HasPrefix(data, []byte("%PDF-")) {
		t.Fatal("not a PDF")
	}
	os.WriteFile(filepath.Join(home, "notes.md"), []byte("# Notes\n"), 0o644)
	for _, bad := range []map[string]any{
		{"symbol": "MSFT", "rows": rows, "format": "docx"},
		{"symbol": "MSFT", "rows": rows, "fundamentals": []any{1}},
		{"symbol": "MSFT", "rows": rows, "analysis": "x", "analysis_path": "notes.md"},
		{"symbol": "MSFT", "rows": rows, "analysis": strings.Repeat("x", maxMarkdownBytes+1)},
	} {
		if r := mcpReportGenerate(ctx, bad); r["success"] != false {
			t.Fatal("accepted", r)
		}
	}
	if r := mcpReportGenerate(ctx, map[string]any{"symbol": "MSFT", "rows": rows, "analysis_path": "notes.md", "title": "Desk note"}); r["success"] != true {
		t.Fatal(r)
	}
}
