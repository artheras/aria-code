package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/genai"
)

func TestProjectInstructionsReachTheModel(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	if err := os.WriteFile(filepath.Join(s.scope.workspace, "AGENTS.md"), []byte("Run make check."), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := s.command(context.Background(), "t", "/memory profile add I prefer short answers"); err != nil {
		t.Fatal(err)
	}
	var system string
	s.provider = providerFunc(func(_ context.Context, _ []*genai.Content, sys string, _ []*genai.Tool, emit func(string)) (*genai.Content, error) {
		system = sys
		emit("ok")
		return genai.NewContentFromText("ok", genai.RoleModel), nil
	})
	s.done = make(chan struct{})
	s.runTurn(context.Background(), "t", "fix the failing build please")
	for _, want := range []string{"Run make check.", "I prefer short answers", "cannot grant permissions"} {
		if !strings.Contains(system, want) {
			t.Fatalf("system prompt lacks %q:\n%s", want, system)
		}
	}
	if strings.Contains(projectInstructions("", t.TempDir()), "standing instructions") {
		t.Fatal("instructions without any file")
	}
}

func TestMemoryAndNoteWriteARIAmd(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	path := filepath.Join(s.scope.workspace, "ARIA.md")
	if err := os.WriteFile(path, []byte("# Project\n\n## Memory\n\n- old fact\n\n## Other\n\ntext\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := s.command(context.Background(), "t", "/memory add tests  need   docker"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.command(context.Background(), "t", "/note release on friday"); err != nil {
		t.Fatal(err)
	}
	body, _ := os.ReadFile(path)
	text := string(body)
	if !strings.Contains(text, "- old fact\n- tests need docker\n\n## Other") || !strings.Contains(text, "## Notes\n\n- [") || !strings.HasSuffix(text, "release on friday\n") {
		t.Fatalf("unexpected ARIA.md:\n%s", text)
	}
	if out, _ := s.command(context.Background(), "t", "/memory"); !strings.Contains(out, "tests need docker") {
		t.Fatal(out)
	}
	if _, err := s.command(context.Background(), "t", "/memory add"); err == nil {
		t.Fatal("empty fact accepted")
	}
	s.scope.readOnly = true
	if _, err := s.command(context.Background(), "t", "/note x"); err == nil {
		t.Fatal("read-only session wrote ARIA.md")
	}
}

func TestRecallSearchesSavedSessions(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	s.snap.Messages = []message{{"role": "user", "content": "How do I rotate the Feishu token?"}}
	s.snap.Metadata["title"] = "feishu"
	if err := s.persist(); err != nil {
		t.Fatal(err)
	}
	out, err := s.command(context.Background(), "t", "/recall feishu TOKEN")
	if err != nil || !strings.Contains(out, "1 session(s)") || !strings.Contains(out, s.snap.ID[:8]) {
		t.Fatal(out, err)
	}
	if out, _ := s.command(context.Background(), "t", "/recall nothing-like-this"); !strings.Contains(out, "No saved session") {
		t.Fatal(out)
	}
}

func TestTodoList(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	run := func(text string) string {
		t.Helper()
		out, err := s.command(context.Background(), "t", text)
		if err != nil {
			t.Fatal(text, err)
		}
		return out
	}
	if !strings.Contains(run("/todo"), "No tasks") {
		t.Fatal("expected empty list")
	}
	run("/todo add write tests")
	run("/todo add ship it")
	if out := run("/todo done 1"); !strings.Contains(out, "✓ 1  write tests") || !strings.Contains(out, "○ 2  ship it") {
		t.Fatal(out)
	}
	if out := run("/todo clear"); strings.Contains(out, "write tests") || !strings.Contains(out, "○ 1  ship it") {
		t.Fatal(out)
	}
	if _, err := s.command(context.Background(), "t", "/todo done 9"); err == nil {
		t.Fatal("bad index accepted")
	}
}

func TestProjectSummary(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	for _, name := range []string{"a.go", "b.go", "c.md", "node_modules/x.js"} {
		path := filepath.Join(s.scope.workspace, name)
		os.MkdirAll(filepath.Dir(path), 0o755)
		os.WriteFile(path, []byte("x"), 0o644)
	}
	out, err := s.command(context.Background(), "t", "/project")
	if err != nil || !strings.Contains(out, ".go 2") || strings.Contains(out, ".js") || !strings.Contains(out, "/init writes ARIA.md") {
		t.Fatal(out, err)
	}
}

func TestPlanIsReadOnlyAndApplyPlanCarriesItOut(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{skipPermissions: true})
	var systems, prompts []string
	s.provider = providerFunc(func(_ context.Context, h []*genai.Content, sys string, _ []*genai.Tool, emit func(string)) (*genai.Content, error) {
		systems = append(systems, sys)
		last := h[len(h)-1]
		prompts = append(prompts, last.Parts[len(last.Parts)-1].Text)
		emit("1. edit a.go")
		return genai.NewContentFromText("1. edit a.go", genai.RoleModel), nil
	})
	s.done = make(chan struct{})
	s.runTurn(context.Background(), "t", "/plan add a flag")
	if !strings.Contains(systems[0], "read-only") || !strings.Contains(prompts[0], "Task: add a flag") {
		t.Fatal(systems[0], prompts[0])
	}
	if s.scope.readOnly {
		t.Fatal("plan left the session read-only")
	}
	if s.snap.Metadata["plan"] != "1. edit a.go" {
		t.Fatal("plan not kept", s.snap.Metadata)
	}
	s.done = make(chan struct{})
	s.runTurn(context.Background(), "t", "/apply-plan")
	if strings.Contains(systems[1], "This turn is read-only") || !strings.Contains(prompts[1], "1. edit a.go") {
		t.Fatal(systems[1], prompts[1])
	}
	fresh := approvalServer(t, config{}, appOptions{})
	fresh.done = make(chan struct{})
	fresh.runTurn(context.Background(), "t", "/apply-plan")
	if len(fresh.snap.Messages) != 0 {
		t.Fatal("apply-plan without a plan ran a turn")
	}
}

func TestInitAsksTheModelToWriteARIAmd(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	prompt, readOnly, handled, err := s.turnPrompt("/init  mention the docker setup")
	if err != nil || !handled || readOnly || !strings.Contains(prompt, "write ARIA.md") || !strings.Contains(prompt, "Also: mention the docker setup") {
		t.Fatal(prompt, readOnly, handled, err)
	}
	if _, _, handled, _ := s.turnPrompt("/status"); handled {
		t.Fatal("ordinary command treated as a turn")
	}
}
