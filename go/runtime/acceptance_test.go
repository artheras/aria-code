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

// contentCheck is a check command that passes once app.py contains "fixed".
func contentCheck() string {
	if runtime.GOOS == "windows" {
		return `findstr /c:fixed app.py`
	}
	return `grep -q fixed app.py`
}

func TestRedChecksGoBackToTheModelUntilGreen(t *testing.T) {
	t.Setenv("ARIA_HOME", t.TempDir())
	// The check passes before the turn, so its failure after the first
	// write is the turn's and goes back to the model (the baseline says so).
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "app.py"), []byte("fixed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	scope, err := newScope(root, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	var directive string
	writes := 0
	provider := providerFunc(func(ctx context.Context, contents []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
		last := contents[len(contents)-1].Parts[0]
		if strings.HasPrefix(last.Text, "## 验收未通过") {
			directive = last.Text
		}
		if last.FunctionResponse == nil {
			writes++
			content := "still broken\n"
			if writes > 1 {
				content = "fixed\n"
			}
			return &genai.Content{Role: "model", Parts: []*genai.Part{call("write_file", map[string]any{"path": "app.py", "content": content})}}, nil
		}
		text := "Done."
		if writes > 1 {
			text = "Fixed the check."
		}
		emit(text)
		return genai.NewContentFromText(text, genai.RoleModel), nil
	})
	hooks := quietHooks()
	hooks.evidence = newTurnEvidence(root)
	var statuses []string
	hooks.emit = func(kind string, fields map[string]any) {
		if kind == "turn.status" {
			statuses = append(statuses, fields["state"].(string))
		}
	}
	cfg := config{"acceptance_commands": []any{contentCheck()}}
	_, answer, err := nativeTurn(context.Background(), provider, scope, cfg, nil, "Fix app.py", hooks)
	if err != nil {
		t.Fatal(err)
	}
	if answer != "Fixed the check." || writes != 2 {
		t.Fatalf("the red check must send the model back: %q after %d writes", answer, writes)
	}
	if !strings.Contains(directive, "### `"+contentCheck()+"` → exit 1") || !strings.Contains(directive, "剩余自动重试次数：1。") {
		t.Fatal(directive)
	}
	if strings.Join(statuses, ",") != "acceptance_started,acceptance_failed,acceptance_started,acceptance_passed" {
		t.Fatal(statuses)
	}
	report := hooks.evidence.report("completed")
	text := report.Render("", root)
	if report.Status != "done" || !strings.Contains(text, "✓ "+contentCheck()) || !strings.Contains(text, "Ready to commit") {
		t.Fatal(text)
	}
}

func TestTheGateIsBoundedAndEndsHonestly(t *testing.T) {
	t.Setenv("ARIA_HOME", t.TempDir())
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "app.py"), []byte("fixed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	scope, err := newScope(root, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	writes := 0
	provider := providerFunc(func(ctx context.Context, contents []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
		if contents[len(contents)-1].Parts[0].FunctionResponse == nil {
			writes++
			return &genai.Content{Role: "model", Parts: []*genai.Part{call("write_file", map[string]any{"path": "app.py", "content": "never\n"})}}, nil
		}
		emit("Done.")
		return genai.NewContentFromText("Done.", genai.RoleModel), nil
	})
	hooks := quietHooks()
	hooks.evidence = newTurnEvidence(root)
	cfg := config{"acceptance_commands": []any{contentCheck()}, "acceptance_max_attempts": 2.0}
	if _, _, err := nativeTurn(context.Background(), provider, scope, cfg, nil, "Fix app.py", hooks); err != nil {
		t.Fatal(err)
	}
	// Two checked attempts; the last red one still gets its repair message,
	// and the write after it is left unverified.
	if writes != 3 || hooks.evidence.accept.attempts != 2 {
		t.Fatal("two attempts, then the turn ends", writes, hooks.evidence.accept.attempts)
	}
	report := hooks.evidence.report("completed")
	if report.Status != "incomplete" || report.Next != "Fix the failing checks" {
		t.Fatal(report.Render("", root))
	}
}

func TestTheGateStaysOffWhereItCannotRun(t *testing.T) {
	for _, cfg := range []config{{"acceptance_gate": false}, {"permission_mode": "read-only"}} {
		if newAcceptanceGate(cfg, t.TempDir()) != nil {
			t.Fatal(cfg)
		}
	}
	gate := newAcceptanceGate(config{}, t.TempDir())
	if gate == nil || gate.maxAttempts != 2 || gate.shouldRun() {
		t.Fatal("on by default, armed only by a write")
	}
}

func TestStripJSONCommentsKeepsURLs(t *testing.T) {
	got := stripJSONComments("{\"a\": \"https://x.io/y\", // note\n/* block\n */ \"b\": 1}")
	if got != "{\"a\": \"https://x.io/y\", \n \"b\": 1}" {
		t.Fatalf("%q", got)
	}
}

// A successful command is not evidence of a captured content revision.
func TestAcceptanceDoesNotInventContentFreshness(t *testing.T) {
	report := acceptanceReport{Checks: []checkResult{{Passed: true}}}
	summary := report.summary()
	if summary["revision_status"] != "not_recorded" {
		t.Fatalf("uncaptured revision reported as current: %v", summary)
	}
	paths, ok := summary["revision_paths"].([]any)
	if !ok || len(paths) != 0 {
		t.Fatalf("expected an explicit empty revision list: %v", summary)
	}
}
