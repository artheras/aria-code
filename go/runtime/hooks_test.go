package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"google.golang.org/genai"
)

func writeHooks(t *testing.T, path string, hooks map[string]any) {
	t.Helper()
	os.MkdirAll(filepath.Dir(path), 0o755)
	body, _ := json.Marshal(hooks)
	if err := os.WriteFile(path, body, 0o644); err != nil {
		t.Fatal(err)
	}
}

// oneCall asks for one tool call, then records what the tool answered.
func oneCall(name string, args map[string]any, results *[]string) modelProvider {
	return providerFunc(func(_ context.Context, h []*genai.Content, _ string, _ []*genai.Tool, emit func(string)) (*genai.Content, error) {
		last := h[len(h)-1]
		if len(last.Parts) > 0 && last.Parts[0].FunctionResponse != nil {
			body, _ := json.Marshal(last.Parts[0].FunctionResponse.Response)
			*results = append(*results, string(body))
			emit("finished")
			return genai.NewContentFromText("finished", genai.RoleModel), nil
		}
		return &genai.Content{Role: genai.RoleModel, Parts: []*genai.Part{call(name, args)}}, nil
	})
}

func TestHooksVetoRecordAndReport(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("hooks in this test are POSIX shell")
	}
	s := approvalServer(t, config{}, appOptions{skipPermissions: true})
	log := filepath.Join(t.TempDir(), "hooks.log")
	writeHooks(t, filepath.Join(s.opts.root, "hooks.json"), map[string]any{
		"PreToolUse":   []any{map[string]any{"tool": "write_file", "command": "echo 'no writes today' >&2; exit 3", "blocking": true}},
		"PostToolUse":  []any{map[string]any{"command": `echo "post $ARIA_TOOL" >> ` + log}},
		"ResponseDone": []any{map[string]any{"command": `echo "done $ARIA_RESPONSE" >> ` + log}},
	})
	var results []string
	s.provider = oneCall("write_file", map[string]any{"path": "x.txt", "content": "x"}, &results)
	s.done = make(chan struct{})
	s.runTurn(context.Background(), "t", "write x please")
	if len(results) != 1 || !strings.Contains(results[0], "blocked by user hook") || !strings.Contains(results[0], "no writes today") {
		t.Fatal(results)
	}
	if _, err := os.Stat(filepath.Join(s.scope.workspace, "x.txt")); !os.IsNotExist(err) {
		t.Fatal("a vetoed write happened")
	}
	results = nil
	os.WriteFile(filepath.Join(s.scope.workspace, "r.txt"), []byte("hi"), 0o644)
	s.provider = oneCall("read_file", map[string]any{"path": "r.txt"}, &results)
	s.done = make(chan struct{})
	s.runTurn(context.Background(), "t", "read r please")
	body, _ := os.ReadFile(log)
	if !strings.Contains(string(body), "post read_file") || !strings.Contains(string(body), "done finished") || strings.Contains(string(body), "post write_file") {
		t.Fatalf("log:\n%s", body)
	}
}

func TestProjectHooksNeedTrust(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("hooks in this test are POSIX shell")
	}
	s := approvalServer(t, config{}, appOptions{})
	marker := filepath.Join(t.TempDir(), "ran")
	path := filepath.Join(s.scope.workspace, ".aria", "hooks.json")
	writeHooks(t, path, map[string]any{"SessionStart": []any{map[string]any{"command": "touch " + marker}}})
	s.fireHooks(context.Background(), "SessionStart", "", nil, nil, "")
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("an untrusted project hook ran")
	}
	out, _ := s.command(context.Background(), "t", "/hooks")
	if !strings.Contains(out, "not trusted") || !strings.Contains(out, "touch") {
		t.Fatal(out)
	}
	if out, _ := s.command(context.Background(), "t", "/hooks trust"); !strings.Contains(out, "Not trusted") {
		t.Fatal("trusted without an approval", out)
	}
	project := readHookFile(path, "project")
	atomicJSON(filepath.Join(s.opts.root, "trusted_hooks.json"), map[string]string{path: project.digest})
	s.fireHooks(context.Background(), "SessionStart", "", nil, nil, "")
	if _, err := os.Stat(marker); err != nil {
		t.Fatal("a trusted project hook did not run")
	}
	// Any change to the file withdraws the trust.
	os.Remove(marker)
	writeHooks(t, path, map[string]any{"SessionStart": []any{map[string]any{"command": "touch " + marker + "; true"}}})
	s.fireHooks(context.Background(), "SessionStart", "", nil, nil, "")
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("a changed project hook ran without new trust")
	}
	if out, _ := s.command(context.Background(), "t", "/doctor"); !strings.Contains(out, "Hooks") {
		t.Fatal(out)
	}
}

func TestBrokenHooksFileIsReported(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	os.WriteFile(filepath.Join(s.opts.root, "hooks.json"), []byte("{nope"), 0o644)
	if out, _ := s.command(context.Background(), "t", "/hooks"); !strings.Contains(out, "✗") {
		t.Fatal(out)
	}
	if ok, _ := s.fireHooks(context.Background(), "PreToolUse", "read_file", nil, nil, ""); !ok {
		t.Fatal("a broken file vetoed a call")
	}
}
