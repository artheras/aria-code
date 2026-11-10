package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/genai"
)

func TestClearForgetsTheConversationButKeepsTheSession(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	id := s.snap.ID
	s.snap.Messages = []message{{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}}
	s.contents = []*genai.Content{genai.NewContentFromText("hi", "user")}
	out, err := s.command(context.Background(), "t", "/clear")
	if err != nil || !strings.Contains(out, "2 messages") {
		t.Fatal(out, err)
	}
	if len(s.snap.Messages) != 0 || len(s.contents) != 0 || !s.historyReset || s.snap.ID != id {
		t.Fatal("clear left history or changed the session", s.snap, s.contents)
	}
	if out, _ := s.command(context.Background(), "t", "/clear"); !strings.Contains(out, "already empty") {
		t.Fatal(out)
	}
	if _, err := s.command(context.Background(), "t", "/clear all"); err == nil {
		t.Fatal("extra arguments accepted")
	}
}

func TestStatusAndContextDescribeTheSession(t *testing.T) {
	s := approvalServer(t, config{"model": "gemini-3.8-pro", "approval_mode": "risk", "auto_approve_level": 2.0}, appOptions{})
	if err := os.WriteFile(filepath.Join(s.scope.workspace, "AGENTS.md"), []byte("rules"), 0o644); err != nil {
		t.Fatal(err)
	}
	s.contents = []*genai.Content{
		genai.NewContentFromText(strings.Repeat("x", 400), "user"),
		{Role: "model", Parts: []*genai.Part{{FunctionCall: &genai.FunctionCall{Name: "read_file", Args: map[string]any{"path": "a"}}}}},
	}
	status, err := s.command(context.Background(), "t", "/status")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"gemini-3.8-pro", s.snap.ID, "workspace-write", "risk (auto level 2)"} {
		if !strings.Contains(status, want) {
			t.Fatalf("status lacks %q:\n%s", want, status)
		}
	}
	ctx, err := s.command(context.Background(), "t", "/context")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"Tool calls     1", "AGENTS.md", "Est. tokens    ~1"} {
		if !strings.Contains(ctx, want) {
			t.Fatalf("context lacks %q:\n%s", want, ctx)
		}
	}
}

func TestToolsShowsHowEachToolIsApproved(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{allowedTools: []string{"write_file"}})
	if err := atomicJSON(filepath.Join(s.opts.root, "tool_policy.json"), map[string]any{"denied": []string{"run_command"}}); err != nil {
		t.Fatal(err)
	}
	out, err := s.command(context.Background(), "t", "/tools")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"run_command  write  denied", "write_file   write  allowed", "edit_file    write  asks", "read_file    read   allowed"} {
		if !strings.Contains(out, want) {
			t.Fatalf("tools lacks %q:\n%s", want, out)
		}
	}
}

func TestDiffSummarisesAndShowsThePatch(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	s := approvalServer(t, config{}, appOptions{})
	dir := s.scope.workspace
	if _, err := s.command(context.Background(), "t", "/diff"); err == nil || !strings.Contains(err.Error(), "not a git repository") {
		t.Fatal("expected a clear error outside git", err)
	}
	run := func(args ...string) {
		cmd := exec.Command("git", append([]string{"-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false"}, args...)...)
		cmd.Dir = dir
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatal(string(out), err)
		}
	}
	run("init", "-q")
	if err := os.WriteFile(filepath.Join(dir, "a.txt"), []byte("one\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run("add", "a.txt")
	run("commit", "-q", "-m", "init")
	if out, err := s.command(context.Background(), "t", "/diff"); err != nil || out != "No changes." {
		t.Fatal(out, err)
	}
	if err := os.WriteFile(filepath.Join(dir, "a.txt"), []byte("two\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	summary, err := s.command(context.Background(), "t", "/diff")
	if err != nil || !strings.Contains(summary, "M a.txt") || !strings.Contains(summary, "1 file changed") {
		t.Fatal(summary, err)
	}
	patch, err := s.command(context.Background(), "t", "/diff --patch")
	if err != nil || !strings.Contains(patch, "-one") || !strings.Contains(patch, "+two") {
		t.Fatal(patch, err)
	}
	if byPath, err := s.command(context.Background(), "t", "/diff a.txt"); err != nil || !strings.Contains(byPath, "+two") {
		t.Fatal(byPath, err)
	}
	if _, err := s.command(context.Background(), "t", "/diff --output=/tmp/x"); err == nil {
		t.Fatal("git options must not pass through")
	}
}
