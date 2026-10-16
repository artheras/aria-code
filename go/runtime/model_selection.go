package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path/filepath"
	"sort"
	"strings"
	"time"
	"unicode"

	"google.golang.org/genai"
)

func (s *server) selectModel(provider, model string) (string, error) {
	provider = strings.ToLower(provider)
	if provider == "gcp" {
		provider = "vertexai"
	}
	if model == "" || len(model) > 512 || strings.IndexFunc(model, func(r rune) bool { return unicode.IsSpace(r) || unicode.IsControl(r) }) >= 0 {
		return "", errors.New("model ID must contain 1–512 bytes without spaces or control characters")
	}
	next := config{}
	for key, value := range s.cfg {
		next[key] = value
	}
	next["local_provider"], next["model"] = provider, model
	if provider == "vertexai" {
		next["use_vertexai"] = true
	}
	if supportedGoogleProvider(provider) {
		model = strings.TrimPrefix(model, "google/")
		next["model"] = model
		_, alias := mappingHas(next["tuned_models"], model)
		if !strings.HasPrefix(model, "gemini-") && !strings.HasPrefix(model, "gemma-") && !alias && !tunedEndpointName.MatchString(model) {
			return "", errors.New("use a Google model ID or a configured tuned model")
		}
	}
	p, err := modelProviderFor(next)
	if err != nil {
		return "", err
	}
	// Save provider and model together; a partial save could route a cloud
	// model to a local endpoint after restart. Preserve unrelated settings.
	disk, err := loadConfig(s.opts.root)
	if err != nil {
		return "", err
	}
	disk["local_provider"], disk["model"] = provider, model
	if provider == "vertexai" {
		disk["use_vertexai"] = true
	}
	if err := atomicJSON(filepath.Join(s.opts.root, "config.json"), disk); err != nil {
		return "", err
	}
	s.cfg, s.provider = next, p
	return fmt.Sprintf("Provider: %s\nModel: %s", provider, model), nil
}

func (s *server) modelsCommand(ctx context.Context, args []string) (string, error) {
	if len(args) > 1 {
		return "", errors.New("usage: /models [google|gcp|ollama|declared-provider]")
	}
	name := providerName(s.cfg)
	if len(args) == 1 {
		name = strings.ToLower(args[0])
	}
	if name == "gcp" {
		name = "vertexai"
	}
	names, more, err := s.availableModels(ctx, name)
	if err != nil {
		return "", err
	}
	if len(names) == 0 {
		return "No models returned by " + name + ". For Ollama, install a model with ollama pull <model>, then retry /models ollama.", nil
	}
	text := name + " models:\n" + strings.Join(names, "\n") + "\n\nSelect: /model " + name + " <model>"
	if more {
		text += "\nShowing the first 100 models; an exact model ID can also be selected."
	}
	return text, nil
}

func (s *server) availableModels(ctx context.Context, name string) ([]string, bool, error) {
	cfg := config{}
	for k, v := range s.cfg {
		cfg[k] = v
	}
	cfg["local_provider"] = name
	if name == "vertexai" {
		cfg["use_vertexai"] = true
	}
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	var names []string
	more := false
	if supportedGoogleProvider(name) {
		p := &googleProvider{cfg: cfg}
		client, err := p.getClient(ctx)
		if err != nil {
			return nil, false, err
		}
		page, err := client.Models.List(ctx, &genai.ListModelsConfig{PageSize: 100})
		if err != nil {
			return nil, false, p.safeError(err)
		}
		for _, m := range page.Items {
			id := strings.TrimPrefix(m.Name, "models/")
			if _, tail, ok := strings.Cut(id, "/models/"); ok {
				id = tail
			}
			if strings.HasPrefix(id, "gemini-") || strings.HasPrefix(id, "gemma-") {
				names = append(names, id)
			}
		}
		more = page.NextPageToken != ""
		names = append(names, tunedModelNames(cfg)...)
	} else {
		d, err := declaredProviderFor(cfg)
		if err != nil {
			return nil, false, err
		}
		names, err = declaredModels(ctx, d)
		if err != nil {
			return nil, false, err
		}
		if len(names) > 100 {
			names, more = names[:100], true
		}
	}
	sort.Strings(names)
	return names, more, nil
}

func (s *server) modelPicker(ctx context.Context, turn string) (string, error) {
	ids := []string{"vertexai", "ollama", "google"}
	choices := [][]string{{"Google Cloud / Vertex AI", "Use configured Cloud identity, project and region"}, {"Ollama", "Local service and installed models"}, {"Google (current authentication settings)", "Gemini API or configured Cloud route"}}
	data, _ := loadProviderConfig()
	declared, _ := parseDeclaredProviders(data)
	var extra []string
	for id := range declared {
		if !contains(ids, id) {
			extra = append(extra, id)
		}
	}
	sort.Strings(extra)
	for _, id := range extra {
		ids = append(ids, id)
		choices = append(choices, []string{id, declared[id].Name})
	}
	r, err := s.dialogRequest(ctx, "input.requested", turn, map[string]any{"title": "Select model provider / 选择模型服务", "choices": choices, "selected": 0})
	if err != nil {
		return "", err
	}
	if r.Choice == nil || *r.Choice < 0 || *r.Choice >= len(ids) {
		return "Model selection cancelled", nil
	}
	name := ids[*r.Choice]
	names, more, listErr := s.availableModels(ctx, name)
	if len(names) > 98 {
		names, more = names[:98], true
	}
	if err := ctx.Err(); err != nil {
		return "", err
	}
	contextText := "Select a model. Switching applies to subsequent turns in this conversation. / 选择模型后，后续消息将发送到该服务。"
	if listErr != nil {
		contextText += "\n" + listErr.Error()
	}
	if more {
		contextText += "\nFirst 100 models shown."
	}
	choices = nil
	for _, model := range names {
		choices = append(choices, []string{model, name})
	}
	choices = append(choices, []string{"Enter model ID / 输入模型名称", "Use an exact ID if discovery is unavailable"}, []string{"Cancel / 取消", "Keep the current model"})
	r, err = s.dialogRequest(ctx, "input.requested", turn, map[string]any{"title": name + " models", "context": contextText, "choices": choices, "selected": 0})
	if err != nil {
		return "", err
	}
	if r.Choice == nil || *r.Choice < 0 || *r.Choice > len(names) {
		return "Model selection cancelled", nil
	}
	if *r.Choice < len(names) {
		return s.selectModel(name, names[*r.Choice])
	}
	r, err = s.dialogRequest(ctx, "input.requested", turn, map[string]any{"prompt": "Model ID / 模型名称", "secret": false})
	if err != nil {
		return "", err
	}
	if strings.TrimSpace(r.Text) == "" {
		return "Model selection cancelled", nil
	}
	return s.selectModel(name, strings.TrimSpace(r.Text))
}

func declaredModels(ctx context.Context, d *declaredProvider) ([]string, error) {
	u, err := url.Parse(d.endpoint())
	if err != nil {
		return nil, err
	}
	u.Path = strings.TrimSuffix(u.Path, "/chat/completions") + "/models"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, err
	}
	req.Header, err = d.headers(ctx, false)
	if err != nil {
		return nil, err
	}
	client := &http.Client{Transport: nativeTransport(), Timeout: 20 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("cannot list %s models; check the service address and that the service is running", d.ID)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s model listing returned HTTP %d", d.ID, resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, (1<<20)+1))
	if err != nil {
		return nil, err
	}
	if len(body) > 1<<20 {
		return nil, errors.New("model list exceeds 1 MiB")
	}
	var result struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, errors.New("invalid model list response")
	}
	var names []string
	for _, row := range result.Data {
		if row.ID != "" && len(row.ID) <= 512 && strings.IndexFunc(row.ID, func(r rune) bool { return unicode.IsSpace(r) || unicode.IsControl(r) }) < 0 {
			names = appendNew(names, row.ID)
		}
	}
	return names, nil
}
