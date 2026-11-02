package main

import (
	"context"
	"fmt"
	"strings"
	"time"
)

// Recheck the server on every selection and inference. Never reuse a positive
// availability cache or substitute a different tag, family or cloud provider.
func requireOllamaModel(ctx context.Context, provider *declaredProvider, requested string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	names, err := declaredModels(ctx, provider)
	if err != nil {
		return "", fmt.Errorf("cannot verify installed Ollama models: %w; check Ollama and retry /models ollama", err)
	}
	for _, name := range names {
		if name == requested {
			return name, nil
		}
	}
	tail := requested[strings.LastIndex(requested, "/")+1:]
	if requested != "" && !strings.Contains(tail, ":") {
		for _, name := range names {
			if name == requested+":latest" {
				return name, nil
			}
		}
	}
	shown := names
	if len(shown) > 10 {
		shown = shown[:10]
	}
	installed := strings.Join(shown, ", ")
	if installed == "" {
		installed = "(none)"
	}
	return "", fmt.Errorf("Ollama model %q is not installed. Installed models: %s. Run ollama pull <model>, then select it with /model ollama <model>", requested, installed)
}
