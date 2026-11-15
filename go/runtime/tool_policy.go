package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Denials always win, including over explicit invocation grants. Read the
// current policy for every operation so another terminal's changes take effect.
func persistentToolPolicy(root, name string) (string, error) {
	return persistentToolPolicyWithParams(root, name, nil)
}

func persistentToolPolicyWithParams(root, name string, params map[string]any) (string, error) {
	policy := struct {
		Allowed, Denied []string
		AskAlways       []string `json:"ask_always"`
	}{}
	if err := readJSON(filepath.Join(root, "tool_policy.json"), &policy); err != nil && !os.IsNotExist(err) {
		return "", fmt.Errorf("tool policy: %w", err)
	}
	groups := []struct {
		values  []string
		verdict string
	}{
		{policy.Denied, "deny"}, {policy.AskAlways, "ask"}, {policy.Allowed, "allow"},
	}
	// Pass 1: parameterized rules like run_command(pytest *) take precedence over blanket tool rules.
	if len(params) > 0 {
		for _, group := range groups {
			for _, rule := range group.values {
				if strings.Contains(rule, "(") && matchesPolicyRule(rule, name, params) {
					return group.verdict, nil
				}
			}
		}
	}
	// Pass 2: exact tool-level rules.
	for _, group := range groups {
		for _, rule := range group.values {
			if !strings.Contains(rule, "(") && strings.TrimSpace(rule) == name {
				return group.verdict, nil
			}
		}
	}
	return "default", nil
}

func policyRuleTarget(toolName string, params map[string]any) string {
	if len(params) == 0 {
		return ""
	}
	if toolName == "run_command" {
		if v, ok := params["command"].(string); ok {
			return strings.TrimSpace(v)
		}
		return ""
	}
	for _, key := range []string{"path", "file_path", "filename", "url", "query", "pattern"} {
		if v, ok := params[key].(string); ok && strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

func matchesPolicyRule(rule, toolName string, params map[string]any) bool {
	rule = strings.TrimSpace(rule)
	if rule == "" {
		return false
	}
	if idx := strings.IndexByte(rule, '('); idx >= 0 && strings.HasSuffix(rule, ")") {
		ruleTool := strings.TrimSpace(rule[:idx])
		if ruleTool != toolName {
			return false
		}
		pattern := strings.TrimSpace(rule[idx+1 : len(rule)-1])
		target := policyRuleTarget(toolName, params)
		if target == "" {
			return false
		}
		return matchGlob(pattern, target)
	}
	return rule == toolName
}

// matchGlob mirrors fnmatch-style '*' (including '/') and '?' matching.
func matchGlob(pattern, text string) bool {
	p, t := []rune(pattern), []rune(text)
	pi, ti := 0, 0
	starIdx, matchIdx := -1, 0
	for ti < len(t) {
		if pi < len(p) && (p[pi] == '?' || p[pi] == t[ti]) {
			pi++
			ti++
		} else if pi < len(p) && p[pi] == '*' {
			starIdx = pi
			matchIdx = ti
			pi++
		} else if starIdx != -1 {
			pi = starIdx + 1
			matchIdx++
			ti = matchIdx
		} else {
			return false
		}
	}
	for pi < len(p) && p[pi] == '*' {
		pi++
	}
	return pi == len(p)
}
