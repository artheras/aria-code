package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/genai"
)

func TestFinanceCommandsNeedTheService(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	if out, _ := s.command(context.Background(), "t", "/finance"); !strings.Contains(out, "not connected") {
		t.Fatal(out)
	}
	if _, err := s.command(context.Background(), "t", "/broker"); err == nil || !strings.Contains(err.Error(), "financial-service") {
		t.Fatal(err)
	}
	s.provider = providerFunc(func(context.Context, []*genai.Content, string, []*genai.Tool, func(string)) (*genai.Content, error) {
		t.Fatal("a finance command reached the model without the service")
		return nil, nil
	})
	s.done = make(chan struct{})
	s.runTurn(context.Background(), "t", "/quote AAPL")
	if len(s.snap.Messages) != 0 {
		t.Fatal(s.snap.Messages)
	}
	if _, err := s.command(context.Background(), "t", "/finance setup relative/path"); err == nil {
		t.Fatal("relative path accepted")
	}
}

// fakeFinance registers the fake MCP server (mcp_test.go) as the finance
// service: echo is read-only, touch is not.
func fakeFinance(t *testing.T, s *server) {
	t.Helper()
	config := map[string]any{"servers": []any{
		map[string]any{"name": "other", "command": "nothing", "enabled": false},
		map[string]any{"name": financeServer, "command": os.Args[0], "args": []string{"-test.run=^TestHelperMCPServer$"}, "env": map[string]string{"ARIA_FAKE_MCP": "1"}},
	}}
	if err := atomicJSON(filepath.Join(s.opts.root, "mcp_servers.json"), config); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.mcp.closeAll)
}

func TestFinanceCommandIsAReadOnlyTurnOnTheServiceTools(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{skipPermissions: true})
	fakeFinance(t, s)
	if out, _ := s.command(context.Background(), "t", "/finance"); !strings.Contains(out, "connected") || !strings.Contains(out, "2 tools") {
		t.Fatal(out)
	}
	var system, prompt string
	var results []string
	s.provider = providerFunc(func(_ context.Context, h []*genai.Content, sys string, _ []*genai.Tool, emit func(string)) (*genai.Content, error) {
		last := h[len(h)-1]
		if len(last.Parts) > 0 && last.Parts[0].FunctionResponse != nil {
			for _, part := range last.Parts {
				body, _ := json.Marshal(part.FunctionResponse.Response)
				results = append(results, string(body))
			}
			emit("answer")
			return genai.NewContentFromText("answer", genai.RoleModel), nil
		}
		system, prompt = sys, last.Parts[len(last.Parts)-1].Text
		return &genai.Content{Role: genai.RoleModel, Parts: []*genai.Part{
			call(mcpToolName(financeServer, "echo"), map[string]any{"text": "AAPL"}),
			call(mcpToolName(financeServer, "touch"), map[string]any{}),
			call("write_file", map[string]any{"path": "notes.txt", "content": "x"}),
		}}, nil
	})
	s.done = make(chan struct{})
	s.runTurn(context.Background(), "t", "/quote AAPL")
	if !strings.Contains(prompt, "/quote AAPL") || !strings.Contains(prompt, "mcp__arthera-finance__echo") || !strings.Contains(prompt, "never invent data") {
		t.Fatal(prompt)
	}
	if !strings.Contains(system, "read-only") {
		t.Fatal("finance turn is not read-only", system)
	}
	joined := strings.Join(results, "\n")
	if !strings.Contains(joined, "echo: AAPL") || strings.Contains(joined, "touched") || statErr(filepath.Join(s.scope.workspace, "notes.txt")) == nil {
		t.Fatal("only the read-only service tool may run in a finance turn", joined)
	}
	if s.scope.readOnly {
		t.Fatal("finance turn left the session read-only")
	}
}

func TestFinanceSetupKeepsOtherServers(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	atomicJSON(filepath.Join(s.opts.root, "mcp_servers.json"), map[string]any{"servers": []any{map[string]any{"name": "fs", "command": "x", "enabled": false, "custom": 1}}})
	python := filepath.Join(t.TempDir(), "python3")
	os.WriteFile(python, []byte(""), 0o755)
	s.command(context.Background(), "t", "/finance setup "+python)
	configs, err := loadMCPConfig(s.opts.root)
	if err != nil || len(configs) != 2 || configs[0].Name != "fs" || configs[1].Name != financeServer || strings.Join(configs[1].Args, " ") != "-I -m arthera_finance.server" {
		t.Fatal(configs, err)
	}
	var raw map[string][]map[string]any
	readJSON(filepath.Join(s.opts.root, "mcp_servers.json"), &raw)
	if raw["servers"][0]["custom"] != 1.0 {
		t.Fatal("an unknown field of another server was lost", raw)
	}
	s.mcp.closeAll()
}

// TestRealFinancialService runs against an installed financial-service:
// ARIA_FINANCIAL_SERVICE=/path/to/.venv/bin/arthera-financial-service.
func TestRealFinancialService(t *testing.T) {
	path := os.Getenv("ARIA_FINANCIAL_SERVICE")
	if path == "" {
		t.Skip("set ARIA_FINANCIAL_SERVICE to an installed arthera-financial-service")
	}
	s := approvalServer(t, config{}, appOptions{})
	t.Cleanup(s.mcp.closeAll)
	if out, err := s.command(context.Background(), "t", "/finance setup "+path); err != nil || !strings.Contains(out, "10 tools") {
		t.Fatal(out, err)
	}
	if out, err := s.command(context.Background(), "t", "/broker connections"); err != nil || !strings.Contains(out, "success") {
		t.Fatal(out, err)
	}
	result := s.mcp.call(context.Background(), mcpToolName(financeServer, "financial_risk_metrics"), map[string]any{"equity": []any{100.0, 110.0, 99.0, 120.0}})
	content, _ := result.Data["content"].(string)
	if !result.Success || !strings.Contains(content, "max_drawdown") {
		t.Fatal(result)
	}
	t.Log(content)
}
