package main

import (
	"os"
	"strings"
	"testing"
	"time"
)

func TestDiagnosticTracePrivacyPersistenceAndBounds(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	secret := "PRIVATE_SECRET_CANARY"
	for i := 0; i < 515; i++ {
		s.recordDiagnostic("tool.completed", map[string]any{"tool": "read_file", "success": false, "path": secret, "error": secret, "params": secret})
	}
	s.recordDiagnostic("answer.delta", map[string]any{"text": secret})
	body, err := s.diagnosticBundle(time.Now())
	if err != nil || strings.Contains(body, secret) || !strings.Contains(body, "runtime_trace") {
		t.Fatal("unsafe bundle", err)
	}
	if len(s.trace) != 512 || s.traceDropped != 3 {
		t.Fatal("trace not bounded")
	}
	data, err := os.ReadFile(s.diagnosticTracePath())
	if err != nil || strings.Contains(string(data), secret) {
		t.Fatal("unsafe persisted trace", err)
	}
	restored := &server{opts: s.opts, snap: s.snap}
	restored.recordDiagnostic("turn.completed", map[string]any{"status": "failed"})
	if len(restored.trace) != 512 || restored.trace[511].Status != "failed" {
		t.Fatal("trace not resumed")
	}
	if _, ok := safeDiagnosticEvent(secret, map[string]any{}); ok {
		t.Fatal("unknown event accepted")
	}
}
