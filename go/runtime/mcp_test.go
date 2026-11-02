package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"image"
	"image/png"
	"math/rand"
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
				if args["text"] == "__image__" {
					reply["result"] = map[string]any{"content": []any{map[string]any{"type": "text", "text": "observed"}, map[string]any{"type": "image", "mimeType": "image/png", "data": base64.StdEncoding.EncodeToString(nativeTestPNG(t))}}}
				}
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

func nativeTestPNG(t *testing.T) []byte {
	t.Helper()
	img := image.NewNRGBA(image.Rect(0, 0, 256, 256))
	rng := rand.New(rand.NewSource(42))
	rng.Read(img.Pix)
	var b bytes.Buffer
	if err := png.Encode(&b, img); err != nil {
		t.Fatal(err)
	}
	return b.Bytes()
}

func TestMCPImagesDecodedAndExcludedFromToolJSON(t *testing.T) {
	data := nativeTestPNG(t)
	encoded := base64.StdEncoding.EncodeToString(data)
	if len(encoded) < 65536 {
		t.Fatal("fixture must exceed default stream size")
	}
	raw, _ := json.Marshal(map[string]any{"content": []any{map[string]any{"type": "text", "text": "observed"}, map[string]any{"type": "image", "mimeType": "image/png", "data": encoded}}})
	result := decodeMCPToolResult(raw, "local", "observe")
	if !result.Success || len(result.Images) != 1 || !bytes.Equal(result.Images[0].Data, data) {
		t.Fatal(result.Error)
	}
	visible, _ := json.Marshal(result)
	if strings.Contains(string(visible), encoded) || strings.Contains(string(visible), "Images") {
		t.Fatal("image leaked into tool result JSON")
	}
	for _, image := range []map[string]any{
		{"type": "image", "mimeType": "image/png", "data": "bad-base64"},
		{"type": "image", "mimeType": "image/jpeg", "data": encoded},
		{"type": "image", "mimeType": "image/png", "data": base64.StdEncoding.EncodeToString([]byte("not png"))},
	} {
		raw, _ := json.Marshal(map[string]any{"content": []any{image}})
		if decodeMCPToolResult(raw, "local", "observe").Success {
			t.Fatal("accepted invalid image")
		}
	}
	raw, _ = json.Marshal(map[string]any{"isError": true, "content": []any{map[string]any{"type": "image", "mimeType": "image/png", "data": encoded}}})
	if result := decodeMCPToolResult(raw, "local", "observe"); result.Success || len(result.Images) != 0 {
		t.Fatal("error image forwarded")
	}
}

func TestNativeMCPImageReachesNextProviderTurn(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	fakeMCPServer(t, s)
	n := 0
	s.provider = providerFunc(func(_ context.Context, h []*genai.Content, _ string, _ []*genai.Tool, emit func(string)) (*genai.Content, error) {
		n++
		if n == 1 {
			return &genai.Content{Role: genai.RoleModel, Parts: []*genai.Part{call("mcp__fake__echo", map[string]any{"text": "__image__"})}}, nil
		}
		last := h[len(h)-1]
		if len(last.Parts) != 1 || last.Parts[0].InlineData == nil || last.Parts[0].InlineData.MIMEType != "image/png" {
			t.Fatal("image missing from next turn")
		}
		previous := h[len(h)-2]
		if previous.Parts[0].FunctionResponse == nil {
			t.Fatal("tool response must precede image message")
		}
		messages, err := openAIMessages(h, "")
		if err != nil {
			t.Fatal(err)
		}
		if messages[len(messages)-2]["role"] != "tool" || messages[len(messages)-1]["role"] != "user" {
			t.Fatal("OpenAI tool/image message order")
		}
		emit("image inspected")
		return genai.NewContentFromText("image inspected", genai.RoleModel), nil
	})
	s.done = make(chan struct{})
	s.runTurn(context.Background(), "t", "inspect the image")
	if n != 2 {
		t.Fatalf("expected 2 model turns, got %d", n)
	}
}

// A tool the server marks destructive (arthera-trading's order execution) is
// approved by a person every time; nothing standing can approve it instead.
func TestDestructiveMCPToolsAlwaysNeedAPerson(t *testing.T) {
	s := approvalServer(t, config{"approval_mode": "risk", "auto_approve_level": 3},
		appOptions{skipPermissions: true, allowedTools: []string{"mcp__trade__execute"}})
	if err := atomicJSON(filepath.Join(s.opts.root, "tool_policy.json"), map[string]any{"allowed": []string{"mcp__trade__execute"}}); err != nil {
		t.Fatal(err)
	}
	s.mcp = newMCPManager(s.opts.root)
	s.mcp.clients["trade"] = &mcpClient{tools: []mcpTool{
		{Name: "execute", Annotations: map[string]any{"destructiveHint": true}},
		{Name: "preview", Annotations: map[string]any{"destructiveHint": false}},
		{Name: "status", Annotations: map[string]any{"readOnlyHint": true, "destructiveHint": true}},
	}}
	s.grants["mcp__trade__execute"] = true
	ok, reason, err := s.approve(context.Background(), "mcp__trade__execute", map[string]any{"preview_id": "tp_1"}, fileChange{})
	if err != nil || ok || !strings.Contains(reason, "destructive") {
		t.Fatal(ok, reason, err)
	}
	// An ordinary write tool keeps its standing approvals, and a read-only one
	// is never destructive whatever else it claims.
	for _, name := range []string{"mcp__trade__preview", "mcp__trade__status"} {
		if ok, reason, err := s.approve(context.Background(), name, map[string]any{}, fileChange{}); err != nil || !ok {
			t.Fatal(name, ok, reason, err)
		}
	}
}
