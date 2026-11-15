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

func TestSessionImportRoundTripPreservesFileSnapshots(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{allowedTools: []string{"write_file"}})
	s.snap.Messages = exportConversation()
	s.contents = []*genai.Content{genai.NewContentFromText("original @file snapshot EVIDENCE", genai.RoleUser)}
	originalID := s.snap.ID
	if _, err := s.exportCommand(context.Background(), "t", []string{"session", "session copy.json"}); err != nil {
		t.Fatal(err)
	}
	result, err := s.importCommand(context.Background(), "t", `"session copy.json"`)
	if err != nil {
		t.Fatal(err)
	}
	id := strings.TrimSpace(strings.Split(result, "/resume ")[1])
	if id == originalID || s.snap.ID != originalID {
		t.Fatal("active session overwritten")
	}
	imported, err := loadSession(s.opts.root, id)
	if err != nil {
		t.Fatal(err)
	}
	contents, err := sessionContents(imported)
	if err != nil || contents[0].Parts[0].Text != "original @file snapshot EVIDENCE" {
		t.Fatal(contents, err)
	}
	if imported.Metadata["imported"] != true {
		t.Fatal(imported.Metadata)
	}
}

func TestSessionImportLegacyArrayAndRejectsMalformedHistory(t *testing.T) {
	body, _ := json.Marshal(exportConversation())
	snap, err := decodeSessionImport(body)
	if err != nil || len(snap.Messages) != 2 {
		t.Fatal(snap, err)
	}
	for _, raw := range []string{`null`, `{}`, `[{"role":"invalid","content":"hello"}]`, `{"messages":[],"provider_contents":[null]}`, `{"messages":[{"role":"user","content":[{"type":"not-supported"}]}]}`} {
		if _, err := decodeSessionImport([]byte(raw)); err == nil {
			t.Fatal("accepted invalid history", raw)
		}
	}
}

func TestSessionImportDeniedOrInvalidDoesNotCreateSession(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	if err := s.persist(); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadDir(filepath.Join(s.opts.root, "sessions"))
	file := filepath.Join(s.scope.workspace, "bad.json")
	if err := os.WriteFile(file, []byte(`{"messages":null}`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := s.importCommand(context.Background(), "t", file); err == nil {
		t.Fatal("invalid imported")
	}
	if err := atomicJSON(filepath.Join(s.opts.root, "tool_policy.json"), map[string]any{"denied": []string{"read_file"}}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.importCommand(context.Background(), "t", file); err == nil {
		t.Fatal("denial ignored")
	}
	after, _ := os.ReadDir(filepath.Join(s.opts.root, "sessions"))
	if len(before) != len(after) {
		t.Fatal("failed import created session")
	}
}

func TestSessionImportChatGPTExport(t *testing.T) {
	chatgptJSON := `[{
		"title": "Quant Strategy Review",
		"current_node": "n3",
		"mapping": {
			"n1": {"id": "n1", "parent": "", "message": {"author": {"role": "user"}, "content": {"parts": ["Review Sharpe ratio"]}}},
			"n2": {"id": "n2", "parent": "n1", "message": {"author": {"role": "assistant"}, "content": {"parts": ["Sharpe looks strong at 1.8."]}}},
			"n3": {"id": "n3", "parent": "n2", "message": {"author": {"role": "user"}, "content": {"parts": ["Add drawdown check"]}}}
		}
	}]`
	snap, err := decodeSessionImport([]byte(chatgptJSON))
	if err != nil {
		t.Fatalf("expected ChatGPT export to decode, got %v", err)
	}
	if len(snap.Messages) != 3 {
		t.Fatalf("expected 3 messages, got %d", len(snap.Messages))
	}
	if snap.Metadata["source"] != "chatgpt" || snap.Metadata["title"] != "Quant Strategy Review" {
		t.Fatalf("unexpected metadata: %+v", snap.Metadata)
	}
	if snap.Messages[0]["content"] != "Review Sharpe ratio" || snap.Messages[2]["content"] != "Add drawdown check" {
		t.Fatalf("unexpected message order: %+v", snap.Messages)
	}
}

func TestSessionImportClaudeCodeAndCodexJSONL(t *testing.T) {
	claudeJSONL := strings.Join([]string{
		`{"type":"user","message":{"role":"user","content":"Fix backtest slippage"}}`,
		`{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Updated backtest.rs slippage math."}]}}`,
	}, "\n")
	snapClaude, err := decodeSessionImport([]byte(claudeJSONL))
	if err != nil {
		t.Fatalf("expected Claude JSONL to decode, got %v", err)
	}
	if len(snapClaude.Messages) != 2 || snapClaude.Metadata["source"] != "claude_code" {
		t.Fatalf("unexpected Claude snapshot: %+v", snapClaude)
	}

	codexJSONL := strings.Join([]string{
		`{"type":"response_item","payload":{"role":"user","content":[{"type":"input_text","text":"Check gateway SSE"}]}}`,
		`{"type":"response_item","payload":{"role":"assistant","content":[{"type":"output_text","text":"SSE endpoint verified."}]}}`,
	}, "\n")
	snapCodex, err := decodeSessionImport([]byte(codexJSONL))
	if err != nil {
		t.Fatalf("expected Codex JSONL to decode, got %v", err)
	}
	if len(snapCodex.Messages) != 2 || snapCodex.Metadata["source"] != "codex" {
		t.Fatalf("unexpected Codex snapshot: %+v", snapCodex)
	}
}

func TestDiscoverExternalSessions(t *testing.T) {
	home := t.TempDir()
	claudeDir := filepath.Join(home, ".claude", "projects", "demo")
	codexDir := filepath.Join(home, ".codex", "sessions", "2026")
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(codexDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(claudeDir, "s1.jsonl"), []byte(`{"type":"user","message":{"role":"user","content":"hi"}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(codexDir, "s2.jsonl"), []byte(`{"type":"response_item","payload":{"role":"user","content":"hi"}}`), 0o644); err != nil {
		t.Fatal(err)
	}

	found := discoverExternalSessions(home, 10)
	if len(found) != 2 {
		t.Fatalf("expected 2 discovered sessions, got %d", len(found))
	}
}
