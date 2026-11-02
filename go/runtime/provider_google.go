package main

// Google authentication and model streaming use the official Go SDK. Tool-call
// parts and their opaque thought signatures are kept intact across rounds;
// hidden reasoning is never forwarded to the terminal.
import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"cloud.google.com/go/auth/credentials"
	"cloud.google.com/go/auth/httptransport"
	"google.golang.org/genai"
)

type modelProvider interface {
	Generate(context.Context, []*genai.Content, string, []*genai.Tool, func(string)) (*genai.Content, error)
}
type googleProvider struct {
	// tuned is Aria's own model, a tuned Vertex endpoint; nil for base models.
	tuned  *tunedModel
	cfg    config
	model  string
	mu     sync.Mutex
	client *genai.Client
}

func first(values ...string) string {
	for _, v := range values {
		if strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}
func supportedGoogleProvider(name string) bool {
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "google", "gemini", "vertexai", "vertex-ai", "google-genai":
		return true
	}
	return false
}
func googleClientConfig(c config) *genai.ClientConfig {
	key := first(c.text("api_key", ""), c.text("gemini_key", ""), os.Getenv("GEMINI_API_KEY"), os.Getenv("GOOGLE_API_KEY"))
	vertex := key == "" || os.Getenv("GOOGLE_CLOUD_PROJECT") != "" || os.Getenv("GOOGLE_APPLICATION_CREDENTIALS") != ""
	backend := strings.ToLower(c.text("local_provider", "google"))
	if backend == "vertexai" || backend == "vertex-ai" || backend == "google-genai" {
		vertex = true
	}
	switch strings.ToLower(os.Getenv("GOOGLE_GENAI_USE_VERTEXAI")) {
	case "1", "true", "yes", "on":
		vertex = true
	case "0", "false", "no", "off":
		vertex = false
	}
	vertex = c.boolean("use_vertexai", vertex)
	if !vertex {
		return &genai.ClientConfig{Backend: genai.BackendGeminiAPI, APIKey: key}
	}
	return &genai.ClientConfig{Backend: genai.BackendVertexAI,
		Project:  first(os.Getenv("GOOGLE_CLOUD_PROJECT"), c.text("gcp_project", "")),
		Location: first(os.Getenv("GOOGLE_CLOUD_LOCATION"), c.text("gcp_location", ""), "global")}
}
func (p *googleProvider) getClient(ctx context.Context) (*genai.Client, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.client != nil {
		return p.client, nil
	}
	cfg := p.cfg
	if p.tuned != nil {
		cfg = config{}
		for key, value := range p.cfg {
			cfg[key] = value
		}
		cfg["use_vertexai"] = true
	}
	options, err := googleHTTPOptions(ctx, cfg)
	if err != nil {
		return nil, p.safeError(err)
	}
	if p.tuned != nil {
		if err := p.tuned.forTuned(options); err != nil {
			return nil, err
		}
	}
	client, err := genai.NewClient(ctx, options)
	if err != nil {
		return nil, p.safeError(err)
	}
	p.client = client
	return client, nil
}

// Shared authenticated transport for the SDK and the existing Vertex Chat
// Completions route. Tokens are refreshed by the auth library on every request.
func googleHTTPOptions(ctx context.Context, cfg config) (*genai.ClientConfig, error) {
	options := googleClientConfig(cfg)
	transport := nativeTransport()
	authClient := &http.Client{Timeout: 20 * time.Second, Transport: transport}
	if options.Backend == genai.BackendVertexAI {
		identity, _ := readCloudIdentity(cloudFolder())
		if options.Project == "" {
			options.Project = identity.project
		}
		// A custom CLOUDSDK_CONFIG ADC file is also an explicit identity.
		if path := filepath.Join(cloudFolder(), "application_default_credentials.json"); os.Getenv("GOOGLE_APPLICATION_CREDENTIALS") == "" {
			if _, err := os.Stat(path); err == nil {
				creds, err := credentials.DetectDefault(&credentials.DetectOptions{CredentialsFile: path, Scopes: []string{cloudScope}, Client: authClient})
				if err != nil {
					return nil, err
				}
				options.Credentials = creds
			}
		}
		if !hasDefaultCredentials() {
			creds, err := nativeCloudCredentials(ctx, identity)
			if err != nil {
				return nil, err
			}
			options.Credentials = creds
		}
	}
	options.HTTPOptions = genai.HTTPOptions{Timeout: genai.Ptr(45 * time.Second)}
	if options.Backend == genai.BackendVertexAI && options.APIKey == "" {
		if options.Credentials == nil {
			creds, err := credentials.DetectDefault(&credentials.DetectOptions{Scopes: []string{cloudScope}, Client: authClient})
			if err != nil {
				return nil, err
			}
			options.Credentials = creds
		}
		quota, err := options.Credentials.QuotaProjectID(ctx)
		if err != nil {
			return nil, err
		}
		headers := http.Header{}
		if quota != "" {
			headers.Set("X-Goog-User-Project", quota)
		}
		client, err := httptransport.NewClient(&httptransport.Options{Credentials: options.Credentials, BaseRoundTripper: transport, Headers: headers})
		if err != nil {
			return nil, err
		}
		client.Timeout = 45 * time.Second
		options.HTTPClient = client
	} else {
		options.HTTPClient = &http.Client{Timeout: 45 * time.Second, Transport: transport}
	}
	return options, nil
}
func (p *googleProvider) safeError(err error) error {
	text := err.Error()
	for _, key := range []string{p.cfg.text("api_key", ""), p.cfg.text("gemini_key", ""), os.Getenv("GEMINI_API_KEY"), os.Getenv("GOOGLE_API_KEY")} {
		if key != "" {
			text = strings.ReplaceAll(text, key, "[redacted]")
		}
	}
	chars := []rune(text)
	if len(chars) > 800 {
		chars = chars[:800]
	}
	return errors.New(string(chars))
}
func (p *googleProvider) Generate(ctx context.Context, contents []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
	client, err := p.getClient(ctx)
	if err != nil {
		return nil, err
	}
	temperature := float32(0.7)
	if n, ok := p.cfg["temperature"].(float64); ok {
		temperature = float32(n)
	}
	settings := &genai.GenerateContentConfig{Temperature: &temperature, Tools: tools,
		SystemInstruction: genai.NewContentFromText(system, genai.RoleUser)}
	if n := p.cfg.number("max_output_tokens", 0); n > 0 {
		settings.MaxOutputTokens = int32(n)
	}
	// /thinking: auto leaves the model's default.
	if level := p.cfg.text("thinking", "auto"); level != "auto" && level != "" {
		settings.ThinkingConfig = &genai.ThinkingConfig{ThinkingLevel: genai.ThinkingLevel(strings.ToUpper(level))}
	}
	for attempt := 0; attempt < 4; attempt++ {
		content := &genai.Content{Role: "model"}
		var failure error
		var finish string
		bytes := 0
		var usage *genai.GenerateContentResponseUsageMetadata
		for response, err := range client.Models.GenerateContentStream(ctx, p.model, contents, settings) {
			if err != nil {
				failure = err
				break
			}
			if response != nil && response.UsageMetadata != nil {
				// Each chunk carries the running totals; the last one counts.
				usage = response.UsageMetadata
			}
			if response == nil || len(response.Candidates) == 0 {
				continue
			}
			candidate := response.Candidates[0]
			if candidate.FinishReason != "" {
				finish = string(candidate.FinishReason)
			}
			if candidate.Content == nil {
				continue
			}
			for _, part := range candidate.Content.Parts {
				if part == nil {
					continue
				}
				bytes += len(part.Text) + len(part.ThoughtSignature)
				if bytes > 4<<20 {
					return content, errors.New("model response exceeds 4 MiB")
				}
				// Unsigned hidden prose is not persisted as an empty payload part.
				// Signed/structured parts remain intact in private provider history;
				// altering their payload could invalidate the opaque signature.
				if part.Thought {
					if len(part.ThoughtSignature) == 0 && part.FunctionCall == nil {
						continue
					}
					content.Parts = append(content.Parts, part)
				} else {
					content.Parts = append(content.Parts, part)
				}
				if part.Text != "" && !part.Thought {
					emit(part.Text)
				}
			}
		}
		if usage != nil {
			recordUsage(ctx, int64(usage.PromptTokenCount), int64(usage.CandidatesTokenCount), int64(usage.ThoughtsTokenCount))
		}
		if ctx.Err() != nil {
			return content, ctx.Err()
		}
		if failure == nil && visibleOrCalls(content) && finish == "STOP" {
			return content, nil
		}
		emptyRound := failure == nil && !visibleOrCalls(content) && (finish == "STOP" || finish == "")
		if failure == nil && !emptyRound {
			failure = fmt.Errorf("model stream did not finish successfully (finish: %s)", finish)
		}
		if emptyRound {
			failure = fmt.Errorf("model returned no visible response or tool call (finish: %s)", finish)
		}
		var apiError genai.APIError
		busy := errors.As(failure, &apiError) && (apiError.Code == 429 || apiError.Code == 500 || apiError.Code == 503 || apiError.Code == 504)
		// Never retry after producing a part: the caller must not replay output
		// or execute a partially repeated tool call.
		if len(content.Parts) > 0 || attempt == 3 || (!busy && !emptyRound) {
			return content, p.safeError(failure)
		}
		if err := waitModelRetry(ctx, attempt); err != nil {
			return content, err
		}
	}
	return nil, errors.New("model retry budget exhausted")
}
func visibleOrCalls(content *genai.Content) bool {
	for _, part := range content.Parts {
		if part.FunctionCall != nil || (!part.Thought && strings.TrimSpace(part.Text) != "") {
			return true
		}
	}
	return false
}
