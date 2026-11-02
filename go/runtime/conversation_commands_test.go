package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/genai"
)

func seeded(t *testing.T) *server {
	t.Helper()
	s := approvalServer(t, config{}, appOptions{})
	s.snap.Messages = []message{
		{"role": "user", "content": "first question"}, {"role": "assistant", "content": "first answer"},
		{"role": "user", "content": "second question"}, {"role": "assistant", "content": "second answer"},
	}
	s.contents = []*genai.Content{
		genai.NewContentFromText("first question", genai.RoleUser), genai.NewContentFromText("first answer", genai.RoleModel),
		genai.NewContentFromText("second question", genai.RoleUser), genai.NewContentFromText("second answer", genai.RoleModel),
	}
	return s
}

func TestBtwAndRecapLeaveTheConversationAlone(t *testing.T) {
	s := seeded(t)
	var sent []*genai.Content
	var tools []*genai.Tool
	s.provider = providerFunc(func(_ context.Context, h []*genai.Content, _ string, ts []*genai.Tool, _ func(string)) (*genai.Content, error) {
		sent, tools = h, ts
		return genai.NewContentFromText("aside", genai.RoleModel), nil
	})
	out, err := s.command(context.Background(), "t", "/btw what  was first?")
	if err != nil || out != "aside" {
		t.Fatal(out, err)
	}
	if len(sent) != 5 || !strings.Contains(sent[4].Parts[0].Text, "what  was first?") || tools != nil {
		t.Fatal("side question did not carry the history without tools", sent, tools)
	}
	if out, err := s.command(context.Background(), "t", "/recap"); err != nil || out != "aside" {
		t.Fatal(out, err)
	}
	if len(s.contents) != 4 || len(s.snap.Messages) != 4 {
		t.Fatal("aside questions changed the conversation")
	}
	if _, err := s.command(context.Background(), "t", "/btw"); err == nil {
		t.Fatal("empty /btw accepted")
	}
}

func TestCompactReplacesModelHistoryWithASummary(t *testing.T) {
	s := seeded(t)
	s.provider = providerFunc(func(_ context.Context, h []*genai.Content, _ string, _ []*genai.Tool, _ func(string)) (*genai.Content, error) {
		if !strings.Contains(h[len(h)-1].Parts[0].Text, "tests") {
			t.Fatal("focus not passed on")
		}
		return genai.NewContentFromText("they asked two questions", genai.RoleModel), nil
	})
	out, err := s.command(context.Background(), "t", "/compact keep the tests")
	if err != nil || !strings.Contains(out, "Compacted") {
		t.Fatal(out, err)
	}
	if len(s.contents) != 2 || !strings.Contains(s.contents[0].Parts[0].Text, "they asked two questions") || len(s.snap.Messages) != 4 {
		t.Fatal("compact did not replace the model history and keep the transcript", s.contents)
	}
	small := approvalServer(t, config{}, appOptions{})
	if out, _ := small.command(context.Background(), "t", "/compact"); !strings.Contains(out, "nothing to compact") {
		t.Fatal(out)
	}
}

func TestCompactFailureKeepsTheHistory(t *testing.T) {
	s := seeded(t)
	s.provider = providerFunc(func(context.Context, []*genai.Content, string, []*genai.Tool, func(string)) (*genai.Content, error) {
		return genai.NewContentFromText("  ", genai.RoleModel), nil
	})
	if _, err := s.command(context.Background(), "t", "/compact"); err == nil || !strings.Contains(err.Error(), "unchanged") {
		t.Fatal(err)
	}
	if len(s.contents) != 4 {
		t.Fatal("failed compact lost history")
	}
}

func TestHistoryRenameAndFork(t *testing.T) {
	s := seeded(t)
	out, err := s.command(context.Background(), "t", "/history")
	if err != nil || !strings.Contains(out, "1  first question") || !strings.Contains(out, "2  second question") {
		t.Fatal(out, err)
	}
	if out, _ := s.command(context.Background(), "t", "/history 1"); strings.Contains(out, "first") {
		t.Fatal("count ignored", out)
	}
	if _, err := s.command(context.Background(), "t", "/history zero"); err == nil {
		t.Fatal("bad count accepted")
	}
	if out, err := s.command(context.Background(), "t", "/rename  Bug   hunt "); err != nil || s.snap.Metadata["title"] != "Bug hunt" {
		t.Fatal(out, err)
	}
	out, err = s.command(context.Background(), "t", "/fork")
	if err != nil {
		t.Fatal(err)
	}
	id := strings.Fields(strings.TrimPrefix(out, "Forked to "))[0]
	fork, err := loadSession(s.opts.root, id)
	if err != nil {
		t.Fatal(err)
	}
	contents, err := sessionContents(fork)
	if err != nil || len(fork.Messages) != 4 || len(contents) != 4 || fork.Metadata["title"] != "Bug hunt (fork)" || fork.Metadata["forked_from"] != s.snap.ID {
		t.Fatal(fork, contents, err)
	}
	fork.Messages[0]["content"] = "changed"
	if s.snap.Messages[0]["content"] != "first question" {
		t.Fatal("fork shares messages with the open session")
	}
}

func TestRegenSendsTheLastPromptAgain(t *testing.T) {
	s := seeded(t)
	var prompts []string
	s.provider = providerFunc(func(_ context.Context, h []*genai.Content, _ string, _ []*genai.Tool, emit func(string)) (*genai.Content, error) {
		prompts = append(prompts, h[len(h)-1].Parts[len(h[len(h)-1].Parts)-1].Text)
		emit("better answer")
		return genai.NewContentFromText("better answer", genai.RoleModel), nil
	})
	s.done = make(chan struct{})
	s.runTurn(context.Background(), "t", "/regen")
	if len(prompts) != 1 || !strings.Contains(prompts[0], "second question") {
		t.Fatal("regen did not resend the prompt", prompts)
	}
	last := s.snap.Messages[len(s.snap.Messages)-1]
	if len(s.snap.Messages) != 4 || last["content"] != "better answer" {
		t.Fatal("regen did not replace the last exchange", s.snap.Messages)
	}
	empty := approvalServer(t, config{}, appOptions{})
	empty.done = make(chan struct{})
	empty.runTurn(context.Background(), "t", "/regen")
	if len(empty.snap.Messages) != 0 {
		t.Fatal("regen with nothing to regenerate ran a turn")
	}
}

func TestSaveAndLoadAreExportAndImport(t *testing.T) {
	s := seeded(t)
	s.opts.allowedTools = []string{"write_file"}
	path := filepath.Join(t.TempDir(), "saved.json")
	if _, err := s.command(context.Background(), "t", "/save "+path); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatal(err)
	}
	if _, err := s.command(context.Background(), "t", "/load"); err == nil {
		t.Fatal("/load without a path accepted")
	}
}
