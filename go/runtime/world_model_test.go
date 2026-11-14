package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestWorldTransitionRecordAndLoad(t *testing.T) {
	root := t.TempDir()
	ws := t.TempDir()

	recordWorldTransition(root, ws, "verify_changes", map[string]any{"paths": []string{"agent.go"}}, toolResult{Success: true}, 42*time.Millisecond)
	recordWorldTransition(root, ws, "edit_file", map[string]any{"path": "agent.go", "api_key": "sk-secret123456"}, toolResult{Success: false, Error: "old_string not found"}, 5*time.Millisecond)

	loaded := loadWorldTransitions(root, 10)
	if len(loaded) != 2 {
		t.Fatalf("expected 2 transitions, got %d", len(loaded))
	}
	if loaded[0].Domain != "code" || loaded[0].RewardScore != 1.0 {
		t.Fatalf("unexpected first transition: %+v", loaded[0])
	}
	if loaded[1].OutcomeOK || loaded[1].RewardScore != -1.0 {
		t.Fatalf("unexpected second transition: %+v", loaded[1])
	}
}

func TestSimulateActionToolPredictsImpactAndRisk(t *testing.T) {
	ws := t.TempDir()
	if err := os.WriteFile(filepath.Join(ws, "agent.go"), []byte("package main\nfunc run() {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(ws, "app.py"), []byte("def hello():\n    return 'ok'\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	res := simulateActionTool(context.Background(), &scope{workspace: ws}, map[string]any{
		"paths": []any{"agent.go", "app.py"},
	})
	if !res.Success {
		t.Fatalf("expected simulateActionTool to succeed, got %s", res.Error)
	}
	if risk, _ := res.Data["predicted_risk"].(string); risk != "medium" {
		t.Fatalf("expected medium risk for multi-file core change, got %q", risk)
	}
	if cmds, ok := res.Data["recommended_checks"].([]string); !ok || len(cmds) == 0 {
		t.Fatalf("expected recommended_checks for python change, got %v", res.Data["recommended_checks"])
	}
}
