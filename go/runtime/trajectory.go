package main

// Full tool events for eval trajectories — the native form of
// apps/cli/exec_events.py with ARIA_EVENTS_FULL=1. A trajectory to learn
// from needs what was written and what came back, so tool.started carries the
// whole parameters and tool.completed the whole result, with secret-shaped
// values masked and results cut at 20,000 characters; training/sft_dataset.py
// turns them into Gemini functionCall / functionResponse turns. Without the
// variable the events stay short, as for logs and the chat.

import (
	"encoding/json"
	"os"
	"regexp"
	"strings"
)

const fullResultChars = 20_000

var secretShaped = regexp.MustCompile(`(?i)\b(api[_-]?key|token|password|passwd|secret)(\s*[=:]\s*)[^\s'"]+` +
	`|\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]+` +
	`|\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}|\bgh[pousr]_[A-Za-z0-9]{12,}|\bAIza[0-9A-Za-z_-]{20,}` +
	`|\bya29\.[0-9A-Za-z_-]+`)

// fullEvents is ARIA_EVENTS_FULL.
func fullEvents() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("ARIA_EVENTS_FULL"))) {
	case "1", "true", "yes":
		return true
	}
	return false
}

// maskText is exec_events._mask_text: secret-shaped values masked, the rest
// of the text — whitespace and newlines included — kept.
func maskText(text string) string {
	return secretShaped.ReplaceAllStringFunc(text, func(match string) string {
		m := secretShaped.FindStringSubmatch(match)
		switch {
		case m[1] != "":
			return m[1] + m[2] + "***"
		case m[3] != "":
			return m[3] + m[4] + "***"
		}
		return "***"
	})
}

// maskValue is exec_events._mask_value over decoded JSON: strings masked,
// keys starting with "_" (context the host added) dropped.
func maskValue(value any) any {
	switch v := value.(type) {
	case string:
		return maskText(v)
	case map[string]any:
		out := make(map[string]any, len(v))
		for key, item := range v {
			if !strings.HasPrefix(key, "_") {
				out[key] = maskValue(item)
			}
		}
		return out
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			out[i] = maskValue(item)
		}
		return out
	}
	return value
}

// jsonValue is a value as JSON would read it back.
func jsonValue(value any) any {
	body, err := json.Marshal(value)
	if err != nil {
		return nil
	}
	var out any
	json.Unmarshal(body, &out)
	return out
}

// fullParams are the call's whole parameters, masked.
func fullParams(params map[string]any) any { return maskValue(jsonValue(params)) }

// fullResult is the tool's whole result, masked, or its first 20,000
// characters as text with "…" when longer.
func fullResult(result toolResult) any {
	masked := maskValue(jsonValue(result))
	body, _ := json.Marshal(masked)
	if runes := []rune(string(body)); len(runes) > fullResultChars {
		return string(runes[:fullResultChars]) + "…"
	}
	return masked
}
