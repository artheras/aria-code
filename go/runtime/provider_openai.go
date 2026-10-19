package main

// Vertex's Chat Completions API is the existing CLI-login route. Preserve that
// contract during migration, including fragmented calls and opaque signatures.
import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"

	"google.golang.org/genai"
)

type openAIProvider struct {
	cfg      config
	model    string
	mu       sync.Mutex
	client   *http.Client
	endpoint string
	// declared is a user-declared provider (provider_declared.go); nil for
	// Vertex's Chat Completions route.
	declared *declaredProvider
}

var errNoVisibleCompletion = errors.New("model returned no visible response or tool call")

func googleModelProvider(c config) modelProvider {
	model := c.text("model", "gemini-3.8-flash")
	options := googleClientConfig(c)
	backend := strings.ToLower(strings.TrimSpace(c.text("local_provider", "google")))
	compat := options.Backend == genai.BackendVertexAI && !hasDefaultCredentials() && (backend == "google" || backend == "gemini")
	compat = compat || strings.Contains(model, "-maas") || strings.HasPrefix(strings.TrimPrefix(model, "google/"), "gemma-")
	if c.text("google_protocol", "") == "chat-completions" {
		compat = true
	}
	if c.text("google_protocol", "") == "generate-content" {
		compat = false
	}
	if compat {
		return &openAIProvider{cfg: c, model: model}
	}
	return &googleProvider{cfg: c, model: model}
}

func (p *openAIProvider) connection(ctx context.Context) (*http.Client, string, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.client != nil {
		return p.client, p.endpoint, nil
	}
	if d := p.declared; d != nil {
		transport := nativeTransport()
		transport.ResponseHeaderTimeout = d.Timeout
		p.client, p.endpoint = &http.Client{Transport: transport}, d.endpoint()
		return p.client, p.endpoint, nil
	}
	options, err := googleHTTPOptions(ctx, p.cfg)
	if err != nil {
		return nil, "", err
	}
	if options.Backend != genai.BackendVertexAI {
		return nil, "", errors.New("Vertex Chat Completions requires a Google Cloud identity")
	}
	// IDs become URL path components; reject separators rather than allowing
	// a configuration value to change the authenticated destination.
	if !validID.MatchString(options.Project) || !validID.MatchString(options.Location) {
		return nil, "", errors.New("a valid Google Cloud project and location are required")
	}
	host := "aiplatform.googleapis.com"
	if options.Location != "global" {
		host = options.Location + "-" + host
	}
	p.endpoint = "https://" + host + "/v1/projects/" + url.PathEscape(options.Project) + "/locations/" + url.PathEscape(options.Location) + "/endpoints/openapi/chat/completions"
	p.client = options.HTTPClient
	return p.client, p.endpoint, nil
}

func openAIMessages(contents []*genai.Content, system string) ([]map[string]any, error) {
	messages := []map[string]any{{"role": "system", "content": system}}
	type pendingCall struct{ id, name string }
	var pending []pendingCall
	for i, content := range contents {
		if content == nil {
			return nil, errors.New("invalid nil provider content")
		}
		var text strings.Builder
		var blocks []map[string]any
		calls := []map[string]any{}
		responses := []map[string]any{}
		for j, part := range content.Parts {
			if part == nil {
				continue
			}
			if !part.Thought {
				text.WriteString(part.Text)
				if part.Text != "" {
					blocks = append(blocks, map[string]any{"type": "text", "text": part.Text})
				}
			}
			if call := part.FunctionCall; call != nil {
				args, err := json.Marshal(call.Args)
				if err != nil {
					return nil, err
				}
				id := call.ID
				if id == "" {
					id = fmt.Sprintf("native_%d_%d", i, j)
				}
				value := map[string]any{"id": id, "type": "function", "function": map[string]any{"name": call.Name, "arguments": string(args)}}
				if len(part.ThoughtSignature) > 0 {
					value["extra_content"] = map[string]any{"google": map[string]any{"thought_signature": base64.StdEncoding.EncodeToString(part.ThoughtSignature)}}
				}
				calls = append(calls, value)
				pending = append(pending, pendingCall{id, call.Name})
			}
			if response := part.FunctionResponse; response != nil {
				id := response.ID
				match := -1
				for k, call := range pending {
					if (id != "" && call.id == id) || (id == "" && call.name == response.Name) {
						id = call.id
						match = k
						break
					}
				}
				if match < 0 {
					return nil, errors.New("tool response does not match a pending call")
				}
				pending = append(pending[:match], pending[match+1:]...)
				body, err := json.Marshal(response.Response)
				if err != nil {
					return nil, err
				}
				responses = append(responses, map[string]any{"role": "tool", "tool_call_id": id, "content": string(body)})
			}
			if data := part.InlineData; data != nil {
				if !strings.HasPrefix(data.MIMEType, "image/") {
					return nil, errors.New("Chat Completions requires a native adapter for this attachment MIME type")
				}
				blocks = append(blocks, map[string]any{"type": "image_url", "image_url": map[string]any{"url": "data:" + data.MIMEType + ";base64," + base64.StdEncoding.EncodeToString(data.Data)}})
			}
			if data := part.FileData; data != nil {
				if !strings.HasPrefix(data.MIMEType, "image/") {
					return nil, errors.New("Chat Completions requires a native adapter for this attachment MIME type")
				}
				blocks = append(blocks, map[string]any{"type": "image_url", "image_url": map[string]any{"url": data.FileURI}})
			}
		}
		if len(calls) > 0 || len(blocks) > 0 {
			role := "user"
			if content.Role == "model" {
				role = "assistant"
			}
			value := map[string]any{"role": role, "content": text.String()}
			if len(blocks) > 0 && (len(blocks) != 1 || blocks[0]["type"] != "text") {
				value["content"] = blocks
			}
			if len(calls) > 0 {
				value["tool_calls"] = calls
			}
			messages = append(messages, value)
		}
		messages = append(messages, responses...)
	}
	return messages, nil
}

type completionCall struct {
	Index    int    `json:"index"`
	ID       string `json:"id"`
	Function struct {
		Name      string `json:"name"`
		Arguments string `json:"arguments"`
	} `json:"function"`
	Extra struct {
		Google struct {
			Signature string `json:"thought_signature"`
		} `json:"google"`
	} `json:"extra_content"`
}
type completionDelta struct {
	Content string           `json:"content"`
	Calls   []completionCall `json:"tool_calls"`
}
type completionEvent struct {
	Choices []struct {
		Index   int             `json:"index"`
		Delta   completionDelta `json:"delta"`
		Message completionDelta `json:"message"`
		Finish  *string         `json:"finish_reason"`
	} `json:"choices"`
	Error *struct {
		Message string `json:"message"`
	} `json:"error"`
}

// A stream is not executable until every call's arguments have been parsed and
// the server sends a completion marker. An EOF halfway through a call is an error.
func parseCompletions(r io.Reader, streaming bool, emit func(string)) (*genai.Content, error) {
	content := &genai.Content{Role: "model"}
	calls := map[int]*completionCall{}
	bytesRead := 0
	finished := false
	consume := func(body []byte) error {
		if bytes.Equal(bytes.TrimSpace(body), []byte("[DONE]")) {
			finished = true
			return nil
		}
		var event completionEvent
		if err := json.Unmarshal(body, &event); err != nil {
			return errors.New("invalid Chat Completions event")
		}
		if event.Error != nil {
			return errors.New("model stream reported an API error")
		}
		for _, choice := range event.Choices {
			if choice.Index != 0 {
				continue
			}
			delta := choice.Delta
			if !streaming {
				delta = choice.Message
				finished = true
			}
			if delta.Content != "" {
				content.Parts = append(content.Parts, &genai.Part{Text: delta.Content})
				emit(delta.Content)
			}
			for n, call := range delta.Calls {
				index := call.Index
				if !streaming {
					index = n
				}
				if index < 0 || index >= 32 {
					return errors.New("tool call index exceeds native batch limit")
				}
				acc := calls[index]
				if acc == nil {
					acc = &completionCall{}
					calls[index] = acc
				}
				acc.ID += call.ID
				acc.Function.Name += call.Function.Name
				acc.Function.Arguments += call.Function.Arguments
				if call.Extra.Google.Signature != "" {
					acc.Extra.Google.Signature += call.Extra.Google.Signature
				}
			}
			if choice.Finish != nil {
				switch *choice.Finish {
				case "stop", "tool_calls":
					finished = true
				case "length":
					return errors.New("model output was truncated at its token limit")
				case "content_filter":
					return errors.New("model output was blocked by its content filter")
				}
			}
		}
		return nil
	}
	if !streaming {
		body, err := io.ReadAll(io.LimitReader(r, 4<<20+1))
		if err != nil {
			return content, err
		}
		if len(body) > 4<<20 {
			return content, errors.New("model response exceeds 4 MiB")
		}
		if err := consume(body); err != nil {
			return content, err
		}
	} else {
		scanner := bufio.NewScanner(r)
		scanner.Buffer(make([]byte, 4096), 1<<20)
		var data []byte
		flush := func() error {
			if len(data) == 0 {
				return nil
			}
			err := consume(data)
			data = nil
			return err
		}
		for scanner.Scan() {
			line := scanner.Text()
			bytesRead += len(line)
			if bytesRead > 4<<20 {
				return content, errors.New("model response exceeds 4 MiB")
			}
			if line == "" {
				if err := flush(); err != nil {
					return content, err
				}
				if finished {
					break
				}
				continue
			}
			if strings.HasPrefix(line, "data:") {
				if len(data) > 0 {
					data = append(data, '\n')
				}
				data = append(data, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " ")...)
			}
		}
		if err := scanner.Err(); err != nil {
			return content, err
		}
		if err := flush(); err != nil {
			return content, err
		}
	}
	if !finished {
		return content, errors.New("model stream ended before completion")
	}
	indices := make([]int, 0, len(calls))
	for i := range calls {
		indices = append(indices, i)
	}
	sort.Ints(indices)
	ids := map[string]bool{}
	// Validate the entire batch first: no partial call is returned on errors.
	parts := []*genai.Part{}
	for _, index := range indices {
		call := calls[index]
		args := map[string]any{}
		if call.Function.Name == "" {
			return content, errors.New("tool call is missing its name")
		}
		if err := json.Unmarshal([]byte(call.Function.Arguments), &args); err != nil || args == nil {
			return content, errors.New("tool call arguments are not a complete JSON object")
		}
		if call.ID == "" {
			id, err := newID()
			if err != nil {
				return content, err
			}
			call.ID = "native_" + id
		}
		if ids[call.ID] {
			return content, errors.New("model returned duplicate tool call IDs")
		}
		ids[call.ID] = true
		part := &genai.Part{FunctionCall: &genai.FunctionCall{ID: call.ID, Name: call.Function.Name, Args: args}}
		if signature := call.Extra.Google.Signature; signature != "" {
			value, err := base64.StdEncoding.DecodeString(signature)
			if err != nil {
				return content, errors.New("invalid Google tool signature")
			}
			part.ThoughtSignature = value
		}
		parts = append(parts, part)
	}
	content.Parts = append(content.Parts, parts...)
	if !visibleOrCalls(content) {
		return content, errNoVisibleCompletion
	}
	return content, nil
}

func (p *openAIProvider) Generate(ctx context.Context, contents []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
	safe := (&googleProvider{cfg: p.cfg}).safeError
	label := "Google Chat Completions"
	if p.declared != nil {
		safe, label = p.declared.redact, p.declared.Name
	}
	client, endpoint, err := p.connection(ctx)
	if err != nil {
		return nil, safe(err)
	}
	messages, err := openAIMessages(contents, system)
	if err != nil {
		return nil, err
	}
	model := p.model
	if p.declared == nil && !strings.Contains(model, "/") {
		model = "google/" + model
	}
	maxTokens := p.cfg.number("max_output_tokens", 32768)
	payload := map[string]any{"model": model, "messages": messages, "stream": true, "temperature": 0.7, "max_tokens": maxTokens}
	if temperature, ok := p.cfg["temperature"].(float64); ok {
		payload["temperature"] = temperature
	}
	declarations := []map[string]any{}
	for _, tool := range tools {
		for _, d := range tool.FunctionDeclarations {
			schema := d.ParametersJsonSchema
			if schema == nil {
				schema = d.Parameters
			}
			declarations = append(declarations, map[string]any{"type": "function", "function": map[string]any{"name": d.Name, "description": d.Description, "parameters": schema}})
		}
	}
	if len(declarations) > 0 {
		payload["tools"] = declarations
		payload["tool_choice"] = "auto"
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	reauthorized := false
	for attempt := 0; attempt < 4; attempt++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		if p.declared != nil {
			headers, err := p.declared.headers(ctx, reauthorized)
			if err != nil {
				return nil, safe(err)
			}
			req.Header = headers
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Accept", "text/event-stream")
		response, err := client.Do(req)
		if err != nil {
			return nil, safe(err)
		}
		var content *genai.Content
		retry := false
		visible := false
		if response.StatusCode == http.StatusUnauthorized && p.declared != nil && p.declared.Auth != nil && !reauthorized {
			// A command-issued token may have expired: fetch a new one once.
			io.Copy(io.Discard, io.LimitReader(response.Body, 65536))
			response.Body.Close()
			reauthorized = true
			attempt--
			continue
		}
		if response.StatusCode != http.StatusOK {
			// Do not expose reflected bodies or credentials in public events.
			io.Copy(io.Discard, io.LimitReader(response.Body, 65536))
			err = fmt.Errorf("%s returned HTTP %d", label, response.StatusCode)
			retry = response.StatusCode == 429 || response.StatusCode == 500 || response.StatusCode == 503 || response.StatusCode == 504
		} else {
			streaming := !strings.Contains(response.Header.Get("Content-Type"), "application/json")
			content, err = parseCompletions(response.Body, streaming, func(text string) { visible = true; emit(text) })
			retry = errors.Is(err, errNoVisibleCompletion)
		}
		response.Body.Close()
		if err == nil {
			return content, nil
		}
		// Transport/protocol errors and partial output are never replayed.
		if visible || (content != nil && len(content.Parts) > 0) || !retry || attempt == 3 {
			return content, safe(err)
		}
		if err := waitModelRetry(ctx, attempt); err != nil {
			return content, err
		}
	}
	return nil, errors.New("model retry budget exhausted")
}

func waitModelRetry(ctx context.Context, attempt int) error {
	timer := time.NewTimer(time.Duration(attempt+1) * time.Second)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
