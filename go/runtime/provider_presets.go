package main

// Built-in provider presets, the native form of the Python registry's
// cloud providers (providers/llm/openai_compat.py, anthropic.py,
// arthera_gateway.py). Each is an OpenAI-compatible Chat Completions
// endpoint with its key in an environment variable, so it is a declared
// provider the user does not have to declare: /model deepseek deepseek-chat
// works once DEEPSEEK_API_KEY is set. A model_providers entry with the same
// name still wins.
//
// Anthropic is reached through its OpenAI-compatible endpoint. The Arthera
// gateway serves Google models to a signed-in user: its bearer is the
// session /login saved, refreshed shortly before it expires.

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type providerPreset struct {
	id, name, baseURL, model, envKey, keyURL string
}

var providerPresets = []providerPreset{
	{"openai", "OpenAI", "https://api.openai.com", "gpt-4o", "OPENAI_API_KEY", "https://platform.openai.com/api-keys"},
	{"anthropic", "Anthropic", "https://api.anthropic.com/v1", "claude-3-7-sonnet", "ANTHROPIC_API_KEY", "https://console.anthropic.com/settings/keys"},
	{"deepseek", "DeepSeek", "https://api.deepseek.com", "deepseek-chat", "DEEPSEEK_API_KEY", "https://platform.deepseek.com/api_keys"},
	{"openrouter", "OpenRouter", "https://openrouter.ai/api", "deepseek/deepseek-r1", "OPENROUTER_API_KEY", "https://openrouter.ai/keys"},
	{"siliconflow", "SiliconFlow", "https://api.siliconflow.cn", "deepseek-ai/DeepSeek-V3", "SILICONFLOW_API_KEY", "https://cloud.siliconflow.cn/account/ak"},
	{"dashscope", "Alibaba DashScope", "https://dashscope.aliyuncs.com/compatible-mode", "qwen-plus", "DASHSCOPE_API_KEY", "https://bailian.console.aliyun.com/"},
	{"moonshot", "Moonshot", "https://api.moonshot.cn/v1", "moonshot-v1-8k", "MOONSHOT_API_KEY", "https://platform.moonshot.cn/console/api-keys"},
	{"zhipu", "Zhipu", "https://open.bigmodel.cn/api/paas/v4", "glm-4-plus", "ZHIPUAI_API_KEY", "https://open.bigmodel.cn/usercenter/apikeys"},
	{"minimax", "MiniMax", "https://api.minimax.chat/v1", "MiniMax-Text-01", "MINIMAX_API_KEY", "https://platform.minimaxi.com/"},
	{"groq", "Groq", "https://api.groq.com/openai", "llama-3.3-70b-versatile", "GROQ_API_KEY", "https://console.groq.com/keys"},
	{"together", "Together", "https://api.together.xyz", "meta-llama/Llama-3.3-70B-Instruct-Turbo", "TOGETHER_API_KEY", "https://api.together.xyz/settings/api-keys"},
	{"ollama", "Ollama (local)", "http://127.0.0.1:11434", "qwen2.5-coder", "", "https://ollama.com"},
	{"lmstudio", "LM Studio (local)", "http://localhost:1234", "", "", ""},
	{"vllm", "vLLM (local)", "http://localhost:8000", "", "", ""},
	{"llamacpp", "llama.cpp (local)", "http://localhost:8080", "", "", ""},
	{"openai-compatible", "OpenAI Compatible", "http://localhost:8000/v1", "", "OPENAI_API_KEY", ""},
}

const (
	gatewayProviderID   = "arthera"
	gatewayDefaultModel = "google/gemini-3.8-flash"
	// firebaseWebAPIKey is Arthera's public Firebase web key (the same one
	// the Python client ships); it identifies the project, it is not secret.
	firebaseWebAPIKey = "AIzaSyC9iYiroXgxTu2ANUPIjeGbITVzWEUjXAI"
	secureTokenURL    = "https://securetoken.googleapis.com/v1/token"
	refreshWithin     = 5 * time.Minute
)

func findPreset(id string) (providerPreset, bool) {
	for _, p := range providerPresets {
		if p.id == id {
			return p, true
		}
	}
	return providerPreset{}, false
}

// presetProvider is the built-in provider called name, if there is one.
func presetProvider(c config, name string) *declaredProvider {
	if name == gatewayProviderID {
		return &declaredProvider{ID: gatewayProviderID, Name: "Arthera gateway", Model: gatewayDefaultModel,
			BaseURL: loginAPIURL(c.text("api_url", "")) + "/api/v2/llm/v1", Timeout: 120 * time.Second,
			Instructions: "sign in with /login", Auth: &providerAuth{source: gatewaySessionToken, Timeout: 10 * time.Second}}
	}
	p, ok := findPreset(name)
	if !ok {
		return nil
	}
	base := p.baseURL
	switch name {
	case "lmstudio", "vllm", "llamacpp", "ollama", "openai-compatible", "openai_compatible":
		base = first(c.text("base_url", ""), c.text("endpoint_url", ""), c.text(name+"_base_url", ""), os.Getenv(strings.ToUpper(strings.ReplaceAll(name, "-", "_"))+"_BASE_URL"), base)
	default:
		base = first(c.text("base_url", ""), c.text("endpoint_url", ""), base)
	}
	model := first(c.text("model", ""), c.text(name+"_model", ""), os.Getenv(strings.ToUpper(strings.ReplaceAll(name, "-", "_"))+"_MODEL"), p.model)
	instructions := ""
	if p.keyURL != "" {
		instructions = "Get one at " + p.keyURL + " and export " + p.envKey + " in your shell profile."
	}
	return &declaredProvider{ID: p.id, Name: p.name, BaseURL: strings.TrimRight(base, "/"), EnvKey: p.envKey,
		Model: model, Instructions: instructions, Timeout: 120 * time.Second}
}

func applyRuntimeOptionOverrides(cfg config, options appOptions) error {
	if options.provider != "" {
		p := strings.ToLower(strings.TrimSpace(options.provider))
		if p == "gcp" {
			p = "vertexai"
		}
		cfg["local_provider"] = p
		if p == "vertexai" {
			cfg["use_vertexai"] = true
		}
	}
	if options.local {
		current := strings.ToLower(strings.TrimSpace(cfg.text("local_provider", "")))
		if options.provider == "" && (current == "" || supportedGoogleProvider(current)) {
			cfg["local_provider"] = "ollama"
		}
	}
	if options.model != "" {
		raw := strings.TrimSpace(options.model)
		if prefix, rest, ok := strings.Cut(raw, "/"); ok && rest != "" {
			p := strings.ToLower(strings.TrimSpace(prefix))
			if p == "google" {
				cfg["model"] = rest
			} else if options.provider == "" {
				if _, preset := findPreset(p); preset || p == "ollama" {
					cfg["local_provider"] = p
					cfg["model"] = rest
				} else {
					cfg["model"] = raw
				}
			} else if strings.EqualFold(cfg.text("local_provider", ""), p) {
				cfg["model"] = rest
			} else {
				cfg["model"] = raw
			}
		} else {
			cfg["model"] = raw
		}
	}
	if options.thinking {
		cfg["thinking"] = "high"
	}
	if strings.TrimSpace(options.url) != "" {
		base := strings.TrimSpace(options.url)
		if !strings.Contains(base, "://") {
			base = "http://" + base
		}
		u, err := url.Parse(base)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
			return errors.New("endpoint URL override must be an http(s) URL without credentials, query or fragment")
		}
		clean := strings.TrimRight(base, "/")
		cfg["endpoint_url"] = clean
		cfg["ollama_base_url"] = clean
	}
	return nil
}

// tokenExpiry is the exp claim of a JWT, zero when unreadable.
func tokenExpiry(token string) time.Time {
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return time.Time{}
	}
	body, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[1], "="))
	if err != nil {
		return time.Time{}
	}
	var claims struct {
		Exp float64 `json:"exp"`
	}
	if json.Unmarshal(body, &claims) != nil || claims.Exp == 0 {
		return time.Time{}
	}
	return time.Unix(int64(claims.Exp), 0)
}

// gatewaySessionToken is the token /login saved, renewed with its refresh
// token when it has five minutes or less left (session_refresh.py).
var gatewaySessionToken = func(ctx context.Context, refresh bool) (string, error) {
	root, err := stateRoot()
	if err != nil {
		return "", err
	}
	path := filepath.Join(root, "config.json")
	cfg, err := loadConfig(root)
	if err != nil {
		return "", err
	}
	token := cfg.text("auth_token", "")
	if token == "" || tokenClaims(token) == nil {
		return "", errors.New("the Arthera gateway needs you signed in: run /login")
	}
	due := refresh || time.Until(tokenExpiry(token)) <= refreshWithin
	refreshToken := cfg.text("refresh_token", "")
	if !due || refreshToken == "" {
		return token, nil
	}
	fresh, newRefresh, expiresIn, err := exchangeRefreshToken(ctx, refreshToken)
	if err != nil {
		// A failed refresh keeps the stored token; the API says if it is dead.
		return token, nil
	}
	latest, err := loadConfig(root)
	if err != nil {
		return token, nil
	}
	// Do not let a slow refresh for one account replace a later sign-in.
	if latest.text("auth_token", "") != token || latest.text("user_id", "") != cfg.text("user_id", "") {
		return latest.text("auth_token", ""), nil
	}
	if claims := tokenClaims(fresh); claims == nil || claims["sub"] != cfg.text("user_id", "") {
		return token, nil
	}
	latest["auth_token"] = fresh
	if newRefresh != "" {
		latest["refresh_token"] = newRefresh
	}
	latest["token_expires_at"] = time.Now().Add(time.Duration(expiresIn) * time.Second).UTC().Format(time.RFC3339)
	if err := atomicJSON(path, latest); err != nil {
		return fresh, nil
	}
	return fresh, nil
}

var secureTokenEndpoint = secureTokenURL

func exchangeRefreshToken(ctx context.Context, refreshToken string) (string, string, int, error) {
	ctx, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()
	form := url.Values{"grant_type": {"refresh_token"}, "refresh_token": {refreshToken}}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, secureTokenEndpoint+"?key="+firebaseWebAPIKey, strings.NewReader(form.Encode()))
	if err != nil {
		return "", "", 0, err
	}
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return "", "", 0, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return "", "", 0, fmt.Errorf("token refresh: HTTP %d", response.StatusCode)
	}
	var reply struct {
		IDToken      string `json:"id_token"`
		RefreshToken string `json:"refresh_token"`
		ExpiresIn    string `json:"expires_in"`
	}
	if err := json.NewDecoder(response.Body).Decode(&reply); err != nil || reply.IDToken == "" {
		return "", "", 0, errors.New("token refresh returned no token")
	}
	seconds := 3600
	fmt.Sscan(reply.ExpiresIn, &seconds)
	return reply.IDToken, reply.RefreshToken, seconds, nil
}
