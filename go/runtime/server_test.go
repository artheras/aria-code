package main

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/genai"
)

type appClient struct {
	in     *io.PipeWriter
	out    *io.PipeReader
	events chan map[string]any
	done   chan error
}

func testApplication(t *testing.T, p modelProvider) (*appClient, string, string) {
	t.Helper()
	root, project := t.TempDir(), t.TempDir()
	if err := atomicJSON(filepath.Join(root, "config.json"), config{"local_provider": "google", "ui_lang": "zh"}); err != nil {
		t.Fatal(err)
	}
	inReader, inWriter := io.Pipe()
	outReader, outWriter := io.Pipe()
	s, err := newServer(appOptions{root: root, workspace: project}, outWriter)
	if err != nil {
		t.Fatal(err)
	}
	s.provider = p
	c := &appClient{in: inWriter, out: outReader, events: make(chan map[string]any, 128), done: make(chan error, 1)}
	go func() {
		scanner := bufio.NewScanner(outReader)
		scanner.Buffer(make([]byte, 4096), maxMessage)
		for scanner.Scan() {
			var event map[string]any
			if json.Unmarshal(scanner.Bytes(), &event) == nil {
				c.events <- event
			}
		}
		close(c.events)
	}()
	go func() { err := s.run(context.Background(), inReader); c.done <- err; outWriter.Close() }()
	t.Cleanup(func() { inWriter.Close(); outReader.Close(); inReader.Close(); outWriter.Close() })
	if _, err := io.WriteString(inWriter, "\n"); err != nil {
		t.Fatal(err)
	}
	return c, root, project
}
func (c *appClient) send(t *testing.T, value map[string]any) {
	t.Helper()
	value["protocol"] = 1
	if err := json.NewEncoder(c.in).Encode(value); err != nil {
		t.Fatal(err)
	}
}
func (c *appClient) until(t *testing.T, kind string) map[string]any {
	t.Helper()
	deadline := time.After(15 * time.Second)
	for {
		select {
		case event, ok := <-c.events:
			if !ok {
				t.Fatal("application disconnected", kind)
			}
			if event["type"] == kind {
				return event
			}
			if event["type"] == "protocol.error" {
				t.Fatal(event)
			}
		case <-deadline:
			t.Fatal("timed out", kind)
		}
	}
}
func (c *appClient) close(t *testing.T) {
	t.Helper()
	c.send(t, map[string]any{"type": "shutdown"})
	c.until(t, "session.closed")
	select {
	case err := <-c.done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(4 * time.Second):
		t.Fatal("application did not stop")
	}
}
func simpleProvider() modelProvider {
	return providerFunc(func(ctx context.Context, contents []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
		last := contents[len(contents)-1]
		if len(last.Parts) > 0 && last.Parts[0].FunctionResponse != nil {
			emit("Operation completed.")
			return genai.NewContentFromText("Operation completed.", genai.RoleModel), nil
		}
		prompt := last.Parts[0].Text
		if prompt == "slow" {
			<-ctx.Done()
			return nil, ctx.Err()
		}
		if strings.HasPrefix(prompt, "write ") {
			return &genai.Content{Role: "model", Parts: []*genai.Part{call("write_file", map[string]any{"path": strings.TrimPrefix(prompt, "write "), "content": "Actual native file body."})}}, nil
		}
		emit("Reply: " + prompt)
		return genai.NewContentFromText("Reply: "+prompt, genai.RoleModel), nil
	})
}
func TestNativeApplicationApprovalCancelAndMultiTurnResume(t *testing.T) {
	c, root, project := testApplication(t, simpleProvider())
	ready := c.until(t, "session.ready")
	id := ready["session_id"].(string)
	if ready["robot"].(map[string]any)["design"] != "cream-block-v2" {
		t.Fatal("native mascot missing")
	}
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "hello", "text": "你好"})
	if event := c.until(t, "turn.completed"); event["status"] != "ok" {
		t.Fatal(event)
	}
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "write", "text": "write note.txt"})
	menu := c.until(t, "approval.requested")
	if !strings.Contains(menu["context"].(string), "note.txt") {
		t.Fatal(menu)
	}
	c.send(t, map[string]any{"type": "dialog.respond", "turn_id": "write", "request_id": menu["request_id"], "choice": 0})
	if event := c.until(t, "turn.completed"); event["status"] != "ok" {
		t.Fatal(event)
	}
	body, err := os.ReadFile(filepath.Join(project, "note.txt"))
	if err != nil || string(body) != "Actual native file body." {
		t.Fatal(string(body), err)
	}
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "slow", "text": "slow"})
	c.until(t, "turn.started")
	c.send(t, map[string]any{"type": "turn.cancel", "turn_id": "slow"})
	if event := c.until(t, "turn.completed"); event["status"] != "cancelled" {
		t.Fatal(event)
	}
	c.send(t, map[string]any{"type": "session.new"})
	if event := c.until(t, "session.ready"); event["session_id"] == id {
		t.Fatal("session was not switched")
	}
	c.send(t, map[string]any{"type": "session.resume", "session_id": id})
	if event := c.until(t, "session.ready"); len(event["messages"].([]any)) < 4 {
		t.Fatal("history was lost", event)
	}
	c.close(t)
	saved, err := loadSession(root, id)
	if err != nil || len(saved.Messages) < 4 {
		t.Fatal(saved, err)
	}
}
func TestDeclineFollowupDoesNotWriteTheFile(t *testing.T) {
	c, _, project := testApplication(t, simpleProvider())
	c.until(t, "session.ready")
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "decline", "text": "write declined.txt"})
	menu := c.until(t, "approval.requested")
	c.send(t, map[string]any{"type": "dialog.respond", "turn_id": "decline", "request_id": menu["request_id"], "choice": 2})
	question := c.until(t, "input.requested")
	c.send(t, map[string]any{"type": "dialog.respond", "turn_id": "decline", "request_id": question["request_id"], "text": "Use a different filename."})
	c.until(t, "turn.completed")
	if _, err := os.Stat(filepath.Join(project, "declined.txt")); !os.IsNotExist(err) {
		t.Fatal("declined file was created")
	}
	c.close(t)
}
func TestCancelledSignedCallsAreSettledBeforeTheNextTurn(t *testing.T) {
	contents := []*genai.Content{{Role: "model", Parts: []*genai.Part{call("write_file", map[string]any{"path": "unused.txt"}), call("read_file", map[string]any{"path": "unused.txt"})}}}
	settled := settlePending(contents)
	if len(settled) != 2 || len(settled[1].Parts) != 2 {
		t.Fatal("unanswered calls remain")
	}
	if again := settlePending(settled); len(again) != 2 {
		t.Fatal("responses were duplicated")
	}
	if settled[0].Parts[0].ThoughtSignature == nil {
		t.Fatal("signature was removed")
	}
}
