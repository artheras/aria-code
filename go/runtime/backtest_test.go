package main

import (
	"encoding/json"
	"math"
	"os"
	"reflect"
	"testing"

	"github.com/artheras/aria-code/go/runtime/quant"
)

func TestBacktestParityFixturesFromExistingPythonEngine(t *testing.T) {
	body, err := os.ReadFile("testdata/backtest-parity.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixtures []struct {
		Name     string        `json:"name"`
		Input    backtestInput `json:"input"`
		Expected quant.Result  `json:"expected"`
	}
	if err := json.Unmarshal(body, &fixtures); err != nil {
		t.Fatal(err)
	}
	for _, f := range fixtures {
		t.Run(f.Name, func(t *testing.T) {
			got, err := runBacktest(f.Input)
			if err != nil {
				t.Fatal(err)
			}
			// Annualizing a two-day run compounds to ~1e14, where Go's math.Pow and
			// C's pow differ in the last digit. Everything else must match exactly.
			if want := f.Expected.Metrics.AnnualReturn; math.Abs(got.Metrics.AnnualReturn-want) <= 1e-12*math.Abs(want) {
				got.Metrics.AnnualReturn = want
			}
			if !reflect.DeepEqual(got, f.Expected) {
				a, _ := json.Marshal(got)
				b, _ := json.Marshal(f.Expected)
				t.Fatalf("domain output differs from Python oracle\nactual: %s\nexpected: %s", a, b)
			}
		})
	}
}
func TestBacktestPreservesInputSymbolOrderAndRejectsDuplicateSymbols(t *testing.T) {
	data, err := orderedSeries(json.RawMessage(`{"ZZZ":[],"AAA":[]}`))
	if err != nil || data[0].Symbol != "ZZZ" {
		t.Fatal(data, err)
	}
	if _, err := orderedSeries(json.RawMessage(`{"AAA":[],"AAA":[]}`)); err == nil {
		t.Fatal("ambiguous duplicate JSON key accepted")
	}
}
