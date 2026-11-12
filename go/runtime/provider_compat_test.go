package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"google.golang.org/genai"
)

func TestVertexRetryOnlyForBusyOrEmptyResponses(t *testing.T) {
	for _, first := range []string{"busy", "empty", "malformed", "partial", "permanent"} {
		t.Run(first, func(t *testing.T) {
			requests := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests++
				w.Header().Set("Content-Type", "text/event-stream")
				if requests == 1 {
					switch first {
					case "busy":
						w.WriteHeader(503)
						fmt.Fprint(w, "sensitive reflected body")
						return
					case "empty":
						fmt.Fprint(w, "data: [DONE]\n\n")
						return
					case "malformed":
						fmt.Fprint(w, "data: {bad\n\n")
						return
					case "partial":
						fmt.Fprint(w, `data: {"choices":[{"delta":{"content":"partial answer"}}]}`+"\n\n")
						return
					case "permanent":
						w.WriteHeader(403)
						return
					}
				}
				fmt.Fprint(w, `data: {"choices":[{"delta":{"content":"complete answer"},"finish_reason":"stop"}]}`+"\n\n")
			}))
			defer server.Close()
			p := &openAIProvider{cfg: config{}, model: "gemini-test", client: server.Client(), endpoint: server.URL}
			var visible strings.Builder
			_, err := p.Generate(context.Background(), nil, "Test", nil, func(text string) { visible.WriteString(text) })
			if first == "busy" || first == "empty" {
				if err != nil || requests != 2 || visible.String() != "complete answer" {
					t.Fatal(requests, visible.String(), err)
				}
			} else if err == nil || requests != 1 {
				t.Fatal(requests, visible.String(), err)
			}
		})
	}
}
func TestVertexBackoffStopsOnCancellation(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { requests++; w.WriteHeader(429) }))
	defer server.Close()
	p := &openAIProvider{cfg: config{}, model: "gemini-test", client: server.Client(), endpoint: server.URL}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	_, err := p.Generate(ctx, nil, "Test", nil, func(string) { t.Fatal("unexpected output") })
	if err == nil || requests != 1 {
		t.Fatal(requests, err)
	}
}

func TestVertexCompatibleStreamingAndSignedToolRoundTrip(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["model"] != "google/gemini-test" || body["stream"] != true {
			t.Error(body["model"], body["stream"])
		}
		messages := body["messages"].([]any)
		if requests == 2 {
			assistant := messages[len(messages)-2].(map[string]any)
			tool := messages[len(messages)-1].(map[string]any)
			call := assistant["tool_calls"].([]any)[0].(map[string]any)
			signature := call["extra_content"].(map[string]any)["google"].(map[string]any)["thought_signature"]
			if call["id"] != "call1" || signature != "b3BhcXVl" || tool["tool_call_id"] != "call1" {
				t.Error("call identity/signature lost", call["id"], signature, tool["tool_call_id"])
			}
		}
		w.Header().Set("Content-Type", "text/event-stream")
		if requests == 1 {
			fmt.Fprint(w, ": keepalive\r\ndata: {\"choices\":[{\"delta\":{\"reasoning_content\":\"private\",\"content\":\"读取\",\"tool_calls\":[{\"index\":0,\"id\":\"call1\",\"function\":{\"name\":\"read_\",\"arguments\":\"{\\\"path\\\":\"}}]}}]}\r\n\r\n")
			fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"name\":\"file\",\"arguments\":\"\\\"note.txt\\\"}\"},\"extra_content\":{\"google\":{\"thought_signature\":\"b3BhcXVl\"}}}]},\"finish_reason\":\"tool_calls\"}]}\n\n")
		} else {
			fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"内容已核对。\"},\"finish_reason\":\"stop\"}]}\n\n")
		}
	}))
	defer server.Close()
	p := &openAIProvider{cfg: config{}, model: "gemini-test", client: server.Client(), endpoint: server.URL}
	contents := []*genai.Content{genai.NewContentFromText("read the file", genai.RoleUser)}
	var visible strings.Builder
	answer, err := p.Generate(context.Background(), contents, "Test", nativeTools(), func(text string) { visible.WriteString(text) })
	if err != nil || visible.String() != "读取" || len(answer.Parts) != 2 {
		t.Fatal(visible.String(), err)
	}
	call := answer.Parts[1].FunctionCall
	if call.Name != "read_file" || call.Args["path"] != "note.txt" {
		t.Fatal(call)
	}
	contents = append(contents, answer, &genai.Content{Role: "user", Parts: []*genai.Part{{FunctionResponse: &genai.FunctionResponse{Name: call.Name, ID: call.ID, Response: map[string]any{"success": true, "content": "source"}}}}})
	if _, err := p.Generate(context.Background(), contents, "Test", nativeTools(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	if requests != 2 {
		t.Fatal("unexpected request count", requests)
	}
}

func TestIncompleteStreamsNeverProduceAnExecutableTool(t *testing.T) {
	for _, body := range []string{
		`data: {"choices":[{"delta":{"tool_calls":[{"id":"bad","function":{"name":"write_file","arguments":"{"}}]}}]}` + "\n\n",
		`data: {"choices":[{"delta":{"tool_calls":[{"id":"bad","function":{"name":"write_file","arguments":"{"}}]},"finish_reason":"tool_calls"}]}` + "\n\n",
		`data: {"choices":[{"delta":{"content":"partial","tool_calls":[{"id":"bad","function":{"name":"write_file","arguments":"{}"}}]},"finish_reason":"length"}]}` + "\n\n",
	} {
		content, err := parseCompletions(strings.NewReader(body), true, func(string) {})
		if err == nil {
			t.Fatal("unfinished stream was accepted")
		}
		for _, part := range content.Parts {
			if part.FunctionCall != nil {
				t.Fatal("unfinished mutation became executable")
			}
		}
	}
}

func TestCompletionResponseChoiceFilteringAndEmptyErrors(t *testing.T) {
	body := `data: {"choices":[{"index":1,"delta":{"content":"wrong choice"}},{"index":0,"delta":{"content":"正确"}}]}` + "\n\n" + "data: [DONE]\n\n"
	var text strings.Builder
	if _, err := parseCompletions(strings.NewReader(body), true, func(s string) { text.WriteString(s) }); err != nil || text.String() != "正确" {
		t.Fatal(text.String(), err)
	}
	for _, body := range []string{"data: [DONE]\n\n", `data: {"error":{"message":"model denied"}}` + "\n\n"} {
		if _, err := parseCompletions(strings.NewReader(body), true, func(string) {}); err == nil {
			t.Fatal("empty/error response accepted")
		}
	}
	content, err := parseCompletions(strings.NewReader(`{"choices":[{"message":{"content":"nonstream"}}]}`), false, func(string) {})
	if err != nil || content.Parts[0].Text != "nonstream" {
		t.Fatal(content, err)
	}
}

func TestParseCompletionsZeroArgsAndReasoningTokens(t *testing.T) {
	body := strings.Join([]string{
		`data: {"choices":[{"index":0,"delta":{"reasoning_content":"step 1","tool_calls":[{"index":0,"id":"c1","function":{"name":"git_status","arguments":""}},{"index":1,"id":"c2","function":{"name":"pwd","arguments":"null"}}]}}]}`,
		`data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":50,"completion_tokens":20,"completion_tokens_details":{"reasoning_tokens":8}}}`,
		`data: [DONE]`,
		"",
	}, "\n\n")
	var u tokenCounts
	var streamed strings.Builder
	content, err := parseCompletions(strings.NewReader(body), true, func(s string) { streamed.WriteString(s) }, &u)
	if err != nil {
		t.Fatal(err)
	}
	if streamed.String() != "" {
		t.Fatalf("reasoning_content should not be streamed as visible output: %q", streamed.String())
	}
	if len(content.Parts) != 2 {
		t.Fatalf("expected 2 tool call parts without private reasoning part, got %+v", content.Parts)
	}
	if content.Parts[0].FunctionCall == nil || content.Parts[0].FunctionCall.Name != "git_status" || len(content.Parts[0].FunctionCall.Args) != 0 {
		t.Fatalf("expected zero-arg git_status call, got %+v", content.Parts[0].FunctionCall)
	}
	if content.Parts[1].FunctionCall == nil || content.Parts[1].FunctionCall.Name != "pwd" || len(content.Parts[1].FunctionCall.Args) != 0 {
		t.Fatalf("expected zero-arg pwd call, got %+v", content.Parts[1].FunctionCall)
	}
	if u.PromptTokens != 50 || u.CompletionTokens != 20 || u.Details == nil || u.Details.ReasoningTokens != 8 {
		t.Fatalf("unexpected usage counts: %+v", u)
	}
}

func TestCompatibleHTTPFailuresNeverExposeReflectedCredentials(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(403)
		fmt.Fprint(w, "fixture-oauth-secret request body")
	}))
	defer server.Close()
	p := &openAIProvider{cfg: config{}, model: "gemini-test", client: server.Client(), endpoint: server.URL}
	_, err := p.Generate(context.Background(), nil, "Test", nil, func(string) {})
	if err == nil || strings.Contains(err.Error(), "fixture-oauth-secret") || !strings.Contains(err.Error(), "403") {
		t.Fatal(err)
	}
}

func TestLocalProviderCompactsOlderToolOutputsAndFiltersMultiEdit(t *testing.T) {
	hugeOutput := strings.Repeat("line of code\n", 200)
	shortOutput := "success"
	contents := []*genai.Content{
		{
			Role: "assistant",
			Parts: []*genai.Part{
				{FunctionCall: &genai.FunctionCall{ID: "call_1", Name: "read_file", Args: map[string]any{"path": "foo.go"}}},
			},
		},
		{
			Role: "user",
			Parts: []*genai.Part{
				{FunctionResponse: &genai.FunctionResponse{ID: "call_1", Name: "read_file", Response: map[string]any{"content": hugeOutput}}},
			},
		},
		{
			Role: "user",
			Parts: []*genai.Part{
				{Text: "now edit it"},
			},
		},
		{
			Role: "assistant",
			Parts: []*genai.Part{
				{FunctionCall: &genai.FunctionCall{ID: "call_2", Name: "edit_file", Args: map[string]any{"path": "foo.go"}}},
			},
		},
		{
			Role: "user",
			Parts: []*genai.Part{
				{FunctionResponse: &genai.FunctionResponse{ID: "call_2", Name: "edit_file", Response: map[string]any{"content": shortOutput}}},
			},
		},
	}
	messages, err := openAIMessages(contents, "System", true)
	if err != nil {
		t.Fatal(err)
	}
	// Older tool response should be compacted
	var olderToolContent, latestToolContent string
	for _, m := range messages {
		if m["role"] == "tool" {
			if m["tool_call_id"] == "call_1" {
				olderToolContent = m["content"].(string)
			}
			if m["tool_call_id"] == "call_2" {
				latestToolContent = m["content"].(string)
			}
		}
	}
	if !strings.Contains(olderToolContent, "truncated") {
		t.Fatalf("expected older tool output to be compacted, got length %d: %q", len(olderToolContent), olderToolContent)
	}
	if !strings.Contains(latestToolContent, "success") {
		t.Fatalf("expected latest tool output to be preserved, got: %q", latestToolContent)
	}
}

func TestParseJSONArgumentsRecovery(t *testing.T) {
	for name, tc := range map[string]struct {
		input string
		want  string
	}{
		"clean": {
			input: `{"command": "git status"}`,
			want:  "git status",
		},
		"markdown_code_block": {
			input: "```json\n{\"command\": \"ls -la\"}\n```",
			want:  "ls -la",
		},
		"trailing_comma": {
			input: `{"command": "echo hi",}`,
			want:  "echo hi",
		},
		"surrounding_text_and_trailing_comma": {
			input: "Here is the tool call:\n```\n{\"command\": \"pwd\",}\n```\nDone",
			want:  "pwd",
		},
	} {
		t.Run(name, func(t *testing.T) {
			args := map[string]any{}
			if err := parseJSONArguments(tc.input, &args); err != nil {
				t.Fatalf("failed to parse %q: %v", tc.input, err)
			}
			if args["command"] != tc.want {
				t.Fatalf("expected command=%q, got=%v", tc.want, args["command"])
			}
		})
	}
}

func TestLocalModelOptionsNumCtxInPayload(t *testing.T) {
	var capturedPayload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Logf("server received: %s %s", r.Method, r.URL.Path)
		if strings.HasSuffix(r.URL.Path, "/models") || strings.HasSuffix(r.URL.Path, "/tags") {
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"data":[{"id":"qwen2.5:7b"}],"models":[{"name":"qwen2.5:7b"}]}`)
			return
		}
		json.NewDecoder(r.Body).Decode(&capturedPayload)
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"hello\"}}]}\n\ndata: [DONE]\n\n")
	}))
	defer server.Close()

	p := &openAIProvider{
		cfg:      config{"local_provider": "ollama", "num_ctx": float64(16384)},
		model:    "qwen2.5:7b",
		client:   server.Client(),
		endpoint: server.URL + "/v1/chat/completions",
		declared: &declaredProvider{ID: "ollama", Name: "Ollama", BaseURL: server.URL},
	}
	_, err := p.Generate(context.Background(), []*genai.Content{{Role: "user", Parts: []*genai.Part{{Text: "hi"}}}}, "system", nil, func(s string) {})
	if err != nil {
		t.Logf("Generate returned err: %v", err)
	}

	opts, ok := capturedPayload["options"].(map[string]any)
	if !ok {
		t.Fatalf("expected options in payload for local model, got: %v", capturedPayload)
	}
	if opts["num_ctx"] != float64(16384) {
		t.Fatalf("expected num_ctx=16384, got: %v", opts["num_ctx"])
	}
	if opts["repeat_penalty"] != float64(1.1) {
		t.Fatalf("expected repeat_penalty=1.1, got: %v", opts["repeat_penalty"])
	}
	if opts["top_p"] != float64(0.9) {
		t.Fatalf("expected top_p=0.9, got: %v", opts["top_p"])
	}
}
