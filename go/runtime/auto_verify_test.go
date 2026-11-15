package main

import (
	"context"
	"strings"
	"testing"
)

func TestExtractFailureDiagnostics(t *testing.T) {
	sample := strings.Join([]string{
		"=== RUN   TestExample",
		"some noisy setup line 1",
		"some noisy setup line 2",
		"--- FAIL: TestExample (0.01s)",
		"    example_test.go:42: expected 10, got 5",
		"FAIL\tgithub.com/artheras/aria-code/go/runtime\t0.123s",
	}, "\n")

	diag := extractFailureDiagnostics(sample)
	if !strings.Contains(diag, "--- FAIL: TestExample") {
		t.Fatalf("expected failure marker in diagnostics, got %q", diag)
	}
	if !strings.Contains(diag, "example_test.go:42: expected 10, got 5") {
		t.Fatalf("expected trailing context line in diagnostics, got %q", diag)
	}
	if strings.Contains(diag, "some noisy setup line 1") {
		t.Fatalf("expected noisy setup lines to be filtered out, got %q", diag)
	}
}

func TestVerifyChangesToolEmptyWorkspace(t *testing.T) {
	dir := t.TempDir()
	res := verifyChangesTool(context.Background(), scope{workspace: dir}, map[string]any{
		"paths": []any{},
	})
	if !res.Success {
		t.Fatalf("expected empty workspace verification to succeed, got error: %s", res.Error)
	}
	if passed, _ := res.Data["passed"].(bool); !passed {
		t.Fatalf("expected passed=true, got %#v", res.Data)
	}
}
