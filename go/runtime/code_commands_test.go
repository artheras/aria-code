package main

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func codeServer(t *testing.T, options appOptions) *server {
	t.Helper()
	s := approvalServer(t, config{}, options)
	files := map[string]string{"main.go": "package main\n\nfunc main() {}\n", "docs/readme.md": "hello\nworld\n"}
	for name, body := range files {
		path := filepath.Join(s.scope.workspace, name)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return s
}

func TestSearchReadAndLs(t *testing.T) {
	s := codeServer(t, appOptions{})
	out, err := s.command(context.Background(), "t", "/search func\\s+main")
	if err != nil || !strings.Contains(out, "main.go:3") {
		t.Fatal(out, err)
	}
	if out, _ := s.command(context.Background(), "t", "/search nothing-here"); !strings.Contains(out, "No matches") {
		t.Fatal(out)
	}
	out, err = s.command(context.Background(), "t", "/read docs/readme.md 2 1")
	if err != nil || !strings.Contains(out, "world") || strings.Contains(out, "hello") {
		t.Fatal(out, err)
	}
	out, err = s.command(context.Background(), "t", "/ls")
	if err != nil || !strings.Contains(out, "main.go") || !strings.Contains(out, "docs"+string(filepath.Separator)) {
		t.Fatal(out, err)
	}
	if _, err := s.command(context.Background(), "t", "/read /etc/passwd"); err == nil {
		t.Fatal("read outside the roots")
	}
}

func TestWriteGoesThroughApprovalAndCheckpoints(t *testing.T) {
	refused := codeServer(t, appOptions{})
	if out, _ := refused.command(context.Background(), "t", "/write notes.txt\nhi"); !strings.Contains(out, "Not written") {
		t.Fatal("write without approval", out)
	}
	s := codeServer(t, appOptions{allowedTools: []string{"write_file"}})
	out, err := s.command(context.Background(), "t", "/write notes.txt\nfirst line\nsecond line")
	if err != nil || !strings.Contains(out, "Wrote") {
		t.Fatal(out, err)
	}
	body, _ := os.ReadFile(filepath.Join(s.scope.workspace, "notes.txt"))
	if string(body) != "first line\nsecond line\n" {
		t.Fatalf("%q", body)
	}
	out, err = s.command(context.Background(), "t", "/write --stage later.txt\nx")
	if err != nil || !strings.Contains(out, "Staged change") {
		t.Fatal(out, err)
	}
	if _, err := os.Stat(filepath.Join(s.scope.workspace, "later.txt")); !os.IsNotExist(err) {
		t.Fatal("staged write touched the file")
	}
	if _, err := s.command(context.Background(), "t", "/write notes.txt"); err == nil {
		t.Fatal("write without content accepted")
	}
}

func TestRunAndBuild(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a POSIX shell")
	}
	s := codeServer(t, appOptions{skipPermissions: true})
	out, err := s.command(context.Background(), "t", "/run echo hello")
	if err != nil || !strings.Contains(out, "hello") {
		t.Fatal(out, err)
	}
	if got := buildCommands(config{}, s.scope.workspace); got != nil {
		t.Fatal("no manifest, yet a build", got)
	}
	os.WriteFile(filepath.Join(s.scope.workspace, "go.mod"), []byte("module x\n"), 0o644)
	if got := buildCommands(config{}, s.scope.workspace); len(got) != 1 || got[0] != "go build ./..." {
		t.Fatal(got)
	}
	if _, err := s.command(context.Background(), "t", "/config set build_command=echo built ok"); err != nil {
		t.Fatal(err)
	}
	if out, err := s.command(context.Background(), "t", "/build"); err != nil || !strings.Contains(out, "built ok") {
		t.Fatal(out, err)
	}
}

func TestEditAndCodeTurns(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	prompt, _, handled, err := s.turnPrompt("/edit main.go rename run to start")
	if err != nil || !handled || !strings.Contains(prompt, "Edit main.go: rename run to start") {
		t.Fatal(prompt, err)
	}
	prompt, _, _, err = s.turnPrompt("/code a CSV parser --save csv.go")
	if err != nil || !strings.Contains(prompt, "a CSV parser") || !strings.Contains(prompt, "Save it to csv.go") {
		t.Fatal(prompt, err)
	}
	for _, text := range []string{"/edit main.go", "/code"} {
		if _, _, _, err := s.turnPrompt(text); err == nil {
			t.Fatal(text, "accepted")
		}
	}
}
