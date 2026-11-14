package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// verifyChangesTool inspects affected symbols/files via the project graph,
// infers targeted verification commands, runs them with a bounded timeout,
// and extracts concise failure diagnostics for Coder self-correction.
func verifyChangesTool(ctx context.Context, s scope, params map[string]any) toolResult {
	var paths []string
	if raw, ok := params["paths"].([]any); ok {
		for _, item := range raw {
			if p, ok := item.(string); ok && strings.TrimSpace(p) != "" {
				paths = append(paths, strings.TrimSpace(p))
			}
		}
	}
	if len(paths) == 0 {
		paths = gitChangedFiles(s.workspace)
	}

	var impactSummary string
	var affectedTests []string
	if len(paths) > 0 {
		g := loadCachedProjectGraph(s.workspace)
		if g == nil {
			g, _ = buildProjectGraph(ctx, s.workspace)
		}
		if g != nil {
			imp := g.impact(paths)
			impactSummary = imp.reportLine()
			affectedTests = imp.tests
		}
	}

	plan := planVerification(s.workspace, paths)
	if len(plan.Commands) == 0 {
		return toolResult{
			Success: true,
			Data: map[string]any{
				"paths":          paths,
				"impact_summary": impactSummary,
				"affected_tests": affectedTests,
				"commands_run":   []string{},
				"passed":         true,
				"reason":         plan.Reason,
			},
		}
	}

	type cmdOutcome struct {
		Command     string `json:"command"`
		Passed      bool   `json:"passed"`
		Diagnostics string `json:"diagnostics,omitempty"`
	}

	outcomes := make([]cmdOutcome, 0, len(plan.Commands))
	allPassed := true
	for _, cmd := range plan.Commands {
		runCtx, cancel := context.WithTimeout(ctx, 45*time.Second)
		res := commandTool(runCtx, commandSpec{
			command: cmd,
			cwd:     s.workspace,
			timeout: 45 * time.Second,
		})
		cancel()

		passed := res.Success
		diag := ""
		if !passed {
			allPassed = false
			rawOut, _ := res.Data["output"].(string)
			if rawOut == "" {
				rawOut = res.Error
			}
			diag = extractFailureDiagnostics(rawOut)
		}
		outcomes = append(outcomes, cmdOutcome{
			Command:     cmd,
			Passed:      passed,
			Diagnostics: diag,
		})
	}

	data := map[string]any{
		"paths":          paths,
		"impact_summary": impactSummary,
		"affected_tests": affectedTests,
		"reason":         plan.Reason,
		"passed":         allPassed,
		"results":        outcomes,
	}
	if !allPassed {
		if cpID, cpLabel := latestCheckpointHint(ctx); cpID != "" {
			data["latest_checkpoint_id"] = cpID
			data["latest_checkpoint_label"] = cpLabel
			data["recovery_hint"] = fmt.Sprintf("Verification failed. You can rewind to checkpoint %s (%s) via /rewind if repairs diverge.", cpID, cpLabel)
		}
	}

	return toolResult{
		Success: allPassed,
		Data:    data,
	}
}

func latestCheckpointHint(ctx context.Context) (string, string) {
	root, err := stateRoot()
	if err != nil || strings.TrimSpace(root) == "" {
		return "", ""
	}
	dbPath := filepath.Join(root, "checkpoints.db")
	if _, err := os.Stat(dbPath); err != nil {
		return "", ""
	}
	store, err := openCheckpoints(root)
	if err != nil {
		return "", ""
	}
	defer store.Close()

	var id, label string
	if err := store.db.QueryRowContext(ctx, `SELECT checkpoint_id, label FROM checkpoints ORDER BY sequence DESC LIMIT 1`).Scan(&id, &label); err != nil {
		return "", ""
	}
	return id, label
}

// extractFailureDiagnostics distills verbose test/compiler logs down to
// the key failing lines (up to 40 lines / 3000 chars) for rapid self-repair.
func extractFailureDiagnostics(output string) string {
	lines := strings.Split(strings.ReplaceAll(output, "\r\n", "\n"), "\n")
	var keyLines []string
	for i, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" {
			continue
		}
		upper := strings.ToUpper(trimmed)
		if strings.Contains(trimmed, "--- FAIL:") ||
			strings.HasPrefix(trimmed, "FAIL\t") ||
			strings.HasPrefix(trimmed, "FAILED ") ||
			strings.HasPrefix(trimmed, "E   ") ||
			strings.Contains(upper, "ERROR:") ||
			strings.Contains(upper, "PANIC:") ||
			strings.Contains(trimmed, "AssertionError") ||
			strings.Contains(trimmed, "error[E") ||
			strings.Contains(trimmed, ": undefined:") {
			keyLines = append(keyLines, line)
			// Include up to 2 trailing context lines for assertion detail
			for j := 1; j <= 2 && i+j < len(lines); j++ {
				if next := strings.TrimSpace(lines[i+j]); next != "" {
					keyLines = append(keyLines, lines[i+j])
				}
			}
		}
	}
	if len(keyLines) == 0 {
		// Fallback to last 25 lines if no standard marker matched
		start := 0
		if len(lines) > 25 {
			start = len(lines) - 25
		}
		keyLines = lines[start:]
	}
	if len(keyLines) > 40 {
		keyLines = keyLines[:40]
	}
	out := strings.Join(keyLines, "\n")
	if len(out) > 3000 {
		out = out[:3000] + "\n... [truncated]"
	}
	return out
}
