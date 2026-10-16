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

func TestReferenceReadPersistsSnapshot(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	path := filepath.Join(s.scope.workspace, "设计 notes.md")
	if err := os.WriteFile(path, []byte("unique original evidence"), 0600); err != nil {
		t.Fatal(err)
	}
	var events []string
	hooks := agentHooks{approve: s.approve, emit: func(kind string, _ map[string]any) { events = append(events, kind) }}
	text, err := resolveReferences(context.Background(), s.scope, `请分析 @file:"设计 notes.md"`, hooks)
	if err != nil || !strings.Contains(text, "unique original evidence") || len(events) != 2 {
		t.Fatal(text, err, events)
	}
	s.contents = []*genai.Content{genai.NewContentFromText(text, genai.RoleUser)}
	if err := s.persist(); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("changed later"), 0600); err != nil {
		t.Fatal(err)
	}
	snap, err := loadSession(s.opts.root, s.snap.ID)
	if err != nil {
		t.Fatal(err)
	}
	contents, err := sessionContents(snap)
	if err != nil || !strings.Contains(contents[0].Parts[0].Text, "unique original evidence") {
		t.Fatal(contents, err)
	}
	if got := referencePaths(`email a@b.com and @file:one.txt @file:one.txt @"two words.txt"`); len(got) != 2 {
		t.Fatal(got)
	}
}

func TestReferencesRespectPolicyBoundsAndTypes(t *testing.T) {
	for _, mode := range []string{"denied", "binary", "large", "outside", "missing"} {
		t.Run(mode, func(t *testing.T) {
			s := approvalServer(t, config{}, appOptions{})
			s.scope.readRoots = []string{s.scope.workspace}
			path := filepath.Join(s.scope.workspace, "a.txt")
			body := []byte("hello")
			switch mode {
			case "denied":
				atomicJSON(filepath.Join(s.opts.root, "tool_policy.json"), map[string]any{"denied": []string{"read_file"}})
			case "binary":
				body = []byte{0, 1, 2}
			case "large":
				body = []byte(strings.Repeat("x", (256<<10)+1))
			case "outside":
				path = filepath.Join(t.TempDir(), "private.txt")
			}
			if mode != "missing" {
				if err := os.WriteFile(path, body, 0600); err != nil {
					t.Fatal(err)
				}
			}
			encoded, _ := json.Marshal(path)
			_, err := resolveReferences(context.Background(), s.scope, "read @file:"+string(encoded), agentHooks{approve: s.approve, emit: func(string, map[string]any) {}})
			if err == nil {
				t.Fatal("invalid reference accepted")
			}
		})
	}
}

func TestReferenceCompletionIsScopedAndQuoted(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	if err := os.WriteFile(filepath.Join(s.scope.workspace, "设计 notes.md"), []byte("text"), 0600); err != nil {
		t.Fatal(err)
	}
	got := s.referenceCompletions("read @设计")
	if len(got) != 1 || got[0] != `read @file:"设计 notes.md"` {
		t.Fatal(got)
	}
	if got := s.referenceCompletions("read @../"); len(got) != 0 {
		t.Fatal("directory escaped", got)
	}
	if err := atomicJSON(filepath.Join(s.opts.root, "tool_policy.json"), map[string]any{"denied": []string{"list_files"}}); err != nil {
		t.Fatal(err)
	}
	if got := s.referenceCompletions("read @"); len(got) != 0 {
		t.Fatal("policy bypass", got)
	}
}
