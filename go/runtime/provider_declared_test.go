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
	"strconv"
	"strings"
	"sync"
	"testing"

	"google.golang.org/genai"
)

func writeProviders(t *testing.T, body map[string]any) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("ARIA_HOME", home)
	text, _ := json.Marshal(body)
	if err := os.WriteFile(filepath.Join(home, "providers.json"), text, 0o600); err != nil {
		t.Fatal(err)
	}
	return home
}

func TestDeclaredProvidersAreValidated(t *testing.T) {
	specs, problems := parseDeclaredProviders(map[string]any{"model_providers": map[string]any{
		"Good":       map[string]any{"base_url": "https://llm.example.com/v1/", "env_key": "GOOD_KEY", "model": "qwen", "timeout": 30},
		"inline":     map[string]any{"base_url": "https://x.example", "api_key": "sk-leaked"},
		"nourl":      map[string]any{"env_key": "X"},
		"google":     map[string]any{"base_url": "https://x.example"},
		"file":       map[string]any{"base_url": "file:///etc/passwd"},
		"both":       map[string]any{"base_url": "https://x.example", "env_key": "X", "auth": map[string]any{"command": "x"}},
		"emptyauth":  map[string]any{"base_url": "https://x.example", "auth": map[string]any{}},
		"notamap":    "https://x.example",
		"commanding": map[string]any{"base_url": "http://localhost:8000", "auth": map[string]any{"command": "gcloud", "args": []any{"auth", "print-access-token"}, "refresh_interval_ms": 0}},
	}})
	if len(specs) != 2 || specs["good"] == nil || specs["commanding"] == nil {
		t.Fatal(specs, problems)
	}
	good := specs["good"]
	if good.BaseURL != "https://llm.example.com/v1" || good.Timeout.Seconds() != 30 || good.Model != "qwen" {
		t.Fatal(good)
	}
	if a := specs["commanding"].Auth; a.Command != "gcloud" || strings.Join(a.Args, " ") != "auth print-access-token" || a.Refresh != 0 {
		t.Fatal(a)
	}
	for id, want := range map[string]string{"inline": "env_key: INLINE_API_KEY", "nourl": "no base_url", "google": "reserved",
		"file": "http(s)", "both": "both env_key and auth", "emptyauth": "auth needs a command", "notamap": "not a mapping"} {
		if !strings.Contains(problems[id], want) {
			t.Errorf("%s: %q", id, problems[id])
		}
	}
	if strings.Contains(fmt.Sprint(problems), "sk-leaked") {
		t.Fatal("a skipped entry must not repeat the secret")
	}
}

func TestEndpointJoinsLikeTheOpenAICompatClient(t *testing.T) {
	for base, want := range map[string]string{
		"https://api.example.com":         "https://api.example.com/v1/chat/completions",
		"https://api.example.com/v1":      "https://api.example.com/v1/chat/completions",
		"https://x.example/v1beta/openai": "https://x.example/v1beta/openai/v1/chat/completions",
		"https://x.example/api/v3":        "https://x.example/api/v3/chat/completions",
	} {
		if got := (&declaredProvider{BaseURL: base}).endpoint(); got != want {
			t.Errorf("%s: %s", base, got)
		}
	}
	d := &declaredProvider{BaseURL: "https://x.example/openai/v1", QueryParams: map[string]string{"api-version": "2025-04-01"}}
	if got := d.endpoint(); got != "https://x.example/openai/v1/chat/completions?api-version=2025-04-01" {
		t.Fatal(got)
	}
}

func TestOnlyUserLevelFilesDeclareProviders(t *testing.T) {
	writeProviders(t, map[string]any{})
	for _, path := range providerConfigFiles() {
		if !filepath.IsAbs(path) || filepath.Base(path) == ".aria.json" {
			t.Fatal("a project file must not declare providers:", path)
		}
	}
}

// completionServer answers Chat Completions as a stream and records requests.
type completionServer struct {
	mu       sync.Mutex
	requests []*http.Request
	bodies   []map[string]any
	status   []int
}

func (c *completionServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	var body map[string]any
	json.NewDecoder(r.Body).Decode(&body)
	c.mu.Lock()
	c.requests = append(c.requests, r)
	c.bodies = append(c.bodies, body)
	status := http.StatusOK
	if len(c.status) > 0 {
		status, c.status = c.status[0], c.status[1:]
	}
	c.mu.Unlock()
	if status != http.StatusOK {
		http.Error(w, `{"error":"no"}`, status)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	io.WriteString(w, `data: {"choices":[{"index":0,"delta":{"content":"Hello from the gateway."}}]}`+"\n\n")
	io.WriteString(w, `data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"read_file","arguments":"{\"path\":\"a.py\"}"}}]},"finish_reason":"tool_calls"}]}`+"\n\n")
	io.WriteString(w, "data: [DONE]\n\n")
}

func TestDeclaredProviderStreamsThroughTheGateway(t *testing.T) {
	fake := &completionServer{}
	server := httptest.NewServer(fake)
	defer server.Close()
	writeProviders(t, map[string]any{"model_providers": map[string]any{"mycorp": map[string]any{
		"name": "Company gateway", "base_url": server.URL + "/v1", "env_key": "MYCORP_TOKEN", "model": "qwen-72b",
		"http_headers": map[string]any{"X-Tenant": "research"}, "env_http_headers": map[string]any{"X-Trace": "MYCORP_TRACE", "X-Unset": "MYCORP_UNSET"},
		"query_params": map[string]any{"api-version": "1"},
	}}})
	t.Setenv("MYCORP_TOKEN", "secret-token-123")
	t.Setenv("MYCORP_TRACE", "trace-9")
	provider, err := modelProviderFor(config{"local_provider": "MyCorp"})
	if err != nil {
		t.Fatal(err)
	}
	var streamed strings.Builder
	content, err := provider.Generate(context.Background(), []*genai.Content{genai.NewContentFromText("hi", genai.RoleUser)}, "system", nativeTools(), func(s string) { streamed.WriteString(s) })
	if err != nil {
		t.Fatal(err)
	}
	if streamed.String() != "Hello from the gateway." || content.Parts[len(content.Parts)-1].FunctionCall.Name != "read_file" {
		t.Fatal(streamed.String(), content.Parts)
	}
	r, body := fake.requests[0], fake.bodies[0]
	if r.URL.Path != "/v1/chat/completions" || r.URL.Query().Get("api-version") != "1" {
		t.Fatal(r.URL)
	}
	if r.Header.Get("Authorization") != "Bearer secret-token-123" || r.Header.Get("X-Tenant") != "research" || r.Header.Get("X-Trace") != "trace-9" {
		t.Fatal(r.Header)
	}
	if _, sent := r.Header["X-Unset"]; sent {
		t.Fatal("a header whose variable is unset is omitted, not sent empty")
	}
	if body["model"] != "qwen-72b" || body["tools"] == nil {
		t.Fatal("the declared model, unprefixed, with tools:", body["model"])
	}
	if providerName(config{"local_provider": "mycorp"}) != "mycorp" {
		t.Fatal("the session reports the declared provider")
	}
}

func TestDeclaredProviderErrorsNameTheFixAndHideTheKey(t *testing.T) {
	fake := &completionServer{status: []int{http.StatusForbidden}}
	server := httptest.NewServer(fake)
	defer server.Close()
	writeProviders(t, map[string]any{"model_providers": map[string]any{"mycorp": map[string]any{
		"base_url": server.URL, "env_key": "MYCORP_TOKEN", "env_key_instructions": "Ask the platform team.",
	}}})
	provider, err := modelProviderFor(config{"local_provider": "mycorp", "model": "m"})
	if err != nil {
		t.Fatal(err)
	}
	_, err = provider.Generate(context.Background(), []*genai.Content{genai.NewContentFromText("hi", genai.RoleUser)}, "s", nil, func(string) {})
	if err == nil || !strings.Contains(err.Error(), "MYCORP_TOKEN") || !strings.Contains(err.Error(), "Ask the platform team.") || len(fake.requests) != 0 {
		t.Fatal("an unset key is reported before any request:", err)
	}
	t.Setenv("MYCORP_TOKEN", "secret-token-123")
	_, err = provider.Generate(context.Background(), []*genai.Content{genai.NewContentFromText("hi", genai.RoleUser)}, "s", nil, func(string) {})
	if err == nil || err.Error() != "mycorp returned HTTP 403" {
		t.Fatal(err)
	}
	if got := (&declaredProvider{EnvKey: "MYCORP_TOKEN"}).redact(fmt.Errorf("bad token secret-token-123")); strings.Contains(got.Error(), "secret-token-123") {
		t.Fatal(got)
	}
	for _, cfg := range []config{{"local_provider": "nobody"}, {"local_provider": "mycorp"}} {
		if _, err := modelProviderFor(cfg); err == nil {
			t.Fatal("an undeclared provider or a missing model is an error:", cfg)
		}
	}
}

// TestAuthHelperProcess stands in for an auth command: it prints a token
// numbered by how often it has run.
func TestAuthHelperProcess(t *testing.T) {
	counter := os.Getenv("ARIA_TEST_TOKEN_COUNTER")
	if counter == "" {
		return
	}
	body, _ := os.ReadFile(counter)
	n, _ := strconv.Atoi(strings.TrimSpace(string(body)))
	n++
	os.WriteFile(counter, []byte(strconv.Itoa(n)), 0o600)
	fmt.Printf("token-%d\n", n)
	os.Exit(0)
}

func TestAuthCommandTokensAreCachedAndRefreshedOnRejection(t *testing.T) {
	fake := &completionServer{status: []int{http.StatusUnauthorized}}
	server := httptest.NewServer(fake)
	defer server.Close()
	counter := filepath.Join(t.TempDir(), "count")
	t.Setenv("ARIA_TEST_TOKEN_COUNTER", counter)
	writeProviders(t, map[string]any{"model_providers": map[string]any{"gw": map[string]any{
		"base_url": server.URL + "/v1", "model": "m",
		"auth": map[string]any{"command": os.Args[0], "args": []any{"-test.run=^TestAuthHelperProcess$"}},
	}}})
	provider, err := modelProviderFor(config{"local_provider": "gw"})
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if _, err := provider.Generate(context.Background(), []*genai.Content{genai.NewContentFromText("hi", genai.RoleUser)}, "s", nil, func(string) {}); err != nil {
			t.Fatal(err)
		}
	}
	var seen []string
	for _, r := range fake.requests {
		seen = append(seen, r.Header.Get("Authorization"))
	}
	// Rejected token-1 → one refresh to token-2, which is then cached.
	if strings.Join(seen, ",") != "Bearer token-1,Bearer token-2,Bearer token-2" {
		t.Fatal(seen)
	}
}

func TestUndeclaredProviderStopsTheSessionBeforeState(t *testing.T) {
	home := writeProviders(t, map[string]any{})
	root := t.TempDir()
	if err := atomicJSON(filepath.Join(root, "config.json"), config{"local_provider": "nosuchprovider"}); err != nil {
		t.Fatal(err)
	}
	_, err := newServer(appOptions{root: root, workspace: t.TempDir()}, io.Discard)
	if err == nil || !strings.Contains(err.Error(), `provider "nosuchprovider" is not built in`) || !strings.Contains(err.Error(), filepath.Join(home, "providers.json")) {
		t.Fatal(err)
	}
	if _, statErr := os.Stat(filepath.Join(root, "sessions")); !os.IsNotExist(statErr) {
		t.Fatal("no session state before the provider is known")
	}
}
