package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/genai"
)

func writeSkill(t *testing.T, dir, name, body string) {
	t.Helper()
	path := filepath.Join(dir, name, "SKILL.md")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestSkillsAreFoundListedAndActivated(t *testing.T) {
	t.Setenv("ARIA_SKILLS_PATH", "")
	t.Setenv("HOME", t.TempDir())
	t.Setenv("USERPROFILE", os.Getenv("HOME"))
	s := approvalServer(t, config{}, appOptions{})
	project := filepath.Join(s.scope.workspace, ".aria", "skills")
	writeSkill(t, project, "release-notes", "---\nname: release-notes\ndescription: Write release notes from git log\n---\nRead git log since the last tag and group changes.\n")
	writeSkill(t, project, "broken", "no front matter")
	writeSkill(t, filepath.Join(s.opts.root, "skills"), "release-notes", "---\nname: release-notes\ndescription: Shadowed copy\n---\nfirst root wins\n")
	out, err := s.command(context.Background(), "t", "/skills")
	if err != nil || !strings.Contains(out, "release-notes") || strings.Contains(out, "broken") {
		t.Fatal(out, err)
	}
	if out, _ := s.command(context.Background(), "t", "/skills show release-notes"); !strings.Contains(out, "first root wins") {
		t.Fatal("the state directory should come before the project", out)
	}
	prompt, _, handled, err := s.turnPrompt("/skills use release-notes for v2.1")
	if err != nil || !handled || !strings.Contains(prompt, "first root wins") || !strings.Contains(prompt, "Task: for v2.1") {
		t.Fatal(prompt, err)
	}
	if _, _, handled, _ := s.turnPrompt("/skills"); handled {
		t.Fatal("listing treated as a turn")
	}
	var system string
	s.provider = providerFunc(func(_ context.Context, _ []*genai.Content, sys string, _ []*genai.Tool, emit func(string)) (*genai.Content, error) {
		system = sys
		emit("ok")
		return genai.NewContentFromText("ok", genai.RoleModel), nil
	})
	s.done = make(chan struct{})
	s.runTurn(context.Background(), "t", "draft notes with $release-notes please")
	if !strings.Contains(system, "activated these skills") || !strings.Contains(system, "first root wins") {
		t.Fatal("$skill did not reach the model", system)
	}
}

func TestVisionPrompt(t *testing.T) {
	prompt, err := visionPrompt("chart.png what trend?")
	if err != nil || prompt != `what trend? @file:"chart.png"` {
		t.Fatal(prompt, err)
	}
	if prompt, _ := visionPrompt("a.png"); !strings.Contains(prompt, "Describe this image") {
		t.Fatal(prompt)
	}
	if _, err := visionPrompt(""); err == nil {
		t.Fatal("empty accepted")
	}
}
