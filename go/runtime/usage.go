package main

// Token usage, the native /cost. Providers add what the API reports to a
// meter carried in the request context; the session keeps the totals in its
// metadata so they survive a resume.

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
)

type usageMeter struct {
	mu                    sync.Mutex
	input, output, thinks int64
	calls                 int64
}

type usageKey struct{}

func withUsage(ctx context.Context, m *usageMeter) context.Context {
	return context.WithValue(ctx, usageKey{}, m)
}

// recordUsage adds one model call's tokens; a context without a meter
// (tests, CLI subcommands) records nothing.
func recordUsage(ctx context.Context, input, output, thinking int64) {
	m, _ := ctx.Value(usageKey{}).(*usageMeter)
	if m == nil || (input == 0 && output == 0 && thinking == 0) {
		return
	}
	m.mu.Lock()
	m.input, m.output, m.thinks, m.calls = m.input+input, m.output+output, m.thinks+thinking, m.calls+1
	m.mu.Unlock()
}

func (m *usageMeter) totals() (input, output, thinking, calls int64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.input, m.output, m.thinks, m.calls
}

// sessionUsage loads the totals saved in the session.
func sessionUsage(metadata map[string]any) *usageMeter {
	m := &usageMeter{}
	saved, _ := asMapping(metadata["usage"])
	read := func(key string) int64 {
		n, _ := pyInt(saved[key])
		return int64(n)
	}
	m.input, m.output, m.thinks, m.calls = read("input_tokens"), read("output_tokens"), read("thinking_tokens"), read("model_calls")
	return m
}

func (s *server) saveUsage() {
	if s.usage == nil {
		return
	}
	input, output, thinking, calls := s.usage.totals()
	if calls == 0 {
		return
	}
	if s.snap.Metadata == nil {
		s.snap.Metadata = map[string]any{}
	}
	s.snap.Metadata["usage"] = map[string]any{"input_tokens": input, "output_tokens": output, "thinking_tokens": thinking, "model_calls": calls}
}

func (s *server) costCommand() (string, error) {
	input, output, thinking, calls := s.usage.totals()
	if calls == 0 {
		return "No model calls in this session yet (or the provider does not report usage).", nil
	}
	rows := [][2]string{
		{"Model calls", fmt.Sprint(calls)},
		{"Input tokens", thousands(int(input))},
		{"Output tokens", thousands(int(output))},
	}
	if thinking > 0 {
		rows = append(rows, [2]string{"Thinking tokens", thousands(int(thinking))})
	}
	// Prices differ per provider and contract, so Aria only estimates with
	// the rates the user sets: /config set token_price=<input>,<output>
	// (currency per million tokens).
	if price := s.cfg.text("token_price", ""); price != "" {
		var in, out float64
		if _, err := fmt.Sscanf(strings.ReplaceAll(price, " ", ""), "%g,%g", &in, &out); err == nil {
			cost := (float64(input)*in + float64(output+thinking)*out) / 1e6
			rows = append(rows, [2]string{"Estimated cost", fmt.Sprintf("%.4f (at %g / %g per million)", cost, in, out)})
		}
	} else {
		rows = append(rows, [2]string{"Cost", "/config set token_price=<input>,<output> per million tokens to estimate"})
	}
	return table("Usage this session", rows), nil
}

var thinkingLevels = map[string]string{"auto": "auto", "minimal": "minimal", "off": "minimal", "low": "low", "medium": "medium", "high": "high", "on": "high"}

func (s *server) thinkingCommand(args []string) (string, error) {
	if len(args) == 0 {
		return "thinking=" + s.cfg.text("thinking", "auto") + "\n/thinking auto|minimal|low|medium|high (on = high, off = minimal). Applies to Gemini models.", nil
	}
	level, ok := thinkingLevels[strings.ToLower(args[0])]
	if !ok || len(args) > 1 {
		return "", errors.New("usage: /thinking auto|minimal|low|medium|high")
	}
	next := config{}
	for key, value := range s.cfg {
		next[key] = value
	}
	next["thinking"] = level
	provider, err := modelProviderFor(next)
	if err != nil {
		return "", err
	}
	if err := s.saveSetting("thinking", level); err != nil {
		return "", err
	}
	s.cfg["thinking"] = level
	s.provider = provider
	note := ""
	if !supportedGoogleProvider(providerName(s.cfg)) {
		note = " (the current provider ignores it; it applies to Gemini models)"
	}
	return "thinking=" + level + note, nil
}
