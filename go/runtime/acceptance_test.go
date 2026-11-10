package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

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

func TestOnlyAMissingToolIsUnavailable(t *testing.T) {
	cases := []struct {
		code          int
		output, error string
		want          string
	}{
		{1, "", "/opt/homebrew/opt/python@3.14/bin/python3.14: No module named pytest\n", "pytest is not installed for this python"},
		{1, "/opt/homebrew/opt/python@3.14/bin/python3.14: No module named pytest\n", "", "pytest is not installed for this python"},
		{127, "", "zsh: command not found: npm", "npm is not installed"},
		{1, "bash: npx: command not found", "", "npx is not installed"},
		{127, "", "", "the command is not installed"},
		{1, "E   ModuleNotFoundError: No module named 'requests'\nFAILED test_x.py::test_a", "", ""},
		{1, "3 failed, 2 passed in 0.1s", "", ""},
		{0, "", "", ""},
	}
	for _, c := range cases {
		if got := unavailableReason(c.code, c.output, c.error); got != c.want {
			t.Errorf("exit %d %q / %q: got %q, want %q", c.code, c.output, c.error, got, c.want)
		}
	}
}

func TestAMissingToolDoesNotFailTheGateOrTheDelivery(t *testing.T) {
	answers := [][3]any{{0, "ok", ""}, {1, "", "python3: No module named pytest"}}
	index := 0
	gate := &acceptanceGate{root: t.TempDir(), maxAttempts: 2, commands: []string{"cmd0", "cmd1"}}
	gate.record("a.py")
	report := gate.run(context.Background(), func(context.Context, string) map[string]any {
		answer := answers[index]
		index++
		return map[string]any{"success": true, "data": map[string]any{
			"exit_code": answer[0], "stdout": answer[1], "stderr": answer[2],
		}}
	})
	if report == nil || !report.passed() || len(report.failures()) != 0 || len(report.notRun()) != 1 || index != 2 {
		t.Fatalf("missing tool stopped the gate: passed=%v failures=%d ran=%d", report.passed(), len(report.failures()), index)
	}
	if !strings.Contains(report.headline(), "验收部分通过") || !strings.Contains(report.headline(), "pytest is not installed") {
		t.Fatal(report.headline())
	}
	summary := gate.summary()
	if _, isBool := summary["verified"].(bool); isBool {
		t.Fatal("part of the plan did not run, so it is not verified")
	}
	checks := summary["reports"].([]any)[0].(map[string]any)["checks"].([]any)
	if checks[1].(map[string]any)["not_run"] != "pytest is not installed for this python" {
		t.Fatal(checks[1])
	}

	ledger := newDeliveryLedger(gate.root)
	ledger.changed["a.py"] = &changedFile{Path: "a.py", Added: 1}
	ledger.order = []string{"a.py"}
	delivery := ledger.Report(summary, nil, nil, "completed")
	if delivery.Status != "done" || delivery.Verified != nil || delivery.Next != "Run the tests yourself — pytest is not installed for this python" {
		t.Fatalf("%s / %v / %s", delivery.Status, delivery.Verified, delivery.Next)
	}
	if len(delivery.Checks) != 1 || delivery.Checks[0]["command"] != "cmd0" {
		t.Fatal(delivery.Checks)
	}
	text := delivery.Render("", gate.root)
	if !strings.Contains(text, "not run: cmd1 — pytest is not installed for this python") {
		t.Fatal(text)
	}
	if delivery.asDict()["not_run"].([]map[string]any)[0]["command"] != "cmd1" {
		t.Fatal(delivery.asDict())
	}
}

func TestAPassingCheckIsTiedToTheFileBytes(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "app.py")
	if err := os.WriteFile(path, []byte("original"), 0o644); err != nil {
		t.Fatal(err)
	}
	calls := 0
	gate := &acceptanceGate{root: root, maxAttempts: 2, commands: []string{"check"}}
	gate.record("app.py")
	report := gate.run(context.Background(), func(context.Context, string) map[string]any {
		calls++
		return map[string]any{"success": true, "data": map[string]any{"exit_code": 0}}
	})
	if report == nil || !report.passed() || report.RevisionStatus != "current" || calls != 1 {
		t.Fatalf("pass not bound to the bytes: %+v", report)
	}
	if gate.summary()["verified"] != true {
		t.Fatal(gate.summary())
	}
	if err := os.Chtimes(path, time.Unix(1, 0), time.Unix(1, 0)); err != nil {
		t.Fatal(err)
	}
	if gate.summary()["verified"] != true {
		t.Fatal("mtime is not the fingerprint")
	}
	if err := os.WriteFile(path, []byte("external edit"), 0o644); err != nil {
		t.Fatal(err)
	}
	view := gate.summary()
	if view["verified"] != nil || view["revision_freshness"] != "stale" || calls != 1 {
		t.Fatalf("external edit reran or stayed verified: %v calls=%d", view, calls)
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if gate.summary()["revision_freshness"] != "stale" {
		t.Fatal(gate.summary()["revision_freshness"])
	}
}

func TestAnAbsoluteRecordedPathStaysInsideTheWorkspace(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "nested", "app.py")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("original"), 0o644); err != nil {
		t.Fatal(err)
	}
	gate := &acceptanceGate{root: root, maxAttempts: 2, commands: []string{"check"}}
	gate.record(path)
	report := gate.run(context.Background(), func(context.Context, string) map[string]any {
		return map[string]any{"success": true, "data": map[string]any{"exit_code": 0}}
	})
	if report == nil || !report.passed() || report.RevisionStatus != "current" || len(report.RevisionPaths) != 1 || report.RevisionPaths[0].Path != "nested/app.py" {
		t.Fatalf("absolute path was not a workspace fingerprint: %+v", report)
	}
}

func TestAChangeDuringTheCheckIsNotAPass(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "app.py")
	if err := os.WriteFile(path, []byte("before"), 0o644); err != nil {
		t.Fatal(err)
	}
	gate := &acceptanceGate{root: root, maxAttempts: 2, commands: []string{"check"}}
	gate.record("app.py")
	report := gate.run(context.Background(), func(context.Context, string) map[string]any {
		if err := os.WriteFile(path, []byte("after"), 0o644); err != nil {
			t.Fatal(err)
		}
		return map[string]any{"success": true, "data": map[string]any{"exit_code": 0}}
	})
	if report.passed() || report.RevisionStatus != "changed_during_checks" || len(report.failures()) != 0 {
		t.Fatalf("%+v", report)
	}
	if gate.summary()["verified"] != nil || !strings.Contains(report.repairDirective(), "不要覆盖用户修改") {
		t.Fatal(gate.summary(), report.repairDirective())
	}
}

func TestASymlinkIsNotFingerprinted(t *testing.T) {
	root := t.TempDir()
	outside := filepath.Join(t.TempDir(), "private.txt")
	if err := os.WriteFile(outside, []byte("private bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(root, "app.py")); err != nil {
		t.Skip(err)
	}
	gate := &acceptanceGate{root: root, maxAttempts: 2, commands: []string{"check"}}
	gate.record("app.py")
	report := gate.run(context.Background(), func(context.Context, string) map[string]any {
		return map[string]any{"success": true, "data": map[string]any{"exit_code": 0}}
	})
	view := gate.summary()
	if report.RevisionStatus != "unavailable" || view["verified"] != nil || view["revision_freshness"] != "unavailable" {
		t.Fatal(report.RevisionStatus, view)
	}
	if strings.Contains(fmt.Sprint(view), "private bytes") {
		t.Fatal(view)
	}
}

func TestAHostInputChangeDropsVerification(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "app.py"), []byte("code"), 0o644); err != nil {
		t.Fatal(err)
	}
	settings := filepath.Join(root, "settings.json")
	if err := os.WriteFile(settings, []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	gate := &acceptanceGate{root: root, maxAttempts: 2, commands: []string{"check"}, revisionInputs: []string{"settings.json"}}
	gate.record("app.py")
	if report := gate.run(context.Background(), func(context.Context, string) map[string]any {
		return map[string]any{"success": true, "data": map[string]any{"exit_code": 0}}
	}); report == nil || gate.summary()["verified"] != true {
		t.Fatal(gate.summary())
	}
	if err := os.WriteFile(settings, []byte(`{"changed":true}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if gate.summary()["verified"] != nil {
		t.Fatal(gate.summary())
	}
}

func TestARealFailureStillStopsBeforeLaterChecks(t *testing.T) {
	index := 0
	gate := &acceptanceGate{root: t.TempDir(), maxAttempts: 2, commands: []string{"cmd0", "cmd1"}}
	gate.record("a.py")
	report := gate.run(context.Background(), func(context.Context, string) map[string]any {
		index++
		return map[string]any{"success": true, "data": map[string]any{
			"exit_code": 1, "stdout": "FAILED test_a.py::test_x", "stderr": "",
		}}
	})
	if report.passed() || len(report.failures()) != 1 || len(report.notRun()) != 0 || index != 1 {
		t.Fatalf("real failure did not stop the gate: %+v calls=%d", report, index)
	}
	if verified := gate.summary()["verified"]; verified != false {
		t.Fatal(verified)
	}
}
