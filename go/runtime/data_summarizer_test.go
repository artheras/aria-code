package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestInspectDataToolCSVAndJSONL(t *testing.T) {
	tmpDir := t.TempDir()
	sc, err := newScope(tmpDir, nil, nil, true)
	if err != nil {
		t.Fatal(err)
	}

	// 1. Create a sample ERP inventory CSV
	csvPath := filepath.Join(tmpDir, "inventory.csv")
	csvContent := `sku,category,warehouse,quantity,unit_cost,last_restocked
SKU-1001,Electronics,WH-East,150,120.50,2026-10-01
SKU-1002,Electronics,WH-West,45,350.00,2026-10-05
SKU-1003,Office,WH-East,500,12.00,2026-09-15
SKU-1004,Office,WH-North,0,45.20,2026-08-20
SKU-1005,Apparel,WH-South,80,25.99,2026-10-12
`
	if err := os.WriteFile(csvPath, []byte(csvContent), 0o644); err != nil {
		t.Fatal(err)
	}

	res := inspectDataTool(context.Background(), sc, map[string]any{
		"path": csvPath,
		"head": float64(3),
	})
	if !res.Success {
		t.Fatalf("inspectDataTool CSV failed: %s", res.Error)
	}

	summary := res.Data["summary"].(string)
	if !strings.Contains(summary, "Rows Sampled**: 5") || !strings.Contains(summary, "Columns**: 6") {
		t.Fatalf("unexpected summary rows/cols count: %s", summary)
	}
	if !strings.Contains(summary, "quantity") || !strings.Contains(summary, "unit_cost") {
		t.Fatalf("expected quantity and unit_cost columns in summary: %s", summary)
	}
	if !strings.Contains(summary, "SKU-1001") {
		t.Fatalf("expected sample row in summary: %s", summary)
	}

	// 2. Create sample JSONL data
	jsonlPath := filepath.Join(tmpDir, "orders.jsonl")
	jsonlContent := `{"order_id": 101, "customer": "Alice", "amount": 150.5, "status": "shipped"}
{"order_id": 102, "customer": "Bob", "amount": 89.0, "status": "delivered"}
{"order_id": 103, "customer": "Charlie", "amount": 320.0, "status": "pending"}
`
	if err := os.WriteFile(jsonlPath, []byte(jsonlContent), 0o644); err != nil {
		t.Fatal(err)
	}

	resJsonl := inspectDataTool(context.Background(), sc, map[string]any{
		"path": jsonlPath,
		"head": float64(2),
	})
	if !resJsonl.Success {
		t.Fatalf("inspectDataTool JSONL failed: %s", resJsonl.Error)
	}
	summaryJsonl := resJsonl.Data["summary"].(string)
	if !strings.Contains(summaryJsonl, "Rows Sampled**: 3") || !strings.Contains(summaryJsonl, "order_id") {
		t.Fatalf("unexpected JSONL summary: %s", summaryJsonl)
	}

	// 3. Invalid file format
	badPath := filepath.Join(tmpDir, "unknown.xyz")
	_ = os.WriteFile(badPath, []byte("xyz"), 0o644)
	resBad := inspectDataTool(context.Background(), sc, map[string]any{"path": badPath})
	if resBad.Success || !strings.Contains(resBad.Error, "unsupported") {
		t.Fatalf("expected unsupported format error, got %v", resBad)
	}
}
