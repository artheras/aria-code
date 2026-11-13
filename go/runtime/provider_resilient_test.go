package main

import (
	"context"
	"errors"
	"strings"
	"testing"

	"google.golang.org/genai"
)

type mockProvider struct {
	result *genai.Content
	err    error
	called int
}

func (m *mockProvider) Generate(ctx context.Context, contents []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
	m.called++
	if m.err != nil {
		return nil, m.err
	}
	if emit != nil && m.result != nil && len(m.result.Parts) > 0 {
		emit(m.result.Parts[0].Text)
	}
	return m.result, nil
}

func TestResilientProviderPrimarySuccess(t *testing.T) {
	primary := &mockProvider{
		result: &genai.Content{Parts: []*genai.Part{{Text: "primary success"}}},
	}
	fallback := &mockProvider{
		result: &genai.Content{Parts: []*genai.Part{{Text: "fallback response"}}},
	}
	r := &resilientProvider{
		primary:      primary,
		fallback:     fallback,
		primaryName:  "primary-llm",
		fallbackName: "fallback-llm",
	}

	res, err := r.Generate(context.Background(), nil, "", nil, func(s string) {})
	if err != nil || res == nil || res.Parts[0].Text != "primary success" {
		t.Fatalf("expected primary success, got res=%v err=%v", res, err)
	}
	if primary.called != 1 || fallback.called != 0 {
		t.Fatalf("expected only primary called, primary=%d fallback=%d", primary.called, fallback.called)
	}
	if r.failoverHits != 0 {
		t.Fatalf("failoverHits should be 0, got %d", r.failoverHits)
	}
}

func TestResilientProviderFailoverOn429(t *testing.T) {
	primary := &mockProvider{
		err: errors.New("HTTP 429: rate limit exceeded, retry in 20s"),
	}
	fallback := &mockProvider{
		result: &genai.Content{Parts: []*genai.Part{{Text: "fallback success"}}},
	}
	failoverTriggered := false
	r := &resilientProvider{
		primary:      primary,
		fallback:     fallback,
		primaryName:  "primary-llm",
		fallbackName: "fallback-llm",
		onFailover: func(err error) {
			failoverTriggered = true
		},
	}

	var emitted string
	res, err := r.Generate(context.Background(), nil, "", nil, func(s string) { emitted = s })
	if err != nil || res == nil || res.Parts[0].Text != "fallback success" {
		t.Fatalf("expected fallback success, got res=%v err=%v", res, err)
	}
	if primary.called != 1 || fallback.called != 1 {
		t.Fatalf("expected both called, primary=%d fallback=%d", primary.called, fallback.called)
	}
	if !failoverTriggered || r.failoverHits != 1 {
		t.Fatalf("expected failover triggered once, got %v hits=%d", failoverTriggered, r.failoverHits)
	}
	if emitted != "fallback success" {
		t.Fatalf("expected emitted fallback text, got %q", emitted)
	}
}

func TestResilientProviderNonFailoverError(t *testing.T) {
	primary := &mockProvider{
		err: errors.New("invalid parameter: temperature must be between 0 and 2"),
	}
	fallback := &mockProvider{
		result: &genai.Content{Parts: []*genai.Part{{Text: "should not be called"}}},
	}
	r := &resilientProvider{
		primary:      primary,
		fallback:     fallback,
		primaryName:  "primary-llm",
		fallbackName: "fallback-llm",
	}

	_, err := r.Generate(context.Background(), nil, "", nil, func(s string) {})
	if err == nil || !strings.Contains(err.Error(), "invalid parameter") {
		t.Fatalf("expected parameter error without failover, got %v", err)
	}
	if fallback.called != 0 {
		t.Fatalf("fallback should not be called for client parameter error")
	}
}
