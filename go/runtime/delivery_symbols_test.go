package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDeliveryNamesTheDefinitionADiffChanged(t *testing.T) {
	root := resolveExisting(t.TempDir())
	path := filepath.Join(root, "app.py")
	after := "def keep():\n    return 1\n\ndef change():\n    return 2\n"
	if err := os.WriteFile(path, []byte(after), 0o644); err != nil {
		t.Fatal(err)
	}
	diff := "" +
		"--- a/app.py\n" +
		"+++ b/app.py\n" +
		"@@ -3,3 +3,3 @@\n" +
		" \n" +
		" def change():\n" +
		"-    return 1\n" +
		"+    return 2\n"
	ledger := newDeliveryLedger(root)
	ledger.Record("edit_file", map[string]any{"path": path}, map[string]any{
		"success": true, "data": map[string]any{"path": path, "diff": diff},
	})
	report := ledger.Report(nil, nil, nil, "completed")
	if len(report.Changed) != 1 || strings.Join(report.Changed[0].Symbols, ",") != "change() modified" {
		t.Fatalf("%v", report.Changed[0])
	}

	t.Setenv("ARIA_HOME", t.TempDir())
	cache, err := graphCachePath(root)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(cache), 0o755); err != nil {
		t.Fatal(err)
	}
	payload := map[string]any{
		"version": 1, "root": root,
		"files":    map[string]any{},
		"services": map[string]string{},
		"defs":     map[string][]string{},
		"refs":     map[string][]string{"change": {"app/cli.py", "tests/test_app.py"}},
	}
	body, _ := json.Marshal(payload)
	if err := os.WriteFile(cache, body, 0o644); err != nil {
		t.Fatal(err)
	}
	report = ledger.Report(nil, nil, nil, "completed")
	if strings.Join(report.Changed[0].TestedBy, ",") != "tests/test_app.py" {
		t.Fatalf("tested by %v", report.Changed[0].TestedBy)
	}
}

func TestADiffThatDoesNotApplyNamesNothing(t *testing.T) {
	root := resolveExisting(t.TempDir())
	path := filepath.Join(root, "app.py")
	if err := os.WriteFile(path, []byte("def change():\n    return 2\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	ledger := newDeliveryLedger(root)
	ledger.Record("edit_file", nil, map[string]any{
		"success": true, "data": map[string]any{"path": path, "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-missing\n+nope\n"},
	})
	report := ledger.Report(nil, nil, nil, "completed")
	if len(report.Changed[0].Symbols) != 0 {
		t.Fatal(report.Changed[0].Symbols)
	}
}
