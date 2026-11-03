package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"google.golang.org/genai"
)

func TestChatCompletionsUsageIsRequestedAndRecorded(t *testing.T) {
	var asked map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		asked = nil
		json.Unmarshal(body, &asked)
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, `data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}`+"\n\n")
		fmt.Fprint(w, `data: {"choices":[],"usage":{"prompt_tokens":120,"completion_tokens":7}}`+"\n\n")
		fmt.Fprint(w, "data: [DONE]\n\n")
	}))
	defer server.Close()
	p := &openAIProvider{cfg: config{}, model: "m", client: server.Client(), endpoint: server.URL}
	meter := &usageMeter{}
	if _, err := p.Generate(withUsage(context.Background(), meter), nil, "s", nil, func(string) {}); err != nil {
		t.Fatal(err)
	}
	if options, _ := asked["stream_options"].(map[string]any); options["include_usage"] != true {
		t.Fatal("usage not requested", asked)
	}
	if in, out, _, calls := meter.totals(); in != 120 || out != 7 || calls != 1 {
		t.Fatal(in, out, calls)
	}
	p.cfg = config{"stream_usage": false}
	if _, err := p.Generate(context.Background(), nil, "s", nil, func(string) {}); err != nil {
		t.Fatal(err)
	}
	if _, ok := asked["stream_options"]; ok {
		t.Fatal("stream_usage=false still sent stream_options")
	}
}

func TestGeminiUsageAndThinkingLevel(t *testing.T) {
	var asked string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		asked = string(body)
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"a\"}]}}],\"usageMetadata\":{\"promptTokenCount\":50,\"candidatesTokenCount\":1}}\n\n")
		fmt.Fprint(w, "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"b\"}]},\"finishReason\":\"STOP\"}],\"usageMetadata\":{\"promptTokenCount\":50,\"candidatesTokenCount\":9,\"thoughtsTokenCount\":30}}\n\n")
	}))
	defer server.Close()
	client, err := genai.NewClient(context.Background(), &genai.ClientConfig{Backend: genai.BackendGeminiAPI, APIKey: "fixture", HTTPOptions: genai.HTTPOptions{BaseURL: server.URL}})
	if err != nil {
		t.Fatal(err)
	}
	p := &googleProvider{cfg: config{"thinking": "high"}, model: "gemini-test", client: client}
	meter := &usageMeter{}
	if _, err := p.Generate(withUsage(context.Background(), meter), []*genai.Content{genai.NewContentFromText("q", genai.RoleUser)}, "s", nil, func(string) {}); err != nil {
		t.Fatal(err)
	}
	if in, out, thinking, calls := meter.totals(); in != 50 || out != 9 || thinking != 30 || calls != 1 {
		t.Fatal("the last chunk's totals should count once", in, out, thinking, calls)
	}
	if !strings.Contains(asked, `"thinkingLevel":"HIGH"`) {
		t.Fatal("thinking level not sent", asked)
	}
	p.cfg = config{}
	p.Generate(context.Background(), []*genai.Content{genai.NewContentFromText("q", genai.RoleUser)}, "s", nil, func(string) {})
	if strings.Contains(asked, "thinkingConfig") {
		t.Fatal("auto sent a thinking config", asked)
	}
}

func TestCostAccumulatesAndSurvivesResume(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	if out, _ := s.command(context.Background(), "t", "/cost"); !strings.Contains(out, "No model calls") {
		t.Fatal(out)
	}
	s.provider = providerFunc(func(ctx context.Context, _ []*genai.Content, _ string, _ []*genai.Tool, emit func(string)) (*genai.Content, error) {
		recordUsage(ctx, 1000, 200, 0)
		emit("ok")
		return genai.NewContentFromText("ok", genai.RoleModel), nil
	})
	for i := 0; i < 2; i++ {
		s.done = make(chan struct{})
		s.runTurn(context.Background(), "t", fmt.Sprintf("question %d please", i))
	}
	out, _ := s.command(context.Background(), "t", "/cost")
	if !strings.Contains(out, "Model calls    2") || !strings.Contains(out, "2,000") || !strings.Contains(out, "token_price") {
		t.Fatal(out)
	}
	if _, err := s.command(context.Background(), "t", "/config set token_price=1, 10"); err != nil {
		t.Fatal(err)
	}
	if out, _ := s.command(context.Background(), "t", "/cost"); !strings.Contains(out, "0.0060") {
		t.Fatal("2,000 in at 1 + 400 out at 10 per million is 0.006", out)
	}
	if _, err := s.command(context.Background(), "t", "/config set token_price=cheap"); err == nil {
		t.Fatal("bad price accepted")
	}
	saved, err := loadSession(s.opts.root, s.snap.ID)
	if err != nil {
		t.Fatal(err)
	}
	if in, _, _, calls := sessionUsage(saved.Metadata).totals(); in != 2000 || calls != 2 {
		t.Fatal("usage not saved with the session", saved.Metadata)
	}
}

func TestThinkingCommand(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	if out, _ := s.command(context.Background(), "t", "/thinking"); !strings.Contains(out, "thinking=auto") {
		t.Fatal(out)
	}
	if out, err := s.command(context.Background(), "t", "/thinking off"); err != nil || !strings.HasPrefix(out, "thinking=minimal") {
		t.Fatal(out, err)
	}
	if disk, _ := loadConfig(s.opts.root); disk["thinking"] != "minimal" {
		t.Fatal(disk)
	}
	if _, err := s.command(context.Background(), "t", "/thinking lots"); err == nil {
		t.Fatal("bad level accepted")
	}
}
