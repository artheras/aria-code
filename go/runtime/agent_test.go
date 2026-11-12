package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/genai"
)

type providerFunc func(context.Context, []*genai.Content, string, []*genai.Tool, func(string)) (*genai.Content, error)

func (f providerFunc) Generate(c context.Context, h []*genai.Content, s string, t []*genai.Tool, e func(string)) (*genai.Content, error) {
	return f(c, h, s, t, e)
}
func call(name string, args map[string]any) *genai.Part {
	return &genai.Part{FunctionCall: &genai.FunctionCall{Name: name, Args: args}, ThoughtSignature: []byte("signed-call")}
}
func quietHooks() agentHooks {
	return agentHooks{emit: func(string, map[string]any) {}, approve: func(context.Context, string, map[string]any, fileChange) (bool, string, error) { return true, "", nil }}
}
func TestSocialClassificationRetainsTasksAndContinuations(t *testing.T) {
	for _, s := range []string{"你好", "Hello, Aria!", "thank you", "你好呀", "👋"} {
		if !social(s) {
			t.Fatal(s)
		}
	}
	for _, s := range []string{"你好，读取 README.md", "hello there, fix tests", "yes", "continue", "hi.txt", "谢谢，继续"} {
		if social(s) {
			t.Fatal(s)
		}
	}
}
func TestModelCannotReadOrWriteFilesDuringStandaloneGreeting(t *testing.T) {
	root := t.TempDir()
	scope, err := newScope(root, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "note.txt"), []byte("private workspace"), 0644); err != nil {
		t.Fatal(err)
	}
	turn := 0
	provider := providerFunc(func(ctx context.Context, contents []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
		if len(tools) != 0 || !strings.Contains(system, "standalone social") {
			t.Fatal(system, tools)
		}
		turn++
		if turn == 1 {
			return &genai.Content{Role: "model", Parts: []*genai.Part{call("read_file", map[string]any{"path": "note.txt"}), call("write_file", map[string]any{"path": "note.txt", "content": "replace"})}}, nil
		}
		for _, part := range contents[len(contents)-1].Parts {
			if part.FunctionResponse.Response["success"] != false {
				t.Fatal("greeting executed a tool", part)
			}
		}
		emit("你好！")
		return genai.NewContentFromText("你好！", genai.RoleModel), nil
	})
	hooks := quietHooks()
	hooks.approve = func(context.Context, string, map[string]any, fileChange) (bool, string, error) {
		t.Fatal("greeting requested approval")
		return false, "", nil
	}
	_, answer, err := nativeTurn(context.Background(), provider, scope, config{}, nil, "你好", hooks)
	if err != nil || answer != "你好！" {
		t.Fatal(answer, err)
	}
	body, err := os.ReadFile(filepath.Join(root, "note.txt"))
	if err != nil || string(body) != "private workspace" {
		t.Fatal(string(body), err)
	}
}
func TestNativeReadResultsRetainSignaturesAndAreFreshAfterMutation(t *testing.T) {
	root := t.TempDir()
	scope, err := newScope(root, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "note.txt")
	if err := os.WriteFile(path, []byte("old evidence"), 0644); err != nil {
		t.Fatal(err)
	}
	stage := 0
	provider := providerFunc(func(ctx context.Context, contents []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
		stage++
		switch stage {
		case 1:
			return &genai.Content{Role: "model", Parts: []*genai.Part{call("read_file", map[string]any{"path": "note.txt"}), call("read_file", map[string]any{"path": "note.txt"})}}, nil
		case 2:
			model := contents[len(contents)-2]
			if string(model.Parts[0].ThoughtSignature) != "signed-call" {
				t.Fatal("signature lost")
			}
			results := contents[len(contents)-1].Parts
			if results[1].FunctionResponse.Response["reused"] != true {
				t.Fatal("duplicate not reused")
			}
			return &genai.Content{Role: "model", Parts: []*genai.Part{call("edit_file", map[string]any{"path": "note.txt", "old_string": "old evidence", "new_string": "new evidence"})}}, nil
		case 3:
			return &genai.Content{Role: "model", Parts: []*genai.Part{call("read_file", map[string]any{"path": "note.txt"})}}, nil
		default:
			data := contents[len(contents)-1].Parts[0].FunctionResponse.Response["data"].(map[string]any)
			if !strings.Contains(data["content"].(string), "new evidence") {
				t.Fatal("stale read", data)
			}
			emit("Read and verified.")
			return genai.NewContentFromText("Read and verified.", genai.RoleModel), nil
		}
	})
	hooks := quietHooks()
	approvals := 0
	hooks.approve = func(ctx context.Context, name string, params map[string]any, c fileChange) (bool, string, error) {
		if name == "edit_file" {
			approvals++
		}
		return true, "", nil
	}
	_, answer, err := nativeTurn(context.Background(), provider, scope, config{}, nil, "Edit note.txt and read it back", hooks)
	if err != nil || answer != "Read and verified." || approvals != 1 {
		t.Fatal(answer, approvals, err)
	}
}
func TestPersistentReadDenialIsCheckedBeforeOpeningTheFile(t *testing.T) {
	root := t.TempDir()
	scope, err := newScope(root, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	p := providerFunc(func(ctx context.Context, contents []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
		if len(contents) == 1 {
			return &genai.Content{Role: "model", Parts: []*genai.Part{call("read_file", map[string]any{"path": "does-not-exist"})}}, nil
		}
		result := contents[len(contents)-1].Parts[0].FunctionResponse.Response
		if result["success"] != false || !strings.Contains(result["error"].(string), "persistent denial") {
			t.Fatal(result)
		}
		emit("Denied.")
		return genai.NewContentFromText("Denied.", genai.RoleModel), nil
	})
	hooks := quietHooks()
	hooks.approve = func(context.Context, string, map[string]any, fileChange) (bool, string, error) {
		return false, "persistent denial", nil
	}
	_, _, err = nativeTurn(context.Background(), p, scope, config{}, nil, "Read a file", hooks)
	if err != nil {
		t.Fatal(err)
	}
}

func TestSanitizeModelAnswerCleansDegenerateSpacingAndBrokenLines(t *testing.T) {
	input := "尝 试\n\n执 行\n\n相 关\n\n操 作\n\n。 请\n\n告 诉 我\n\n您 的\n\n偏 好\n\n。"
	cleaned := sanitizeModelAnswer(input)
	expected := "尝试执行相关操作。请告诉我您的偏好。"
	if cleaned != expected {
		t.Fatalf("expected %q, got %q", expected, cleaned)
	}

	// English spacing and markdown must be preserved
	eng := "Hello world!\n\nThis is a normal paragraph with `code block`."
	if sanitizeModelAnswer(eng) != eng {
		t.Fatalf("expected %q, got %q", eng, sanitizeModelAnswer(eng))
	}
}

func TestRepetitiveFailingToolCallsGetLoopBreakerWarning(t *testing.T) {
	root := t.TempDir()
	scope, err := newScope(root, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	round := 0
	p := providerFunc(func(ctx context.Context, contents []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
		round++
		if round == 1 {
			// First failure
			return &genai.Content{Role: "model", Parts: []*genai.Part{call("read_file", map[string]any{"path": "missing.txt"})}}, nil
		}
		if round == 2 {
			lastResp := contents[len(contents)-1].Parts[0].FunctionResponse.Response
			errStr, _ := lastResp["error"].(string)
			if strings.Contains(errStr, "Warning: this exact call failed") {
				t.Fatalf("round 1 should not have warning yet, got: %v", errStr)
			}
			// Exact same failing call again
			return &genai.Content{Role: "model", Parts: []*genai.Part{call("read_file", map[string]any{"path": "missing.txt"})}}, nil
		}
		// Round 3 receives response of round 2 call
		lastResp := contents[len(contents)-1].Parts[0].FunctionResponse.Response
		errStr, _ := lastResp["error"].(string)
		if !strings.Contains(errStr, "Warning: this exact call failed 2 times consecutively") {
			t.Fatalf("expected loop breaker warning on round 2 repeat, got: %v", errStr)
		}
		emit("I notice the file is missing.")
		return genai.NewContentFromText("I notice the file is missing.", genai.RoleModel), nil
	})
	hooks := quietHooks()
	_, answer, err := nativeTurn(context.Background(), p, scope, config{}, nil, "Check missing file", hooks)
	if err != nil {
		t.Fatal(err)
	}
	if answer != "I notice the file is missing." {
		t.Fatalf("expected final answer, got: %q", answer)
	}
}
