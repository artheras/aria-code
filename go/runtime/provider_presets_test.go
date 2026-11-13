package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestPresetsNeedNoDeclaration(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	t.Setenv("USERPROFILE", os.Getenv("HOME"))
	for _, tc := range []struct{ id, endpoint string }{
		{"deepseek", "https://api.deepseek.com/v1/chat/completions"},
		{"anthropic", "https://api.anthropic.com/v1/chat/completions"},
		{"zhipu", "https://open.bigmodel.cn/api/paas/v4/chat/completions"},
		{"groq", "https://api.groq.com/openai/v1/chat/completions"},
		{"lmstudio", "http://localhost:1234/v1/chat/completions"},
		{"vllm", "http://localhost:8000/v1/chat/completions"},
		{"llamacpp", "http://localhost:8080/v1/chat/completions"},
	} {
		spec, err := declaredProviderFor(config{"local_provider": tc.id})
		localServer := tc.id == "lmstudio" || tc.id == "vllm" || tc.id == "llamacpp"
		if err != nil || spec.endpoint() != tc.endpoint || (spec.Model == "" && !localServer) {
			t.Fatal(tc.id, spec, err)
		}
	}
	t.Setenv("DEEPSEEK_API_KEY", "")
	spec, _ := declaredProviderFor(config{"local_provider": "deepseek"})
	if _, err := spec.headers(context.Background(), false); err == nil || !strings.Contains(err.Error(), "DEEPSEEK_API_KEY") {
		t.Fatal("missing key not reported", err)
	}
	t.Setenv("DEEPSEEK_API_KEY", "sk-test")
	h, err := spec.headers(context.Background(), false)
	if err != nil || h.Get("Authorization") != "Bearer sk-test" {
		t.Fatal(h, err)
	}
}

func TestUserDeclarationWinsOverAPreset(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	os.MkdirAll(filepath.Join(home, ".aria"), 0o755)
	os.WriteFile(filepath.Join(home, ".aria", "providers.yaml"), []byte("model_providers:\n  openai:\n    base_url: https://proxy.example.com/v1\n    env_key: MY_KEY\n"), 0o644)
	spec, err := declaredProviderFor(config{"local_provider": "openai"})
	if err != nil || spec.BaseURL != "https://proxy.example.com/v1" || spec.EnvKey != "MY_KEY" {
		t.Fatal(spec, err)
	}
}

func TestPresetChatCompletionsCall(t *testing.T) {
	var auth, model string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth = r.Header.Get("Authorization")
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		model, _ = body["model"].(string)
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, `data: {"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}`+"\n\ndata: [DONE]\n\n")
	}))
	defer server.Close()
	t.Setenv("HOME", t.TempDir())
	t.Setenv("ANTHROPIC_API_KEY", "ant-key")
	t.Setenv("LMSTUDIO_DUMMY", "")
	p, err := modelProviderFor(config{"local_provider": "anthropic", "model": "claude-haiku-4-5"})
	if err != nil {
		t.Fatal(err)
	}
	op := p.(*openAIProvider)
	op.declared.BaseURL = server.URL + "/v1"
	var text strings.Builder
	if _, err := op.Generate(context.Background(), nil, "s", nil, func(s string) { text.WriteString(s) }); err != nil || text.String() != "hello" {
		t.Fatal(text.String(), err)
	}
	if auth != "Bearer ant-key" || model != "claude-haiku-4-5" {
		t.Fatal(auth, model)
	}
}

func TestGatewayUsesAndRefreshesTheLoginSession(t *testing.T) {
	root := t.TempDir()
	t.Setenv("ARIA_HOME", root)
	claims := goodClaims()
	claims["exp"] = float64(time.Now().Add(time.Minute).Unix()) // due for refresh
	stale := fakeToken(claims)
	claims["exp"] = float64(time.Now().Add(time.Hour).Unix())
	claims["fresh"] = true
	fresh := fakeToken(claims)
	var form string
	tokens := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		form = string(body)
		json.NewEncoder(w).Encode(map[string]string{"id_token": fresh, "refresh_token": "r2", "expires_in": "3600"})
	}))
	defer tokens.Close()
	old := secureTokenEndpoint
	secureTokenEndpoint = tokens.URL
	defer func() { secureTokenEndpoint = old }()

	if _, err := gatewaySessionToken(context.Background(), false); err == nil || !strings.Contains(err.Error(), "/login") {
		t.Fatal("signed out should ask for /login", err)
	}
	atomicJSON(filepath.Join(root, "config.json"), map[string]any{"auth_token": stale, "refresh_token": "r1", "user_id": "user-1", "model": "m"})
	token, err := gatewaySessionToken(context.Background(), false)
	if err != nil || token != fresh || !strings.Contains(form, "refresh_token=r1") {
		t.Fatal(token == fresh, form, err)
	}
	saved, _ := loadConfig(root)
	if saved.text("auth_token", "") != fresh || saved.text("refresh_token", "") != "r2" || saved.text("model", "") != "m" {
		t.Fatal(saved)
	}
	spec, err := declaredProviderFor(config{"local_provider": "arthera"})
	if err != nil || spec.endpoint() != artheraAPIURL+"/api/v2/llm/v1/chat/completions" || spec.Model != gatewayDefaultModel {
		t.Fatal(spec, err)
	}
	h, err := spec.headers(context.Background(), false)
	if err != nil || h.Get("Authorization") != "Bearer "+fresh {
		t.Fatal(err)
	}
}

func TestProvidersListsThePresets(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	t.Setenv("GROQ_API_KEY", "x")
	s := approvalServer(t, config{}, appOptions{})
	out, _ := s.command(context.Background(), "t", "/providers")
	for _, want := range []string{"deepseek", "anthropic", "GROQ_API_KEY is set", "needs DEEPSEEK_API_KEY", "arthera", "needs /login"} {
		if !strings.Contains(out, want) {
			t.Fatalf("lacks %q:\n%s", want, out)
		}
	}
}

func TestRuntimeOptionOverridesAndSaveUsage(t *testing.T) {
	cfg := config{"local_provider": "google", "model": "gemini-2.5-pro"}
	applyRuntimeOptionOverrides(cfg, appOptions{
		local:    true,
		model:    "ollama/qwen2.5-coder:7b",
		thinking: true,
		url:      "http://127.0.0.1:11434",
	})
	if cfg.text("local_provider", "") != "ollama" || cfg.text("model", "") != "qwen2.5-coder:7b" {
		t.Fatalf("unexpected provider/model: %v", cfg)
	}
	if cfg.text("ollama_base_url", "") != "http://127.0.0.1:11434" || cfg.text("thinking", "") != "high" {
		t.Fatalf("unexpected url/thinking: %v", cfg)
	}

	usagePath := filepath.Join(t.TempDir(), "usage.json")
	t.Setenv("ARIA_USAGE_FILE", usagePath)
	s := approvalServer(t, cfg, appOptions{})
	s.usage = &usageMeter{input: 42, output: 18, thinks: 7, calls: 2}
	s.saveUsage()
	raw, err := os.ReadFile(usagePath)
	if err != nil {
		t.Fatal(err)
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatal(err)
	}
	if payload["provider"] != "ollama" || payload["model"] != "qwen2.5-coder:7b" || payload["input_tokens"] != float64(42) || payload["thinking_tokens"] != float64(7) {
		t.Fatalf("unexpected usage file payload: %v", payload)
	}
}

func TestCursorOpenAICompatiblePresets(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	t.Setenv("USERPROFILE", os.Getenv("HOME"))
	for _, tc := range []struct {
		id       string
		endpoint string
		defModel string
	}{
		{"openrouter", "https://openrouter.ai/api/v1/chat/completions", "deepseek/deepseek-r1"},
		{"minimax", "https://api.minimax.chat/v1/chat/completions", "MiniMax-Text-01"},
		{"ollama", "http://127.0.0.1:11434/v1/chat/completions", "qwen2.5-coder"},
		{"openai-compatible", "http://localhost:8000/v1/chat/completions", ""},
	} {
		spec, err := declaredProviderFor(config{"local_provider": tc.id})
		if err != nil {
			t.Fatalf("declaredProviderFor(%s) error: %v", tc.id, err)
		}
		if spec.endpoint() != tc.endpoint {
			t.Fatalf("preset %s expected endpoint %s, got %s", tc.id, tc.endpoint, spec.endpoint())
		}
		if tc.defModel != "" && spec.Model != tc.defModel {
			t.Fatalf("preset %s expected model %s, got %s", tc.id, tc.defModel, spec.Model)
		}
	}

	// Verify generic override via base_url and model
	customSpec, err := declaredProviderFor(config{
		"local_provider": "openai-compatible",
		"base_url":       "http://10.0.0.5:1234/v1",
		"model":          "custom-llm-v1",
	})
	if err != nil {
		t.Fatalf("unexpected error for custom openai-compatible: %v", err)
	}
	if customSpec.endpoint() != "http://10.0.0.5:1234/v1/chat/completions" || customSpec.Model != "custom-llm-v1" {
		t.Fatalf("custom override failed: endpoint=%s model=%s", customSpec.endpoint(), customSpec.Model)
	}
}
