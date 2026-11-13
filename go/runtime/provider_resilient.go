package main

import (
	"context"
	"fmt"
	"strings"
	"sync/atomic"

	"google.golang.org/genai"
)

type resilientProvider struct {
	primary      modelProvider
	fallback     modelProvider
	primaryName  string
	fallbackName string
	failoverHits int64
	onFailover   func(err error)
}

func (r *resilientProvider) Generate(ctx context.Context, contents []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
	content, err := r.primary.Generate(ctx, contents, system, tools, emit)
	if err == nil {
		return content, nil
	}
	if r.fallback != nil && isFailoverError(err) && ctx.Err() == nil {
		atomic.AddInt64(&r.failoverHits, 1)
		if r.onFailover != nil {
			r.onFailover(err)
		}
		fbContent, fbErr := r.fallback.Generate(ctx, contents, system, tools, emit)
		if fbErr == nil {
			return fbContent, nil
		}
		return nil, fmt.Errorf("primary model failed (%v) and fallback %s also failed: %w", err, r.fallbackName, fbErr)
	}
	return content, err
}

func isFailoverError(err error) bool {
	if err == nil {
		return false
	}
	s := strings.ToLower(err.Error())
	return strings.Contains(s, "429") ||
		strings.Contains(s, "rate limit") ||
		strings.Contains(s, "quota") ||
		strings.Contains(s, "overloaded") ||
		strings.Contains(s, "500") ||
		strings.Contains(s, "502") ||
		strings.Contains(s, "503") ||
		strings.Contains(s, "504") ||
		strings.Contains(s, "connection refused") ||
		strings.Contains(s, "timeout") ||
		strings.Contains(s, "temporarily unavailable") ||
		strings.Contains(s, "capacity")
}
