package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func initTestGitRepo(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	run := func(args ...string) {
		t.Helper()
		cmd := exec.Command("git", args...)
		cmd.Dir = dir
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v failed: %v\n%s", args, err, string(out))
		}
	}
	run("init")
	run("config", "user.email", "test@example.com")
	run("config", "user.name", "Test User")
	filePath := filepath.Join(dir, "main.go")
	if err := os.WriteFile(filePath, []byte("package main\nfunc main() {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run("add", "main.go")
	run("commit", "-m", "initial commit")
	return dir
}

func TestGitReadOnlyTools(t *testing.T) {
	dir := initTestGitRepo(t)
	s := &scope{workspace: dir}
	ctx := context.Background()

	// Modify file to test status and diff
	if err := os.WriteFile(filepath.Join(dir, "main.go"), []byte("package main\nfunc main() { println(1) }\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	statusRes := gitStatusTool(ctx, s, map[string]any{})
	if !statusRes.Success {
		t.Fatalf("gitStatusTool failed: %s", statusRes.Error)
	}
	if count, _ := statusRes.Data["changed_count"].(int); count != 1 {
		t.Fatalf("expected changed_count 1, got %v", statusRes.Data["changed_count"])
	}

	diffRes := gitDiffTool(ctx, s, map[string]any{"path": "main.go"})
	if !diffRes.Success {
		t.Fatalf("gitDiffTool failed: %s", diffRes.Error)
	}
	out, _ := diffRes.Data["output"].(string)
	if !strings.Contains(out, "println(1)") {
		t.Fatalf("expected diff to contain println(1), got %q", out)
	}

	logRes := gitLogTool(ctx, s, map[string]any{"limit": 5})
	if !logRes.Success {
		t.Fatalf("gitLogTool failed: %s", logRes.Error)
	}
	logOut, _ := logRes.Data["output"].(string)
	if !strings.Contains(logOut, "initial commit") {
		t.Fatalf("expected log to contain initial commit, got %q", logOut)
	}

	// Path traversal check
	escapeRes := gitDiffTool(ctx, s, map[string]any{"path": "../outside.txt"})
	if escapeRes.Success {
		t.Fatal("expected path traversal to fail")
	}
}
