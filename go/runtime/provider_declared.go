package main

// Declared providers — the native form of src/aria_code/providers/llm/custom.py.
// Google uses its SDK; Ollama has a local OpenAI-compatible shortcut. Other
// models are reached through an
// OpenAI-compatible Chat Completions endpoint the user declares in their own
// configuration (a self-hosted vLLM, Ollama's /v1, LiteLLM, OpenRouter, a
// company gateway), the way Codex's model_providers and Claude Code's gateway
// settings work:
//
//	model_providers:
//	  mycorp:
//	    name: Company inference gateway
//	    base_url: https://llm.internal.example.com/v1
//	    env_key: MYCORP_LLM_TOKEN          # the variable's name, never its value
//	    env_key_instructions: Ask the platform team, then put it in ~/.aria/.env
//	    model: qwen-72b
//	    http_headers: {X-Tenant: research}
//	    env_http_headers: {X-Trace-Id: MYCORP_TRACE_ID}
//	    query_params: {api-version: "2025-04-01"}
//	    timeout: 120
//	  vertex-gateway:
//	    base_url: https://gateway.example.com/v1
//	    auth: {command: gcloud, args: [auth, print-access-token]}
//
// selected with local_provider: mycorp. Credentials come only from an
// environment variable (env_key) or a command that prints a token (auth, like
// Claude Code's apiKeyHelper); an entry that writes a key into the file is
// refused. Only user-level files are read: a project must not be able to send
// the conversation, or a credential, to a server of its choosing — which the
// Python loader's ./.aria.json allows and Codex forbids.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"go.yaml.in/yaml/v3"
)

// forbiddenInlineSecrets are keys that mean a secret was written into the file.
var forbiddenInlineSecrets = set("api_key", "apikey", "token", "bearer_token", "secret")

// reservedProviderIDs belong to the built-in Google provider.
var reservedProviderIDs = set("google", "gemini", "vertexai", "vertex-ai", "google-genai")

type providerAuth struct {
	Command   string
	Args      []string
	Cwd       string
	Refresh   time.Duration
	Timeout   time.Duration
	mu        sync.Mutex
	token     string
	fetchedAt time.Time
}

type declaredProvider struct {
	ID, Name, BaseURL    string
	EnvKey, Instructions string
	Model                string
	Headers, EnvHeaders  map[string]string
	QueryParams          map[string]string
	Timeout              time.Duration
	Auth                 *providerAuth
}

// providerConfigFiles are the user-level files model_providers is read from,
// the first that exists winning, as in the Python registry — without its
// project-relative ./.aria.json.
func providerConfigFiles() []string {
	var files []string
	if root, err := stateRoot(); err == nil {
		files = append(files, filepath.Join(root, "providers.json"))
	}
	if home, err := os.UserHomeDir(); err == nil {
		files = append(files, filepath.Join(home, ".aria", "providers.yaml"), filepath.Join(home, ".aria", "providers.json"))
	}
	return appendNew(nil, files...)
}

func loadProviderConfig() (map[string]any, string) {
	for _, path := range providerConfigFiles() {
		text, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var data any
		if strings.HasSuffix(path, ".yaml") || strings.HasSuffix(path, ".yml") {
			err = yaml.Unmarshal(text, &data)
		} else {
			err = json.Unmarshal(text, &data)
		}
		if mapping, ok := asMapping(data); ok && err == nil {
			return mapping, path
		}
		return nil, path
	}
	return nil, ""
}

func stringMap(value any) map[string]string {
	out := map[string]string{}
	if mapping, ok := asMapping(value); ok {
		for key, item := range mapping {
			out[key] = pyStr(item)
		}
	}
	return out
}

// parseDeclaredProviders is custom.parse_custom_providers: the usable
// entries, and why each unusable one was skipped. A broken entry never stops
// the others, or the runtime.
func parseDeclaredProviders(cfg map[string]any) (map[string]*declaredProvider, map[string]string) {
	specs, problems := map[string]*declaredProvider{}, map[string]string{}
	section, ok := asMapping(cfg["model_providers"])
	if !ok {
		return specs, problems
	}
	for rawID, rawEntry := range section {
		id := strings.ToLower(strings.TrimSpace(rawID))
		raw, ok := asMapping(rawEntry)
		if id == "" || !ok {
			problems[rawID] = "not a mapping"
			continue
		}
		var inline []string
		for key := range raw {
			if forbiddenInlineSecrets[strings.ToLower(key)] {
				inline = append(inline, key)
			}
		}
		if len(inline) > 0 {
			sort.Strings(inline)
			problems[id] = fmt.Sprintf("writes a secret into the file (%s); name an environment variable in env_key instead, e.g. env_key: %s_API_KEY",
				strings.Join(inline, ", "), strings.ToUpper(strings.ReplaceAll(id, "-", "_")))
			continue
		}
		if !truthy(raw["base_url"]) {
			problems[id] = "has no base_url"
			continue
		}
		if reservedProviderIDs[id] {
			problems[id] = "uses a name reserved for the built-in Google provider"
			continue
		}
		spec := &declaredProvider{
			ID: id, Name: id, BaseURL: strings.TrimRight(pyStr(raw["base_url"]), "/"),
			Headers: stringMap(raw["http_headers"]), EnvHeaders: stringMap(raw["env_http_headers"]),
			QueryParams: stringMap(raw["query_params"]), Timeout: 120 * time.Second,
		}
		if truthy(raw["name"]) {
			spec.Name = pyStr(raw["name"])
		}
		if truthy(raw["env_key"]) {
			spec.EnvKey = pyStr(raw["env_key"])
		}
		if truthy(raw["env_key_instructions"]) {
			spec.Instructions = pyStr(raw["env_key_instructions"])
		}
		if truthy(raw["model"]) {
			spec.Model = pyStr(raw["model"])
		}
		if n, ok := pyInt(raw["timeout"]); ok && n > 0 {
			spec.Timeout = time.Duration(n) * time.Second
		}
		if parsed, err := url.Parse(spec.BaseURL); err != nil || (parsed.Scheme != "https" && parsed.Scheme != "http") || parsed.Host == "" {
			problems[id] = "base_url must be an http(s) URL"
			continue
		}
		if authRaw, present := raw["auth"]; present {
			auth, ok := asMapping(authRaw)
			if !ok || !truthy(auth["command"]) {
				problems[id] = "auth needs a command that prints the token"
				continue
			}
			if spec.EnvKey != "" {
				problems[id] = "sets both env_key and auth; use one"
				continue
			}
			a := &providerAuth{Command: pyStr(auth["command"]), Refresh: 5 * time.Minute, Timeout: 5 * time.Second}
			for _, arg := range asSlice(auth["args"]) {
				a.Args = append(a.Args, pyStr(arg))
			}
			if truthy(auth["cwd"]) {
				a.Cwd = expandUser(pyStr(auth["cwd"]))
			}
			if n, ok := pyInt(auth["refresh_interval_ms"]); ok && n >= 0 {
				a.Refresh = time.Duration(n) * time.Millisecond
			}
			if n, ok := pyInt(auth["timeout_ms"]); ok && n > 0 {
				a.Timeout = time.Duration(n) * time.Millisecond
			}
			spec.Auth = a
		}
		specs[id] = spec
	}
	return specs, problems
}

// declaredProviderFor is the declared provider local_provider names, nil when
// it names the built-in Google provider, or an error saying why it cannot be
// used.
func declaredProviderFor(c config) (*declaredProvider, error) {
	name := strings.ToLower(strings.TrimSpace(c.text("local_provider", "google")))
	if supportedGoogleProvider(name) {
		return nil, nil
	}
	cfg, path := loadProviderConfig()
	specs, problems := parseDeclaredProviders(cfg)
	if spec := specs[name]; spec != nil {
		return spec, nil
	}
	if problem := problems[name]; problem != "" {
		return nil, fmt.Errorf("model provider %q in %s %s", name, path, problem)
	}
	if name == "ollama" {
		base := first(c.text("ollama_base_url", ""), os.Getenv("OLLAMA_HOST"), "http://127.0.0.1:11434")
		if !strings.Contains(base, "://") {
			base = "http://" + base
		}
		u, err := url.Parse(base)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
			return nil, errors.New("ollama_base_url must be an http(s) URL without credentials, query or fragment")
		}
		return &declaredProvider{ID: "ollama", Name: "Ollama", BaseURL: strings.TrimRight(base, "/"), Timeout: 120 * time.Second}, nil
	}
	where := "~/.aria/providers.yaml"
	if path != "" {
		where = path
	}
	return nil, fmt.Errorf("provider %q is not built in; declare it under model_providers in %s (an OpenAI-compatible base_url and env_key), or use local_provider google", name, where)
}

// chatCompletionsURL is openai_compat.chat_completions_url, with the
// declared query parameters.
var versionSuffix = regexp.MustCompile(`/v\d+(?:beta)?$`)

func (d *declaredProvider) endpoint() string {
	endpoint := d.BaseURL + "/v1/chat/completions"
	if versionSuffix.MatchString(d.BaseURL) {
		endpoint = d.BaseURL + "/chat/completions"
	}
	if len(d.QueryParams) > 0 {
		values := url.Values{}
		for key, value := range d.QueryParams {
			values.Set(key, value)
		}
		separator := "?"
		if strings.Contains(endpoint, "?") {
			separator = "&"
		}
		endpoint += separator + values.Encode()
	}
	return endpoint
}

// missingKeyHint is CustomProviderSpec.missing_key_hint.
func (d *declaredProvider) missingKeyHint() string {
	return strings.TrimSpace(fmt.Sprintf("provider '%s' 需要环境变量 %s，当前未设置或为空。 %s", d.ID, d.EnvKey, d.Instructions))
}

// headers are the request headers: the credential, the static headers, and
// the ones taken from environment variables — omitted, not sent empty, when
// the variable is unset.
func (d *declaredProvider) headers(ctx context.Context, refresh bool) (http.Header, error) {
	h := http.Header{}
	for name, value := range d.Headers {
		h.Set(name, value)
	}
	for name, variable := range d.EnvHeaders {
		if value := strings.TrimSpace(os.Getenv(variable)); value != "" {
			h.Set(name, value)
		}
	}
	switch {
	case d.Auth != nil:
		token, err := d.Auth.get(ctx, refresh)
		if err != nil {
			return nil, err
		}
		h.Set("Authorization", "Bearer "+token)
	case d.EnvKey != "":
		key := strings.TrimSpace(os.Getenv(d.EnvKey))
		if key == "" {
			return nil, errors.New(d.missingKeyHint())
		}
		h.Set("Authorization", "Bearer "+key)
	}
	return h, nil
}

// get runs the auth command when the cached token is older than the refresh
// interval, or when the server rejected it.
func (a *providerAuth) get(ctx context.Context, refresh bool) (string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.token != "" && !refresh && (a.Refresh == 0 || time.Since(a.fetchedAt) < a.Refresh) {
		return a.token, nil
	}
	ctx, cancel := context.WithTimeout(ctx, a.Timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, a.Command, a.Args...)
	cmd.Dir = a.Cwd
	cmd.Stdin = nil
	var stdout bytes.Buffer
	cmd.Stdout = &stdout
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("the provider's auth command failed: %v", err)
	}
	token := strings.TrimSpace(stdout.String())
	if token == "" || strings.ContainsAny(token, "\r\n") {
		return "", errors.New("the provider's auth command must print only the token")
	}
	a.token, a.fetchedAt = token, time.Now()
	return token, nil
}

// redact removes the provider's credentials from an error before it reaches
// an event or the model.
func (d *declaredProvider) redact(err error) error {
	text := err.Error()
	secrets := []string{}
	if d.EnvKey != "" {
		secrets = append(secrets, os.Getenv(d.EnvKey))
	}
	if d.Auth != nil {
		d.Auth.mu.Lock()
		secrets = append(secrets, d.Auth.token)
		d.Auth.mu.Unlock()
	}
	for _, variable := range d.EnvHeaders {
		secrets = append(secrets, os.Getenv(variable))
	}
	for _, secret := range secrets {
		if secret = strings.TrimSpace(secret); len(secret) >= 4 {
			text = strings.ReplaceAll(text, secret, "[redacted]")
		}
	}
	if runes := []rune(text); len(runes) > 800 {
		text = string(runes[:800])
	}
	return errors.New(text)
}

// modelProviderFor is the provider a configuration selects: Google unless
// local_provider names a declared provider.
func modelProviderFor(c config) (modelProvider, error) {
	spec, err := declaredProviderFor(c)
	if err != nil {
		return nil, err
	}
	if spec == nil {
		tuned, err := resolveTunedModel(c)
		if err != nil {
			return nil, err
		}
		if tuned != nil {
			return &googleProvider{cfg: c, model: tuned.resource, tuned: tuned}, nil
		}
		return googleModelProvider(c), nil
	}
	model := c.text("model", "")
	if c["model"] == nil || strings.TrimSpace(model) == "" {
		model = spec.Model
	}
	if model == "" {
		return nil, fmt.Errorf("provider %q needs a model: set model in the configuration or in its model_providers entry", spec.ID)
	}
	return &openAIProvider{cfg: c, model: model, declared: spec}, nil
}

// providerName is what the session reports as its provider.
func providerName(c config) string {
	if spec, err := declaredProviderFor(c); err == nil && spec != nil {
		return spec.ID
	}
	return "google"
}

// providersCommand answers `aria-runtime providers`: the declared providers,
// where they were read from and why any entry was skipped — without
// credentials, only whether each is available.
func providersCommand(stdout io.Writer) int {
	cfg, path := loadProviderConfig()
	specs, problems := parseDeclaredProviders(cfg)
	type row struct {
		ID           string            `json:"id"`
		Name         string            `json:"name"`
		BaseURL      string            `json:"base_url"`
		Endpoint     string            `json:"endpoint"`
		EnvKey       string            `json:"env_key,omitempty"`
		KeySet       bool              `json:"key_set"`
		AuthCommand  string            `json:"auth_command,omitempty"`
		Model        string            `json:"model,omitempty"`
		Headers      []string          `json:"headers"`
		Instructions string            `json:"env_key_instructions,omitempty"`
		Timeout      float64           `json:"timeout"`
		Query        map[string]string `json:"query_params"`
	}
	rows := []row{}
	for _, spec := range specs {
		r := row{ID: spec.ID, Name: spec.Name, BaseURL: spec.BaseURL, Endpoint: spec.endpoint(), EnvKey: spec.EnvKey, Model: spec.Model,
			Instructions: spec.Instructions, Timeout: spec.Timeout.Seconds(), Query: spec.QueryParams, Headers: []string{}}
		r.KeySet = spec.EnvKey == "" || strings.TrimSpace(os.Getenv(spec.EnvKey)) != ""
		if spec.Auth != nil {
			r.AuthCommand = spec.Auth.Command
		}
		for name := range spec.Headers {
			r.Headers = append(r.Headers, name)
		}
		for name, variable := range spec.EnvHeaders {
			if strings.TrimSpace(os.Getenv(variable)) != "" {
				r.Headers = appendNew(r.Headers, name)
			}
		}
		sort.Strings(r.Headers)
		rows = append(rows, r)
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i].ID < rows[j].ID })
	json.NewEncoder(stdout).Encode(map[string]any{"source": path, "providers": rows, "skipped": problems})
	return 0
}
