package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
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

func TestWorldStatsAndDPOPairs(t *testing.T) {
	root := t.TempDir()
	ws := t.TempDir()

	recordWorldTransition(root, ws, "edit_file", map[string]any{"path": "a.go"}, toolResult{Success: false, Error: "old_string not found"}, 10*time.Millisecond)
	recordWorldTransition(root, ws, "edit_file", map[string]any{"path": "a.go"}, toolResult{Success: true}, 12*time.Millisecond)
	recordWorldTransition(root, ws, "verify_changes", map[string]any{"paths": []string{"a.go"}}, toolResult{Success: true}, 50*time.Millisecond)

	entries := loadWorldTransitions(root, 20)
	stats := summarizeWorldTransitions(entries)
	codeStat := stats["code"]
	if codeStat.Total != 3 || codeStat.Succeeded != 2 || codeStat.Failed != 1 {
		t.Fatalf("unexpected code domain stats: %+v", codeStat)
	}

	pairs := ExportWorldDPOPairs(entries)
	if len(pairs) != 1 {
		t.Fatalf("expected 1 DPO pair, got %d", len(pairs))
	}
	if pairs[0]["domain"] != "code" || pairs[0]["chosen_tool"] != "edit_file" {
		t.Fatalf("unexpected DPO pair: %+v", pairs[0])
	}
}

func TestCursorAndClaudeRulesAndMCPCompat(t *testing.T) {
	root := t.TempDir()
	ws := t.TempDir()

	// 1. .cursor/mcp.json with mcpServers dictionary
	cursorMCPDir := filepath.Join(root, ".cursor")
	if err := os.MkdirAll(cursorMCPDir, 0o755); err != nil {
		t.Fatal(err)
	}
	mcpJSON := `{"mcpServers":{"github":{"command":"npx","args":["-y","@modelcontextprotocol/server-github"]}}}`
	if err := os.WriteFile(filepath.Join(cursorMCPDir, "mcp.json"), []byte(mcpJSON), 0o644); err != nil {
		t.Fatal(err)
	}
	servers, err := loadMCPConfig(root)
	if err != nil || len(servers) != 1 || servers[0].Name != "github" {
		t.Fatalf("expected github server from .cursor/mcp.json, got %v, err=%v", servers, err)
	}

	// 2. .cursor/rules/*.mdc and .claude/rules/*.md in projectInstructions
	cursorRules := filepath.Join(ws, ".cursor", "rules")
	claudeRules := filepath.Join(ws, ".claude", "rules")
	_ = os.MkdirAll(cursorRules, 0o755)
	_ = os.MkdirAll(claudeRules, 0o755)
	_ = os.WriteFile(filepath.Join(cursorRules, "frontend.mdc"), []byte("Always use semantic HTML."), 0o644)
	_ = os.WriteFile(filepath.Join(claudeRules, "backend.md"), []byte("Keep Go handlers thread-safe."), 0o644)

	instructions := projectInstructions(root, ws)
	if !strings.Contains(instructions, "Always use semantic HTML.") || !strings.Contains(instructions, "Keep Go handlers thread-safe.") {
		t.Fatalf("expected layered rules in projectInstructions, got %q", instructions)
	}

	// 3. External CLI argument builder
	bin, args, err := buildExternalCLIArgs("claude", "Fix tests")
	if err != nil || bin != "claude" || len(args) != 2 || args[0] != "-p" {
		t.Fatalf("unexpected claude CLI args: %s %v %v", bin, args, err)
	}
	bin2, args2, err := buildExternalCLIArgs("codex", "Fix tests")
	if err != nil || bin2 != "codex" || len(args2) != 2 || args2[0] != "exec" {
		t.Fatalf("unexpected codex CLI args: %s %v %v", bin2, args2, err)
	}
}
