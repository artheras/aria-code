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
