package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

const gitOutputLimit = 16000

func gitSafeRelPath(workspace, rawPath string) (string, error) {
	trimmed := strings.TrimSpace(rawPath)
	if trimmed == "" || trimmed == "." {
		return ".", nil
	}
	absRoot, err := filepath.Abs(workspace)
	if err != nil {
		return "", err
	}
	candidate := trimmed
	if !filepath.IsAbs(candidate) {
		candidate = filepath.Join(absRoot, candidate)
	}
	absTarget, err := filepath.Abs(candidate)
	if err != nil {
		return "", err
	}
	rel, err := filepath.Rel(absRoot, absTarget)
	if err != nil {
		return "", err
	}
	if rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("path %q escapes workspace", rawPath)
	}
	return rel, nil
}

func runGitReadOnly(ctx context.Context, workspace string, args ...string) (string, error) {
	if strings.TrimSpace(workspace) == "" {
		return "", errors.New("workspace is required")
	}
	runCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()

	fullArgs := append([]string{"--no-pager"}, args...)
	cmd := exec.CommandContext(runCtx, "git", fullArgs...)
	cmd.Dir = workspace
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		errText := strings.TrimSpace(stderr.String())
		if errText == "" {
			errText = err.Error()
		}
		return "", errors.New(errText)
	}
	return stdout.String(), nil
}

func gitStatusTool(ctx context.Context, s *scope, params map[string]any) toolResult {
	args := []string{"status", "--short", "--branch"}
	if rawPath := str(params, "path", ""); rawPath != "" {
		rel, err := gitSafeRelPath(s.workspace, rawPath)
		if err != nil {
			return failed(err)
		}
		if rel != "." {
			args = append(args, "--", rel)
		}
	}
	out, err := runGitReadOnly(ctx, s.workspace, args...)
	if err != nil {
		return failed(err)
	}
	lines := strings.Split(strings.TrimRight(out, "\r\n"), "\n")
	changedCount := 0
	for _, line := range lines {
		if strings.TrimSpace(line) != "" && !strings.HasPrefix(line, "## ") {
			changedCount++
		}
	}
	return toolResult{
		Success: true,
		Data: map[string]any{
			"output":        capText(out, gitOutputLimit),
			"changed_count": changedCount,
		},
	}
}

func gitDiffTool(ctx context.Context, s *scope, params map[string]any) toolResult {
	staged, _ := params["staged"].(bool)
	statOnly, _ := params["stat"].(bool)

	args := []string{"diff"}
	if staged {
		args = append(args, "--staged")
	}
	if statOnly {
		args = append(args, "--stat")
	} else {
		args = append(args, "--unified=3")
	}
	if base := strings.TrimSpace(str(params, "base", "")); base != "" {
		if strings.HasPrefix(base, "-") {
			return failed(errors.New("invalid git base ref"))
		}
		args = append(args, base)
	}
	if rawPath := str(params, "path", ""); rawPath != "" {
		rel, err := gitSafeRelPath(s.workspace, rawPath)
		if err != nil {
			return failed(err)
		}
		if rel != "." {
			args = append(args, "--", rel)
		}
	}
	out, err := runGitReadOnly(ctx, s.workspace, args...)
	if err != nil {
		return failed(err)
	}
	if strings.TrimSpace(out) == "" {
		out = "(no differences)"
	}
	return toolResult{
		Success: true,
		Data: map[string]any{
			"output": capText(out, gitOutputLimit),
			"staged": staged,
		},
	}
}

func gitLogTool(ctx context.Context, s *scope, params map[string]any) toolResult {
	limit := 10
	if n, ok := pyInt(params["limit"]); ok && n > 0 {
		limit = n
	}
	if limit > 50 {
		limit = 50
	}
	args := []string{"log", fmt.Sprintf("-n%d", limit), "--oneline", "--decorate"}
	if rawPath := str(params, "path", ""); rawPath != "" {
		rel, err := gitSafeRelPath(s.workspace, rawPath)
		if err != nil {
			return failed(err)
		}
		if rel != "." {
			args = append(args, "--", rel)
		}
	}
	out, err := runGitReadOnly(ctx, s.workspace, args...)
	if err != nil {
		return failed(err)
	}
	return toolResult{
		Success: true,
		Data: map[string]any{
			"output": capText(out, gitOutputLimit),
			"limit":  limit,
		},
	}
}
