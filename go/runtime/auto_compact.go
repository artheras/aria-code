package main

import (
	"strings"
	"time"

	"google.golang.org/genai"
)

// Auto-compact runs before a model turn when the history is both long and
// close to the context window. It is local: asking an already-full local
// model to summarise itself is how the turn fails. /compact still asks the
// model. The transcript in the session is left as it is.

const (
	defaultContextWindow    = 16384
	defaultCompactThreshold = 0.78
	autoCompactMinMessages  = 8
	autoCompactTail         = 8
)

func contextWindow(cfg config) int {
	if n := cfg.number("context_window", 0); n > 0 {
		return n
	}
	if n := cfg.number("num_ctx", 0); n > 0 {
		return n
	}
	return defaultContextWindow
}

func compactThreshold(cfg config) float64 {
	raw := defaultCompactThreshold
	switch value := cfg["auto_compact_threshold"].(type) {
	case float64:
		raw = value
	case int:
		raw = float64(value)
	}
	if raw < 0.50 {
		raw = 0.50
	}
	if raw > 0.95 {
		raw = 0.95
	}
	return raw
}

func shouldAutoCompact(contents []*genai.Content, prompt string, cfg config) bool {
	if !cfg.boolean("auto_compact_context", true) || len(contents) < autoCompactMinMessages {
		return false
	}
	window := contextWindow(cfg)
	tokens := estimateTokens(contents) + len(prompt)/4
	return float64(tokens)/float64(window) >= compactThreshold(cfg)
}

func (s *server) maybeAutoCompact(turn, prompt string) bool {
	if !shouldAutoCompact(s.contents, prompt, s.cfg) {
		return false
	}
	before := estimateTokens(s.contents)
	next := compactContents(s.contents)
	if estimateTokens(next) >= before {
		return false
	}
	s.contents = next
	if s.snap.Metadata == nil {
		s.snap.Metadata = map[string]any{}
	}
	s.snap.Metadata["compacted_at"] = time.Now().UTC().Format(time.RFC3339)
	count := 0
	switch n := s.snap.Metadata["auto_compact_count"].(type) {
	case int:
		count = n
	case float64:
		count = int(n)
	}
	s.snap.Metadata["auto_compact_count"] = count + 1
	_ = s.emit("turn.status", turn, map[string]any{
		"state":   "auto_compact",
		"message": "Context was close to the window, so earlier turns were summarised locally",
	})
	return true
}

func compactContents(contents []*genai.Content) []*genai.Content {
	if len(contents) <= autoCompactTail {
		return contents
	}
	cut := len(contents) - autoCompactTail
	for cut > 0 && contents[cut].Role != genai.RoleUser {
		cut--
	}
	if cut == 0 {
		return contents
	}
	head := []*genai.Content{
		genai.NewContentFromText("Summary of the conversation so far:\n\n"+summarizeDropped(contents[:cut]), genai.RoleUser),
		genai.NewContentFromText("Understood. I will continue from this summary.", genai.RoleModel),
	}
	return append(head, contents[cut:]...)
}

func summarizeDropped(contents []*genai.Content) string {
	var b strings.Builder
	for _, content := range contents {
		text := strings.Join(strings.Fields(contentText(content)), " ")
		if text == "" {
			continue
		}
		runes := []rune(text)
		if len(runes) > 240 {
			text = string(runes[:240]) + "…"
		}
		role := "Aria"
		if content.Role == genai.RoleUser {
			role = "User"
		}
		if b.Len() > 0 {
			b.WriteString("\n")
		}
		b.WriteString(role)
		b.WriteString(": ")
		b.WriteString(text)
		if b.Len() > 1600 {
			break
		}
	}
	return b.String()
}
