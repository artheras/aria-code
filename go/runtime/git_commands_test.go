package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"google.golang.org/genai"
)

func gitRepo(t *testing.T, s *server) func(...string) {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	run := func(args ...string) {
		t.Helper()
		cmd := exec.Command("git", append([]string{"-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false"}, args...)...)
		cmd.Dir = s.scope.workspace
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatal(string(out), err)
		}
	}
	run("init", "-q")
	// /commit runs plain git, which needs an identity; CI runners have none.
	run("config", "user.name", "t")
	run("config", "user.email", "t@example.invalid")
	if err := os.WriteFile(filepath.Join(s.scope.workspace, "a.txt"), []byte("one\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run("add", "a.txt")
	run("commit", "-q", "-m", "init")
	return run
}

func TestGitReadsDirectlyAndRefusesUnsafeOptions(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	if _, err := s.command(context.Background(), "t", "/git"); err == nil || !strings.Contains(err.Error(), "not a git repository") {
		t.Fatal(err)
	}
	gitRepo(t, s)
	if out, err := s.command(context.Background(), "t", "/git log --oneline"); err != nil || !strings.Contains(out, "init") {
		t.Fatal(out, err)
	}
	if _, err := s.command(context.Background(), "t", "/git diff --output=/tmp/x"); err == nil {
		t.Fatal("--output accepted")
	}
	// A command that changes the repository needs approval; this headless
	// test server has none to give.
	if out, _ := s.command(context.Background(), "t", "/git checkout -b x"); !strings.Contains(out, "not executed") {
		t.Fatal("checkout ran without approval", out)
	}
}

func TestCommitWritesAMessageAndCommits(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{skipPermissions: true})
	gitRepo(t, s)
	if out, _ := s.command(context.Background(), "t", "/commit"); out != "Nothing to commit." {
		t.Fatal(out)
	}
	if err := os.WriteFile(filepath.Join(s.scope.workspace, "b.txt"), []byte("new\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var asked string
	s.provider = providerFunc(func(_ context.Context, h []*genai.Content, _ string, _ []*genai.Tool, _ func(string)) (*genai.Content, error) {
		asked = h[0].Parts[0].Text
		return genai.NewContentFromText("```\nAdd b.txt\n```", genai.RoleModel), nil
	})
	if _, err := s.command(context.Background(), "t", "/commit"); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(asked, "b.txt") {
		t.Fatal("message prompt lacks the new file", asked)
	}
	log, err := git(context.Background(), s.scope.workspace, nil, 0x7fffffff, "log", "-1", "--format=%s")
	if err != nil || log != "Add b.txt" {
		t.Fatal(log, err)
	}
}

func TestReviewUsesTheIsolatedReviewer(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	gitRepo(t, s)
	if out, _ := s.command(context.Background(), "t", "/review"); out != "Nothing to review." {
		t.Fatal(out)
	}
	if err := os.WriteFile(filepath.Join(s.scope.workspace, "a.txt"), []byte("two\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var system, prompt string
	var tools []*genai.Tool
	s.provider = providerFunc(func(_ context.Context, h []*genai.Content, sys string, ts []*genai.Tool, _ func(string)) (*genai.Content, error) {
		system, prompt, tools = sys, h[0].Parts[0].Text, ts
		return genai.NewContentFromText(`{"verdict":"changes_requested","findings":[{"severity":"blocking","file":"a.txt","line":1,"issue":"wrong value"}]}`, genai.RoleModel), nil
	})
	out, err := s.command(context.Background(), "t", "/review")
	if err != nil || !strings.Contains(out, "1 blocking finding") || !strings.Contains(out, "wrong value") {
		t.Fatal(out, err)
	}
	if system != reviewerSystem || tools != nil || !strings.Contains(prompt, "+two") {
		t.Fatal("review was not isolated", system, tools)
	}
	if _, err := s.command(context.Background(), "t", "/review --base --output=x"); err == nil {
		t.Fatal("option accepted as base")
	}
}

func TestTestRunsTheDeclaredChecks(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses POSIX true/false")
	}
	s := approvalServer(t, config{"acceptance_commands": []any{"true", "false", "echo never"}}, appOptions{skipPermissions: true})
	out, err := s.command(context.Background(), "t", "/test")
	if err == nil || !strings.Contains(out, "✓ true") || !strings.Contains(out, "✗ false") || strings.Contains(out, "never") {
		t.Fatal(out, err)
	}
}

func TestEngineeringTurnsAndPlanKeeping(t *testing.T) {
	for _, tc := range []struct {
		text, want string
		readOnly   bool
	}{
		{"/fix", "Fix the failing checks", false},
		{"/fix the login crash", "Fix this: the login crash", false},
		{"/bug totals are off by one", "Investigate this bug", false},
		{"/trace panic: nil map", "read-only", true},
		{"/quality", "read-only", true},
	} {
		s := approvalServer(t, config{}, appOptions{})
		prompt, readOnly, handled, err := s.turnPrompt(tc.text)
		if err != nil || !handled || readOnly != tc.readOnly || !strings.Contains(prompt, tc.want) {
			t.Fatal(tc.text, prompt, readOnly, handled, err)
		}
	}
	for _, text := range []string{"/bug", "/trace"} {
		s := approvalServer(t, config{}, appOptions{})
		if _, _, _, err := s.turnPrompt(text); err == nil {
			t.Fatal(text, "accepted without an argument")
		}
	}
	s := approvalServer(t, config{}, appOptions{})
	s.snap.Metadata["plan"] = "keep me"
	s.provider = providerFunc(func(_ context.Context, _ []*genai.Content, _ string, _ []*genai.Tool, emit func(string)) (*genai.Content, error) {
		emit("diagnosis")
		return genai.NewContentFromText("diagnosis", genai.RoleModel), nil
	})
	s.done = make(chan struct{})
	s.runTurn(context.Background(), "t", "/trace boom")
	if s.snap.Metadata["plan"] != "keep me" || s.scope.readOnly {
		t.Fatal("trace replaced the plan or left the session read-only", s.snap.Metadata)
	}
}
