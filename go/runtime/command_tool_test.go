package main

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"google.golang.org/genai"
)

func testWriteCommand() string {
	if runtime.GOOS == "windows" {
		return "echo native-proof> note.txt & type note.txt"
	}
	return "printf 'native-proof' > note.txt; cat note.txt"
}
func TestNativeCommandRunsInApprovedProjectAndReturnsActualExitAndOutput(t *testing.T) {
	project := t.TempDir()
	scope, err := newScope(project, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	spec, err := commandPlan(scope, config{}, map[string]any{"command": testWriteCommand(), "timeout": 10})
	if err != nil {
		t.Fatal(err)
	}
	result := commandTool(context.Background(), spec)
	if !result.Success || result.Data["exit_code"] != 0 || !strings.Contains(result.Data["stdout"].(string), "native-proof") {
		t.Fatal(result)
	}
	body, err := os.ReadFile(filepath.Join(project, "note.txt"))
	if err != nil || !strings.Contains(string(body), "native-proof") {
		t.Fatal(string(body), err)
	}
	spec.command = "exit 7"
	result = commandTool(context.Background(), spec)
	if !result.Success || result.Data["exit_code"] != 7 {
		t.Fatal("a nonzero program exit must remain inspectable", result)
	}
}
func TestNativeCommandRejectsSpoofedPolicyAndUnapprovedWorkingDirectory(t *testing.T) {
	s, err := newScope(t.TempDir(), nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	for _, params := range []map[string]any{{"command": "echo ok", "_permission_mode": "full-access"}, {"command": "echo ok", "user_approved": true}, {"command": "echo ok", "cwd": t.TempDir()}, {"command": "echo ok", "timeout": 0}} {
		if _, err := commandPlan(s, config{}, params); err == nil {
			t.Fatal("provider bypassed command policy", params)
		}
	}
	for _, cfg := range []config{{"permission_mode": "read-only"}, {"network_enabled": false}} {
		if _, err := commandPlan(s, cfg, map[string]any{"command": "echo ok"}); err == nil {
			t.Fatal("unsupported sandbox mode was ignored")
		}
	}
}
func TestNativeCommandOutputRemainsBoundedAndDrainsTheStream(t *testing.T) {
	b := &outputTail{limit: 16}
	for i := 0; i < 10000; i++ {
		n, err := b.Write([]byte("0123456789"))
		if n != 10 || err != nil {
			t.Fatal(n, err)
		}
	}
	text, cut := b.text()
	if len(text) != 16 || !cut || !strings.HasSuffix(text, "0123456789") {
		t.Fatal(text, cut)
	}
}
func TestDirectShellShortcutNeedsApprovalAndDoesNotCallTheModel(t *testing.T) {
	c, _, project := testApplication(t, providerFunc(func(context.Context, []*genai.Content, string, []*genai.Tool, func(string)) (*genai.Content, error) {
		t.Error("a direct shell shortcut called inference")
		return nil, context.Canceled
	}))
	c.until(t, "session.ready")
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "shell", "text": "!" + testWriteCommand()})
	menu := c.until(t, "approval.requested")
	if !strings.Contains(menu["context"].(string), "native-proof") {
		t.Fatal("approval lost the actual command")
	}
	// The assessment is data for the frontend's card, not text in the context.
	risk, ok := menu["risk"].(map[string]any)
	if !ok || risk["level"] == nil || risk["score"] == nil || risk["reversible"] == nil {
		t.Fatal("approval carries no structured risk", menu)
	}
	if strings.Contains(menu["context"].(string), "/100") {
		t.Fatal("the risk card is also in the context text, so it would show twice")
	}
	if _, err := os.Stat(filepath.Join(project, "note.txt")); !os.IsNotExist(err) {
		t.Fatal("shell executed before approval")
	}
	c.send(t, map[string]any{"type": "dialog.respond", "turn_id": "shell", "request_id": menu["request_id"], "choice": 0})
	if done := c.until(t, "turn.completed"); done["status"] != "ok" {
		t.Fatal(done)
	}
	body, err := os.ReadFile(filepath.Join(project, "note.txt"))
	if err != nil || !strings.Contains(string(body), "native-proof") {
		t.Fatal(string(body), err)
	}
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "declined-shell", "text": "!echo refused"})
	menu = c.until(t, "approval.requested")
	c.send(t, map[string]any{"type": "dialog.respond", "turn_id": "declined-shell", "request_id": menu["request_id"], "choice": -1})
	c.until(t, "turn.completed")
	c.close(t)
}
func TestCommandSessionGrantDoesNotApproveADifferentCommand(t *testing.T) {
	c, _, _ := testApplication(t, simpleProvider())
	c.until(t, "session.ready")
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "first", "text": "!echo first"})
	menu := c.until(t, "approval.requested")
	c.send(t, map[string]any{"type": "dialog.respond", "turn_id": "first", "request_id": menu["request_id"], "choice": 1})
	c.until(t, "turn.completed")
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "different", "text": "!echo second"})
	menu = c.until(t, "approval.requested")
	c.send(t, map[string]any{"type": "dialog.respond", "turn_id": "different", "request_id": menu["request_id"], "choice": -1})
	c.until(t, "turn.completed")
	c.close(t)
}
func TestCancellationStopsACommandBeforeItsLaterWrite(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix process-group cancellation; Windows uses the host job and taskkill")
	}
	project := t.TempDir()
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	r := commandTool(ctx, commandSpec{command: "(sleep 0.4; printf leaked > later.txt) & wait", cwd: project, timeout: 5 * time.Second})
	if r.Success {
		t.Fatal("cancelled command reported success")
	}
	time.Sleep(500 * time.Millisecond)
	if _, err := os.Stat(filepath.Join(project, "later.txt")); !os.IsNotExist(err) {
		t.Fatal("a child kept running after cancellation")
	}
}
