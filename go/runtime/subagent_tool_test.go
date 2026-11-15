package main

import (
	"context"
	"strings"
	"testing"

	"google.golang.org/genai"
)

type stubSubagentProvider struct {
	reply string
}

func (s *stubSubagentProvider) Generate(_ context.Context, _ []*genai.Content, _ string, _ []*genai.Tool, onDelta func(string)) (*genai.Content, error) {
	if onDelta != nil {
		onDelta(s.reply)
	}
	return genai.NewContentFromText(s.reply, genai.RoleModel), nil
}

func TestSpawnSubagentToolDepthGuardAndSuccess(t *testing.T) {
	p := &stubSubagentProvider{reply: "Found symbol Foo in bar.go:12"}
	sc := &scope{workspace: t.TempDir()}
	cfg := config{}

	res := spawnSubagentTool(context.Background(), p, nil, sc, cfg, map[string]any{
		"task": "Locate Foo symbol",
	})
	if !res.Success {
		t.Fatalf("expected spawnSubagentTool to succeed, got error: %s", res.Error)
	}
	if report, _ := res.Data["report"].(string); !strings.Contains(report, "bar.go:12") {
		t.Fatalf("expected report to contain bar.go:12, got %q", report)
	}

	// Recursion guard
	deepCtx := context.WithValue(context.Background(), subagentDepthCtxKey{}, maxSubagentDepth)
	blocked := spawnSubagentTool(deepCtx, p, nil, sc, cfg, map[string]any{
		"task": "Nested search",
	})
	if blocked.Success {
		t.Fatal("expected nested spawnSubagentTool to be blocked by recursion guard")
	}
}
