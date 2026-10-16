package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestModelSwitchPersistsBothFieldsAndFailurePreservesSelection(t *testing.T) {
	writeProviders(t, map[string]any{})
	s := approvalServer(t, config{}, appOptions{})
	if _, err := s.command(context.Background(), "t", "/model ollama qwen-test:local"); err != nil {
		t.Fatal(err)
	}
	cfg, err := loadConfig(s.opts.root)
	if err != nil || cfg["local_provider"] != "ollama" || cfg["model"] != "qwen-test:local" {
		t.Fatal(cfg, err)
	}
	old := s.provider
	if _, err := s.command(context.Background(), "t", "/model undeclared-test model"); err == nil {
		t.Fatal("invalid provider accepted")
	}
	if s.provider != old {
		t.Fatal("failed switch changed provider")
	}
	if _, err := s.command(context.Background(), "t", "/model gcp gemini-test"); err != nil {
		t.Fatal(err)
	}
	cfg, err = loadConfig(s.opts.root)
	if err != nil || cfg["local_provider"] != "vertexai" || cfg["use_vertexai"] != true || cfg["model"] != "gemini-test" {
		t.Fatal(cfg, err)
	}
}

func TestOllamaListingAndHealthUseActualEndpoint(t *testing.T) {
	writeProviders(t, map[string]any{})
	count := 0
	host := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		count++
		if r.URL.Path != "/v1/models" || r.Method != "GET" {
			t.Error(r.URL, r.Method)
		}
		io.WriteString(w, `{"data":[{"id":"qwen-test:local"},{"id":"llama-test"}]}`)
	}))
	defer host.Close()
	s := approvalServer(t, config{"ollama_base_url": host.URL}, appOptions{})
	text, err := s.command(context.Background(), "t", "/models ollama")
	if err != nil || !strings.Contains(text, "qwen-test:local") || count != 1 {
		t.Fatal(text, err, count)
	}
	if _, err := s.command(context.Background(), "t", "/model ollama qwen-test:local"); err != nil {
		t.Fatal(err)
	}
	text, err = s.command(context.Background(), "t", "/health")
	if err != nil || !strings.Contains(text, "ollama reachable") || strings.Contains(text, "Google") {
		t.Fatal(text, err)
	}
	host.Close()
	if _, err := s.command(context.Background(), "t", "/models ollama"); err == nil {
		t.Fatal("offline service reported ready")
	}
}

func TestModelPickerUsesProtocolAndCanCancel(t *testing.T) {
	writeProviders(t, map[string]any{})
	host := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"data":[{"id":"qwen-fixture"}]}`)
	}))
	defer host.Close()
	t.Setenv("OLLAMA_HOST", host.URL)
	c, root, _ := testApplication(t, nil)
	c.until(t, "session.ready")
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "pick", "text": "/model"})
	provider := c.until(t, "input.requested")
	c.send(t, map[string]any{"type": "dialog.respond", "turn_id": "pick", "request_id": provider["request_id"], "choice": 1})
	models := c.until(t, "input.requested")
	c.send(t, map[string]any{"type": "dialog.respond", "turn_id": "pick", "request_id": models["request_id"], "choice": 0})
	if done := c.until(t, "turn.completed"); done["status"] != "ok" {
		t.Fatal(done)
	}
	c.until(t, "session.state")
	cfg, err := loadConfig(root)
	if err != nil || cfg["local_provider"] != "ollama" || cfg["model"] != "qwen-fixture" {
		t.Fatal(cfg, err)
	}
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "cancel-pick", "text": "/model"})
	provider = c.until(t, "input.requested")
	c.send(t, map[string]any{"type": "dialog.respond", "turn_id": "cancel-pick", "request_id": provider["request_id"], "choice": -1})
	c.until(t, "turn.completed")
	c.until(t, "session.state")
	cfg, _ = loadConfig(root)
	if cfg["model"] != "qwen-fixture" {
		t.Fatal("cancel changed model")
	}
	c.send(t, map[string]any{"type": "shutdown"})
	c.until(t, "session.closed")
}
