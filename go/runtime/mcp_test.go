package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/genai"
)

// TestHelperMCPServer is not a test: run as a subprocess with
// ARIA_FAKE_MCP=1, it is a small stdio MCP server with one read-only tool
// (echo) and one that changes things (touch).
func TestHelperMCPServer(t *testing.T) {
	if os.Getenv("ARIA_FAKE_MCP") != "1" {
		t.Skip("helper process")
	}
	in := bufio.NewScanner(os.Stdin)
	out := json.NewEncoder(os.Stdout)
	for in.Scan() {
		var m struct {
			ID     *int64         `json:"id"`
			Method string         `json:"method"`
			Params map[string]any `json:"params"`
		}
		if json.Unmarshal(in.Bytes(), &m) != nil || m.ID == nil {
			continue
		}
		reply := map[string]any{"jsonrpc": "2.0", "id": *m.ID}
		switch m.Method {
		case "initialize":
			reply["result"] = map[string]any{"protocolVersion": mcpProtocolVersion, "capabilities": map[string]any{"tools": map[string]any{}}, "serverInfo": map[string]any{"name": "fake"}}
		case "tools/list":
			reply["result"] = map[string]any{"tools": []any{
				map[string]any{"name": "echo", "description": "Echo text back", "annotations": map[string]any{"readOnlyHint": true},
					"inputSchema": map[string]any{"$schema": "x", "type": "object", "properties": map[string]any{"text": map[string]any{"type": "string"}}}},
				map[string]any{"name": "touch", "description": "Create a marker", "inputSchema": map[string]any{"type": "object"}},
			}}
		case "tools/call":
			args, _ := m.Params["arguments"].(map[string]any)
			if m.Params["name"] == "echo" {
				reply["result"] = map[string]any{"content": []any{map[string]any{"type": "text", "text": fmt.Sprint("echo: ", args["text"])}}}
			} else {
				reply["result"] = map[string]any{"content": []any{map[string]any{"type": "text", "text": "touched"}}, "isError": false}
			}
		default:
			reply["error"] = map[string]any{"code": -32601, "message": "no such method"}
		}
		out.Encode(reply)
	}
	os.Exit(0)
}

func fakeMCPServer(t *testing.T, s *server) {
	t.Helper()
	config := map[string]any{"servers": []any{
		map[string]any{"name": "fake", "command": os.Args[0], "args": []string{"-test.run=^TestHelperMCPServer$"}, "env": map[string]string{"ARIA_FAKE_MCP": "1"}},
		map[string]any{"name": "off", "command": "nothing", "enabled": false},
		map[string]any{"name": "missing", "command": filepath.Join(t.TempDir(), "no-such-binary")},
	}}
	if err := atomicJSON(filepath.Join(s.opts.root, "mcp_servers.json"), config); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.mcp.closeAll)
}

func TestMCPServersStartAndListTools(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	if out, _ := s.command(context.Background(), "t", "/mcp"); !strings.Contains(out, "No MCP servers") {
		t.Fatal(out)
	}
	s.mcp = newMCPManager(s.opts.root)
	fakeMCPServer(t, s)
	out, err := s.command(context.Background(), "t", "/mcp")
	if err != nil || !strings.Contains(out, "● fake") || !strings.Contains(out, "2 tools") || !strings.Contains(out, "○ off") || !strings.Contains(out, "✗ missing") {
		t.Fatal(out, err)
	}
	out, _ = s.command(context.Background(), "t", "/mcp tools")
	if !strings.Contains(out, "mcp__fake__echo") || !strings.Contains(out, "read") || !strings.Contains(out, "mcp__fake__touch") {
		t.Fatal(out)
	}
	if out, _ := s.command(context.Background(), "t", "/tools"); !strings.Contains(out, "mcp__fake__echo") {
		t.Fatal("/tools does not show MCP tools", out)
	}
	for _, d := range s.mcp.declarations() {
		if _, ok := d.ParametersJsonSchema.(map[string]any)["$schema"]; ok {
			t.Fatal("$schema passed to the provider")
		}
	}
	if out, _ := s.command(context.Background(), "t", "/mcp reload"); !strings.Contains(out, "● fake") {
		t.Fatal(out)
	}
}

func TestModelCallsMCPToolsUnderApproval(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	fakeMCPServer(t, s)
	var results []string
	calls := []string{"mcp__fake__echo", "mcp__fake__touch"}
	s.provider = providerFunc(func(_ context.Context, h []*genai.Content, _ string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
		last := h[len(h)-1]
		if len(last.Parts) > 0 && last.Parts[0].FunctionResponse != nil {
			for _, part := range last.Parts {
				body, _ := json.Marshal(part.FunctionResponse.Response)
				results = append(results, string(body))
			}
			emit("ok")
			return genai.NewContentFromText("ok", genai.RoleModel), nil
		}
		found := 0
		for _, d := range tools[0].FunctionDeclarations {
			if strings.HasPrefix(d.Name, "mcp__fake__") {
				found++
			}
		}
		if found != 2 {
			t.Error("MCP tools not offered to the model")
		}
		return &genai.Content{Role: genai.RoleModel, Parts: []*genai.Part{
			call(calls[0], map[string]any{"text": "hi"}), call(calls[1], map[string]any{}),
		}}, nil
	})
	s.done = make(chan struct{})
	s.runTurn(context.Background(), "t", "use the fake tools")
	joined := strings.Join(results, "\n")
	// The read-only tool runs; the other needs an approval this headless
	// server cannot give.
	if !strings.Contains(joined, "echo: hi") || strings.Contains(joined, "touched") || !strings.Contains(joined, "declined") {
		t.Fatal(joined)
	}
}

func TestMCPToolNamesAreModelSafe(t *testing.T) {
	if got := mcpToolName("my server", "read/file"); got != "mcp__my_server__read_file" {
		t.Fatal(got)
	}
}
