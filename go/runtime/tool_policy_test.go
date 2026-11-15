package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestPersistentToolPolicyWithParams(t *testing.T) {
	root := t.TempDir()
	policyJSON := `{
  "allowed": ["run_command(pytest *)", "write_file(tests/*.py)"],
  "denied": ["run_command(rm -rf *)", "write_file(*.env)"],
  "ask_always": ["write_file"]
}`
	if err := os.WriteFile(filepath.Join(root, "tool_policy.json"), []byte(policyJSON), 0o600); err != nil {
		t.Fatal(err)
	}

	cases := []struct {
		name   string
		params map[string]any
		want   string
	}{
		{"run_command", map[string]any{"command": "pytest tests/test_a.py"}, "allow"},
		{"run_command", map[string]any{"command": "rm -rf /tmp/x"}, "deny"},
		{"run_command", map[string]any{"command": "ls -la"}, "default"},
		{"run_command", nil, "default"},
		{"write_file", map[string]any{"path": "tests/unit/test_b.py"}, "allow"},
		{"write_file", map[string]any{"path": ".env"}, "deny"},
		{"write_file", map[string]any{"path": "src/main.py"}, "ask"},
		{"read_file", map[string]any{"path": "README.md"}, "default"},
	}

	for _, tc := range cases {
		got, err := persistentToolPolicyWithParams(root, tc.name, tc.params)
		if err != nil {
			t.Fatalf("unexpected error for %s: %v", tc.name, err)
		}
		if got != tc.want {
			t.Errorf("persistentToolPolicyWithParams(%q, %v) = %q, want %q", tc.name, tc.params, got, tc.want)
		}
	}
}
