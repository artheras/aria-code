package main

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"google.golang.org/genai"
)

// writer asks for one write_file call, then answers.
func writer(path, content string) modelProvider {
	return providerFunc(func(_ context.Context, h []*genai.Content, _ string, _ []*genai.Tool, emit func(string)) (*genai.Content, error) {
		last := h[len(h)-1]
		if len(last.Parts) > 0 && last.Parts[0].FunctionResponse != nil {
			emit("done")
			return genai.NewContentFromText("done", genai.RoleModel), nil
		}
		return &genai.Content{Role: genai.RoleModel, Parts: []*genai.Part{call("write_file", map[string]any{"path": path, "content": content})}}, nil
	})
}

func TestTaskWorksInAWorktreeAndAppliesBack(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("git apply through cmd.exe is covered by the integration suite")
	}
	s := approvalServer(t, config{}, appOptions{skipPermissions: true})
	gitRepo(t, s)
	s.provider = writer("feature.txt", "new feature\n")
	out, err := s.command(context.Background(), "t", "/task add a feature file")
	if err != nil || !strings.Contains(out, "/task diff") {
		t.Fatal(out, err)
	}
	if _, err := os.Stat(filepath.Join(s.scope.workspace, "feature.txt")); !os.IsNotExist(err) {
		t.Fatal("the task wrote into the user's checkout")
	}
	tasks, _ := s.loadTasks()
	if len(tasks) != 1 || statErr(filepath.Join(tasks[0].Path, "feature.txt")) != nil {
		t.Fatal("no file in the worktree", tasks)
	}
	if out, err := s.command(context.Background(), "t", "/task diff"); err != nil || !strings.Contains(out, "+new feature") {
		t.Fatal(out, err)
	}
	if out, _ := s.command(context.Background(), "t", "/tasks"); !strings.Contains(out, tasks[0].ID) {
		t.Fatal(out)
	}
	if out, err := s.command(context.Background(), "t", "/task apply"); err != nil || !strings.Contains(out, "Applied task") {
		t.Fatal(out, err)
	}
	if body, _ := os.ReadFile(filepath.Join(s.scope.workspace, "feature.txt")); string(body) != "new feature\n" {
		t.Fatalf("not applied: %q", body)
	}
	if out, err := s.command(context.Background(), "t", "/worktree discard "+tasks[0].ID[:4]); err != nil || !strings.Contains(out, "Discarded") {
		t.Fatal(out, err)
	}
	if statErr(tasks[0].Path) == nil {
		t.Fatal("worktree left behind")
	}
	if _, err := s.command(context.Background(), "t", "/task diff"); err == nil {
		t.Fatal("diff of a discarded task")
	}
}

func TestDelegateRunsInAFreshContextAndReportsBack(t *testing.T) {
	s := seeded(t)
	os.MkdirAll(filepath.Join(s.scope.workspace, ".aria", "agents"), 0o755)
	os.WriteFile(filepath.Join(s.scope.workspace, ".aria", "agents", "reviewer.md"), []byte("Reviews for security\nAlways check input validation."), 0o644)
	var seen []*genai.Content
	s.provider = providerFunc(func(_ context.Context, h []*genai.Content, _ string, _ []*genai.Tool, emit func(string)) (*genai.Content, error) {
		seen = h
		emit("found nothing")
		return genai.NewContentFromText("found nothing", genai.RoleModel), nil
	})
	out, err := s.command(context.Background(), "t", "/delegate --agent reviewer check the login form")
	if err != nil || !strings.Contains(out, "Agent reviewer reported") {
		t.Fatal(out, err)
	}
	if len(seen) != 1 || !strings.Contains(seen[0].Parts[0].Text, "input validation") || !strings.Contains(seen[0].Parts[0].Text, "check the login form") {
		t.Fatal("subagent did not start fresh with the agent's instructions", seen)
	}
	if len(s.contents) != 6 || !strings.Contains(s.contents[5].Parts[0].Text, "found nothing") {
		t.Fatal("report did not join the conversation", len(s.contents))
	}
	if out, _ := s.command(context.Background(), "t", "/agents"); !strings.Contains(out, "reviewer") || !strings.Contains(out, "Reviews for security") {
		t.Fatal(out)
	}
	if _, err := s.command(context.Background(), "t", "/delegate --agent nobody x"); err == nil {
		t.Fatal("unknown agent accepted")
	}
}

func TestWorkflowsParseRunAndRememberTrust(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a POSIX shell")
	}
	s := approvalServer(t, config{}, appOptions{skipPermissions: true})
	dir := filepath.Join(s.scope.workspace, ".aria", "workflows")
	os.MkdirAll(dir, 0o755)
	os.WriteFile(filepath.Join(dir, "check.yaml"), []byte("description: Quick check\nsteps:\n  - name: Echo\n    run: echo hello {{args}}\n  - command: /todo add {{args}}\n"), 0o644)
	os.WriteFile(filepath.Join(dir, "broken.yaml"), []byte("steps:\n  - run: a\n    prompt: b\n"), 0o644)
	out, err := s.command(context.Background(), "t", "/wf")
	if err != nil || !strings.Contains(out, "check") || !strings.Contains(out, "Quick check (2 steps)") || !strings.Contains(out, "exactly one of run, prompt, command") {
		t.Fatal(out, err)
	}
	out, err = s.command(context.Background(), "t", "/workflow run check release notes")
	if err != nil || !strings.Contains(out, "✓ 1. Echo") || !strings.Contains(out, "✓ 2.") || !strings.Contains(out, "release notes") {
		t.Fatal(out, err)
	}
	trusted := trustedWorkflows{}
	if err := readJSON(filepath.Join(s.opts.root, "trusted_workflows.json"), &trusted); err != nil || len(trusted) != 1 {
		t.Fatal("trust not remembered", trusted, err)
	}
	if _, err := s.command(context.Background(), "t", "/workflow run broken"); err == nil {
		t.Fatal("invalid workflow ran")
	}
	if out, err := s.command(context.Background(), "t", "/workflow new release"); err != nil || statErr(filepath.Join(dir, "release.yaml")) != nil {
		t.Fatal(out, err)
	}
	if _, err := parseWorkflow(filepath.Join(dir, "release.yaml")); err != nil {
		t.Fatal("template does not parse", err)
	}
}

func TestWorkflowFirstRunNeedsApproval(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	dir := filepath.Join(s.scope.workspace, ".aria", "workflows")
	os.MkdirAll(dir, 0o755)
	os.WriteFile(filepath.Join(dir, "x.yaml"), []byte("steps:\n  - command: /status\n"), 0o644)
	if out, _ := s.command(context.Background(), "t", "/workflow run x"); !strings.Contains(out, "Workflow not run") {
		t.Fatal("an untrusted workflow ran without approval", out)
	}
	if statErr(filepath.Join(s.opts.root, "trusted_workflows.json")) == nil {
		t.Fatal("trusted without approval")
	}
}

func TestConsultUsage(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	for _, text := range []string{"/consult", "/consult ollama", "/consult ollama/qwen3"} {
		if _, err := s.command(context.Background(), "t", text); err == nil || !strings.Contains(err.Error(), "usage") {
			t.Fatal(text, err)
		}
	}
}
