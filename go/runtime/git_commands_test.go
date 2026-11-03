package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
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
	run := gitRepo(t, s)
	if out, _ := s.command(context.Background(), "t", "/review"); out != "Nothing to review." {
		t.Fatal(out)
	}
	write := func(name, text string) {
		t.Helper()
		if err := os.WriteFile(filepath.Join(s.scope.workspace, name), []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("a.txt", "two\n")
	write("new.txt", "brand new\nsecond\n")
	write("AGENTS.md", "Never log tokens.\n")
	var system, prompt string
	var tools []*genai.Tool
	reply := `{"findings":[{"title":"[P1] wrong value","body":"a.txt now says two.","confidence_score":0.9,
	  "code_location":{"file_path":"a.txt","line_range":{"start":1,"end":1}}},
	 {"title":"old issue","body":"elsewhere","priority":3,"code_location":{"file_path":"a.txt","line_range":{"start":40,"end":41}}}],
	 "overall_correctness":"patch is incorrect","overall_explanation":"The value is wrong.","overall_confidence_score":0.8}`
	s.provider = providerFunc(func(_ context.Context, h []*genai.Content, sys string, ts []*genai.Tool, _ func(string)) (*genai.Content, error) {
		system, prompt, tools = sys, h[0].Parts[0].Text, ts
		return genai.NewContentFromText(reply, genai.RoleModel), nil
	})
	before := len(s.snap.Messages)
	out, err := s.command(context.Background(), "t", "/review")
	if err != nil || !strings.Contains(out, "Needs changes") || !strings.Contains(out, "1. [P1] wrong value  a.txt:1") ||
		!strings.Contains(out, "Outside this change") {
		t.Fatal(out, err)
	}
	if !strings.HasPrefix(system, reviewRubric) || !strings.Contains(system, "AGENTS.md:\nNever log tokens.") || tools != nil {
		t.Fatal("reviewer not isolated or rules missing", system, tools)
	}
	// Untracked files are reviewed by content, not listed by name.
	if !strings.Contains(prompt, "+two") || !strings.Contains(prompt, "+brand new") || !strings.Contains(prompt, "uncommitted changes") {
		t.Fatal(prompt)
	}
	if len(s.snap.Messages) != before+2 || !strings.Contains(s.snap.Messages[before+1]["content"].(string), "1. [P1] wrong value") {
		t.Fatal("review not kept for follow-ups", s.snap.Messages)
	}
	run("add", "-A")
	run("commit", "-q", "-m", "Change a")
	if _, err := s.command(context.Background(), "t", "/review --commit HEAD"); err != nil || !strings.Contains(prompt, `Commit title: "Change a"`) {
		t.Fatal(err, prompt)
	}
	if _, err := s.command(context.Background(), "t", "/review new.txt"); err != nil || !strings.Contains(prompt, "file new.txt") || !strings.Contains(prompt, " second") {
		t.Fatal(err, prompt)
	}
	for _, bad := range []string{"/review --base --output=x", "/review --commit", "/review --staged extra", "/review --nope"} {
		if _, err := s.command(context.Background(), "t", bad); err == nil {
			t.Fatal("accepted", bad)
		}
	}
}

func TestReviewFitsWholeFilesAndNamesTheRest(t *testing.T) {
	section := func(name string, n int) string {
		return "diff --git a/" + name + " b/" + name + "\n@@ -1,0 +1," + fmt.Sprint(n) + " @@\n" + strings.Repeat("+x\n", n)
	}
	in := fitReview(reviewTarget{kind: "uncommitted"}, "/r", section("a.go", 10)+section("b.go", 50)+section("c.go", 2), 130)
	if strings.Join(in.files, ",") != "a.go,c.go" || strings.Join(in.omitted, ",") != "b.go" || in.added != 12 {
		t.Fatal(in.files, in.omitted, in.added)
	}
	_, user := buildCodeReview(in, "en", "")
	if !strings.Contains(user, "NOT shown and must not be reviewed or guessed about: b.go") {
		t.Fatal(user)
	}
	if got := in.hunks["a.go"]; len(got) != 1 || got[0] != (reviewRange{1, 10}) {
		t.Fatal(got)
	}
}

func TestReviewParsingDegradesInSteps(t *testing.T) {
	r := parseCodeReview("Sure!\n```json\n{\"findings\":[],\"overall_correctness\":\"patch is correct\"}\n```", nil)
	if !r.structured || r.correct == nil || !*r.correct {
		t.Fatal(r)
	}
	r = parseCodeReview("I think it is fine.", nil)
	if r.structured || r.explanation != "I think it is fine." {
		t.Fatal(r)
	}
	if !strings.Contains(renderCodeReview(r, "zh"), "审查模型未按约定格式回答") {
		t.Fatal(renderCodeReview(r, "zh"))
	}
}

func TestHeadlessCodeReviewGatesOnPriority(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	var reply string
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		chunk, _ := json.Marshal(map[string]any{"choices": []any{map[string]any{"delta": map[string]any{"content": reply}, "finish_reason": "stop"}}})
		fmt.Fprintf(w, "data: %s\n\ndata: [DONE]\n\n", chunk)
	}))
	defer model.Close()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("ARIA_HOME", home)
	t.Setenv("FAKE_REVIEW_KEY", "k")
	os.MkdirAll(filepath.Join(home, ".aria"), 0o755)
	os.WriteFile(filepath.Join(home, ".aria", "providers.yaml"), []byte("model_providers:\n  fake:\n    base_url: "+model.URL+"/v1\n    env_key: FAKE_REVIEW_KEY\n"), 0o644)
	atomicJSON(filepath.Join(home, "config.json"), config{"local_provider": "fake", "model": "m"})
	s := approvalServer(t, config{}, appOptions{})
	gitRepo(t, s)
	os.WriteFile(filepath.Join(s.scope.workspace, "a.txt"), []byte("two\n"), 0o644)
	t.Chdir(s.scope.workspace)
	finding := `{"findings":[{"title":"[P2] meh","body":"b","code_location":{"file_path":"a.txt","line_range":{"start":1,"end":1}}}],"overall_correctness":"patch is incorrect"}`
	for _, tc := range []struct {
		reply string
		args  []string
		code  int
	}{
		{finding, []string{"--fail-on", "P1"}, 0},
		{finding, []string{"--fail-on", "P2", "--json"}, 1},
		{"not json", []string{"--fail-on", "P1"}, 2},
		{"not json", nil, 0},
		{finding, []string{"--fail-on", "P9"}, 2},
	} {
		reply = tc.reply
		var out, errs strings.Builder
		if code := codeReviewCommand(context.Background(), tc.args, &out, &errs); code != tc.code {
			t.Fatal(tc.args, code, out.String(), errs.String())
		}
		if len(tc.args) > 2 && tc.args[2] == "--json" && !strings.Contains(out.String(), `"priority_label": "P2"`) {
			t.Fatal(out.String())
		}
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
