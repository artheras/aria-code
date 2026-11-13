package main

import (
	"context"
	"errors"
	"strings"
)

type subagentDepthCtxKey struct{}

const maxSubagentDepth = 1

func subagentDepth(ctx context.Context) int {
	if v, ok := ctx.Value(subagentDepthCtxKey{}).(int); ok {
		return v
	}
	return 0
}

func spawnSubagentTool(ctx context.Context, p modelProvider, router *roleRouter, s *scope, c config, params map[string]any) toolResult {
	task := strings.TrimSpace(str(params, "task", ""))
	if task == "" {
		return failed(errors.New("task is required"))
	}
	if subagentDepth(ctx) >= maxSubagentDepth {
		return failed(errors.New("subagent recursion limit reached; perform remaining inspection directly"))
	}
	if p == nil {
		return failed(errors.New("model provider is required for subagent"))
	}

	subProvider := p
	if router != nil {
		if rp := router.ProviderForRole(RoleResearch); rp != nil {
			subProvider = rp
		}
	}

	subScope := &scope{}
	if s != nil {
		*subScope = *s
	}
	subScope.readOnly = true

	subCfg := config{}
	for k, v := range c {
		subCfg[k] = v
	}
	maxRounds := 10
	if n, ok := pyInt(params["max_rounds"]); ok && n > 0 && n <= 12 {
		maxRounds = n
	}
	subCfg["max_tool_rounds"] = maxRounds
	subCfg["auto_compact_context"] = true
	subCfg["review_gate"] = false

	subHooks := agentHooks{
		emit: func(string, map[string]any) {},
		approve: func(context.Context, string, map[string]any, fileChange) (bool, string, error) {
			return true, "read-only subagent", nil
		},
		evidence: newTurnEvidence(subScope.workspace),
	}

	prompt := "You are a read-only research subagent with a fresh context. Investigate the task thoroughly using read/search/git tools and return a concise, factual report with exact file paths and line numbers:\n\n" + task
	subCtx := context.WithValue(ctx, subagentDepthCtxKey{}, subagentDepth(ctx)+1)
	_, answer, err := nativeTurn(subCtx, subProvider, subScope, subCfg, nil, prompt, subHooks)
	if err != nil {
		return failed(err)
	}
	return toolResult{
		Success: true,
		Data: map[string]any{
			"report": answer,
			"role":   string(RoleResearch),
		},
	}
}
