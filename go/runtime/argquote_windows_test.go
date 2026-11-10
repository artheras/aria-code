//go:build windows

package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// A batch file splits unquoted arguments at = , and ;.
func TestBatchFilesReceiveArgumentsWithEqualsWhole(t *testing.T) {
	script := filepath.Join(t.TempDir(), "echo args.cmd")
	os.WriteFile(script, []byte("@echo [%~1]\r\n"), 0o644)
	out, err := shellCommand(context.Background(), argQuote(script)+" "+argQuote("--screenshot=C:\\x\\y.png")).CombinedOutput()
	if err != nil || !strings.Contains(string(out), "[--screenshot=C:\\x\\y.png]") {
		t.Fatal(string(out), err)
	}
}
