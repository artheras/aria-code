package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// WorldTransition represents one Action-State-Verifier tuple (S, A, R, S')
// suitable for training outcome/critic/world models across code, physical, and quant domains.
type WorldTransition struct {
	Timestamp    string         `json:"timestamp"`
	Domain       string         `json:"domain"` // "code", "physical", "quant", "general"
	StateSummary map[string]any `json:"state_summary,omitempty"`
	ActionTool   string         `json:"action_tool"`
	ActionArgs   any            `json:"action_args,omitempty"`
	OutcomeOK    bool           `json:"outcome_ok"`
	RewardScore  float64        `json:"reward_score"`
	ErrorSummary string         `json:"error_summary,omitempty"`
	ElapsedMs    int64          `json:"elapsed_ms"`
}

func worldTrajectoriesEnabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("ARIA_WORLD_TRAJECTORIES"))) {
	case "1", "true", "yes":
		return true
	}
	return false
}

func worldTransitionsPath(stateRoot string) string {
	if strings.TrimSpace(stateRoot) == "" {
		return ""
	}
	return filepath.Join(stateRoot, "world_transitions.jsonl")
}

func classifyTransitionDomain(toolName string) string {
	switch toolName {
	case "edit_file", "multi_edit", "write_file", "verify_changes", "simulate_action", "run_command", "git_status", "git_diff":
		return "code"
	case "solve_spatial_constraints", "inspect_3d_asset":
		return "physical"
	case "price_option", "optimize_portfolio", "run_backtest":
		return "quant"
	default:
		return "general"
	}
}

func computeTransitionReward(toolName string, res toolResult) float64 {
	if !res.Success {
		return -1.0
	}
	if code, ok := res.Data["exit_code"].(int); ok && code != 0 {
		return -0.8
	}
	switch toolName {
	case "verify_changes":
		return 1.0
	case "edit_file", "multi_edit", "write_file":
		return 0.6
	case "solve_spatial_constraints", "run_backtest", "optimize_portfolio", "price_option":
		return 0.5
	default:
		return 0.2
	}
}

func recordWorldTransition(stateRoot, workspace, toolName string, params map[string]any, res toolResult, elapsed time.Duration) {
	path := worldTransitionsPath(stateRoot)
	if path == "" {
		return
	}
	entry := WorldTransition{
		Timestamp: time.Now().UTC().Format(time.RFC3339),
		Domain:    classifyTransitionDomain(toolName),
		StateSummary: map[string]any{
			"workspace_name": filepath.Base(workspace),
		},
		ActionTool:   toolName,
		ActionArgs:   fullParams(params),
		OutcomeOK:    res.Success,
		RewardScore:  computeTransitionReward(toolName, res),
		ErrorSummary: capText(maskText(res.Error), 500),
		ElapsedMs:    elapsed.Milliseconds(),
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return
	}
	line, err := json.Marshal(entry)
	if err != nil {
		return
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return
	}
	_, _ = f.Write(append(line, '\n'))
	_ = f.Close()
}

func loadWorldTransitions(stateRoot string, limit int) []WorldTransition {
	path := worldTransitionsPath(stateRoot)
	if path == "" {
		return []WorldTransition{}
	}
	f, err := os.Open(path)
	if err != nil {
		return []WorldTransition{}
	}
	defer f.Close()

	var entries []WorldTransition
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 16*1024), 512*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var item WorldTransition
		if err := json.Unmarshal([]byte(line), &item); err == nil {
			entries = append(entries, item)
		}
	}
	if limit > 0 && len(entries) > limit {
		entries = entries[len(entries)-limit:]
	}
	return entries
}

// simulateActionTool predicts the blast radius, affected tests, and risk tier
// of modifying candidate files before any disk mutation occurs.
func simulateActionTool(ctx context.Context, s *scope, params map[string]any) toolResult {
	if s == nil || strings.TrimSpace(s.workspace) == "" {
		return failed(errors.New("workspace is required"))
	}
	var paths []string
	if raw, ok := params["paths"].([]any); ok {
		for _, item := range raw {
			if p, ok := item.(string); ok && strings.TrimSpace(p) != "" {
				paths = append(paths, strings.TrimSpace(p))
			}
		}
	}
	if len(paths) == 0 {
		if single := strings.TrimSpace(str(params, "path", "")); single != "" {
			paths = append(paths, single)
		}
	}
	if len(paths) == 0 {
		return failed(errors.New("paths or path is required to simulate code impact"))
	}

	var impactedFiles []string
	var affectedTests []string
	var impactSummary string
	g := loadCachedProjectGraph(s.workspace)
	if g == nil {
		g, _ = buildProjectGraph(ctx, s.workspace)
	}
	if g != nil {
		imp := g.impact(paths)
		impactedFiles = append(impactedFiles, imp.direct...)
		impactedFiles = append(impactedFiles, imp.indirect...)
		affectedTests = imp.tests
		impactSummary = imp.reportLine()
	}

	plan := planVerification(s.workspace, paths)

	risk := "low"
	fanout := len(impactedFiles)
	if fanout >= 10 || len(paths) >= 5 {
		risk = "high"
	} else if fanout >= 3 || len(paths) >= 2 {
		risk = "medium"
	}
	for _, p := range paths {
		lower := strings.ToLower(p)
		if strings.Contains(lower, "schema") || strings.Contains(lower, "agent.go") || strings.Contains(lower, "cargo.toml") || strings.Contains(lower, "go.mod") {
			if risk == "low" {
				risk = "medium"
			}
		}
	}

	cpID, _ := latestCheckpointHint(ctx)
	var empiricalRate float64 = 1.0
	var codeSamples int
	if root, err := stateRoot(); err == nil && root != "" {
		stats := summarizeWorldTransitions(loadWorldTransitions(root, 200))
		if cs, ok := stats["code"]; ok && cs.Total > 0 {
			empiricalRate = cs.SuccessRate
			codeSamples = cs.Total
			if cs.Total >= 4 && cs.SuccessRate < 0.5 && risk == "low" {
				risk = "medium"
			}
		}
	}

	return toolResult{
		Success: true,
		Data: map[string]any{
			"candidate_paths":        paths,
			"impact_summary":         impactSummary,
			"impacted_files":         impactedFiles,
			"affected_tests":         affectedTests,
			"predicted_risk":         risk,
			"fanout_count":           fanout,
			"recommended_checks":     plan.Commands,
			"plan_reason":            plan.Reason,
			"rollback_ready":         cpID != "",
			"latest_checkpoint":      cpID,
			"empirical_success_rate": empiricalRate,
			"historical_samples":     codeSamples,
		},
	}
}

type WorldDomainStats struct {
	Total       int     `json:"total"`
	Succeeded   int     `json:"succeeded"`
	Failed      int     `json:"failed"`
	SuccessRate float64 `json:"success_rate"`
	AvgReward   float64 `json:"avg_reward"`
}

func summarizeWorldTransitions(entries []WorldTransition) map[string]WorldDomainStats {
	out := map[string]WorldDomainStats{}
	rewardSum := map[string]float64{}
	for _, e := range entries {
		domain := e.Domain
		if domain == "" {
			domain = "general"
		}
		st := out[domain]
		st.Total++
		if e.OutcomeOK {
			st.Succeeded++
		} else {
			st.Failed++
		}
		rewardSum[domain] += e.RewardScore
		out[domain] = st
	}
	for domain, st := range out {
		if st.Total > 0 {
			st.SuccessRate = float64(st.Succeeded) / float64(st.Total)
			st.AvgReward = rewardSum[domain] / float64(st.Total)
			out[domain] = st
		}
	}
	return out
}

// ExportWorldDPOPairs pairs consecutive failed->succeeded transitions in the same
// domain into preference pairs for Critic / DPO training.
func ExportWorldDPOPairs(entries []WorldTransition) []map[string]any {
	var pairs []map[string]any
	lastFailedByDomain := map[string]*WorldTransition{}
	for i := range entries {
		e := entries[i]
		if !e.OutcomeOK || e.RewardScore < 0 {
			copied := e
			lastFailedByDomain[e.Domain] = &copied
			continue
		}
		if prev := lastFailedByDomain[e.Domain]; prev != nil && e.RewardScore > prev.RewardScore {
			pairs = append(pairs, map[string]any{
				"domain":        e.Domain,
				"chosen_tool":   e.ActionTool,
				"chosen_args":   e.ActionArgs,
				"rejected_tool": prev.ActionTool,
				"rejected_args": prev.ActionArgs,
				"rejected_err":  prev.ErrorSummary,
				"reward_margin": e.RewardScore - prev.RewardScore,
			})
			delete(lastFailedByDomain, e.Domain)
		}
	}
	return pairs
}
