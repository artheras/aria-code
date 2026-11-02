//go:build windows

package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// A command that starts with a quoted program path, like /browser's, used
// to reach cmd.exe as \"…\" and fail.
func TestCmdKeepsQuotedProgramPaths(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "with space")
	os.MkdirAll(dir, 0o755)
	script := filepath.Join(dir, "say.cmd")
	os.WriteFile(script, []byte("@echo said %~1\r\n"), 0o644)
	out, err := shellCommand(context.Background(), `"`+script+`" "two words"`).CombinedOutput()
	if err != nil || !strings.Contains(string(out), "said two words") {
		t.Fatal(string(out), err)
	}
}
