package main

import (
	"strings"
	"testing"
)

func TestLoopGuardOscillationAndReadThreshold(t *testing.T) {
	g := newLoopGuard(3)

	// Consecutive read-only calls trigger advisory at threshold, then reset on write.
	for i := 0; i < 2; i++ {
		_, adv := g.record("read_file", map[string]any{"path": "a.go"}, toolResult{Success: true, Data: map[string]any{}}, false)
		if adv != "" {
			t.Fatalf("unexpected advisory before threshold: %q", adv)
		}
	}
	_, adv := g.record("search_code", map[string]any{"query": "foo"}, toolResult{Success: true, Data: map[string]any{}}, false)
	if !strings.Contains(adv, "consecutive read-only") {
		t.Fatalf("expected read-only advisory, got: %q", adv)
	}

	// File-hash oscillation A -> B -> A triggers shouldBreak.
	_, adv = g.record("edit_file", map[string]any{"path": "a.go"}, toolResult{Success: true, Data: map[string]any{"path": "a.go", "after_hash": "hashA"}}, true)
	if adv != "" || g.shouldBreak {
		t.Fatalf("unexpected break on first hash")
	}
	_, adv = g.record("edit_file", map[string]any{"path": "a.go"}, toolResult{Success: true, Data: map[string]any{"path": "a.go", "after_hash": "hashB"}}, true)
	if adv != "" || g.shouldBreak {
		t.Fatalf("unexpected break on second hash")
	}
	_, adv = g.record("edit_file", map[string]any{"path": "a.go"}, toolResult{Success: true, Data: map[string]any{"path": "a.go", "after_hash": "hashA"}}, true)
	if !g.shouldBreak || !strings.Contains(adv, "oscillating") {
		t.Fatalf("expected oscillation break and advisory, got shouldBreak=%v adv=%q", g.shouldBreak, adv)
	}
}
