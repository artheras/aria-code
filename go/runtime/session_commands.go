package main

// Session commands ported from the Python worker (session_ux_cmds.py,
// diagnostic_cmds.py, ui_cmds.py, model_cmds.py, engineering_cmds.py) so the
// native runtime answers them without Python.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Files read as project instructions, in the order a reader should check.
var projectContextFiles = []string{"ARIA.md", "AGENTS.md", "CLAUDE.md", ".cursorrules"}

// clearConversation forgets the conversation but keeps the session, its
// settings and its checkpoints: the next turn starts with no history.
func (s *server) clearConversation() (string, error) {
	removed := len(s.snap.Messages)
	s.snap.Messages = []message{}
	s.contents = nil
	s.historyReset = true
	if removed == 0 {
		return "Conversation is already empty.", nil
	}
	return fmt.Sprintf("Conversation cleared (%d messages).", removed), nil
}

func (s *server) permissionMode() string {
	if s.scope.readOnly {
		return "read-only"
	}
	return "workspace-write"
}

func (s *server) statusCommand() (string, error) {
	autoLevel, ok := pyInt(s.cfg["auto_approve_level"])
	if !ok {
		autoLevel = 1
	}
	network := "on"
	if !s.cfg.boolean("network_enabled", true) {
		network = "off"
	}
	rows := [][2]string{
		{"Version", version},
		{"Provider", providerName(s.cfg)},
		{"Model", s.cfg.text("model", "gemini-3.8-flash")},
		{"Session", s.snap.ID},
		{"Workspace", s.scope.workspace},
		{"Permissions", s.permissionMode()},
		{"Approval", fmt.Sprintf("%s (auto level %d)", s.cfg.text("approval_mode", "manual"), autoLevel)},
		{"Network", network},
		{"Messages", fmt.Sprint(len(s.snap.Messages))},
		{"Tools", fmt.Sprint(len(fileDefinitions))},
	}
	return table("Status", rows), nil
}

// estimateTokens is a rough count (about four characters a token) over
// everything the model is sent again on the next turn, tool results included.
func estimateTokens(contents any) int {
	body, err := json.Marshal(contents)
	if err != nil {
		return 0
	}
	return len(body) / 4
}

func (s *server) contextCommand() (string, error) {
	tokens := estimateTokens(s.contents)
	calls := 0
	for _, content := range s.contents {
		for _, part := range content.Parts {
			if part.FunctionCall != nil {
				calls++
			}
		}
	}
	project := []string{}
	for _, name := range projectContextFiles {
		if info, err := os.Stat(filepath.Join(s.scope.workspace, name)); err == nil && !info.IsDir() {
			project = append(project, name)
		}
	}
	found := "none"
	if len(project) > 0 {
		found = strings.Join(project, ", ")
	}
	rows := [][2]string{
		{"Model", s.cfg.text("model", "gemini-3.8-flash")},
		{"Messages", fmt.Sprint(len(s.snap.Messages))},
		{"Provider turns", fmt.Sprint(len(s.contents))},
		{"Tool calls", fmt.Sprint(calls)},
		{"Est. tokens", fmt.Sprintf("~%d", tokens)},
		{"Project files", found},
	}
	return table("Context", rows) + "\n\n/clear forgets the conversation · /undo removes the last exchange", nil
}

func (s *server) toolsCommand(ctx context.Context) (string, error) {
	var b strings.Builder
	b.WriteString("Tools\n")
	for _, d := range fileDefinitions {
		policy, err := persistentToolPolicy(s.opts.root, d.name)
		if err != nil {
			return "", err
		}
		access := "read"
		if d.mutate {
			access = "write"
		}
		switch {
		case policy == "deny":
			policy = "denied"
		case policy == "ask":
			policy = "always asks"
		case policy == "allow" || s.opts.skipPermissions || contains(s.opts.allowedTools, d.name) || s.grants[d.name]:
			policy = "allowed"
		case d.mutate:
			policy = "asks"
		default:
			policy = "allowed"
		}
		fmt.Fprintf(&b, "\n  %-12s %-5s  %-11s  %s", d.name, access, policy, firstSentence(d.description))
	}
	if s.mcp != nil {
		s.mcp.ensure(ctx)
		if len(s.mcp.declarations()) > 0 {
			b.WriteString("\n\n" + s.mcp.toolList())
		}
	}
	return b.String(), nil
}

// diffCommand shows what changed in the workspace: a summary by default,
// the patch with --patch, or the patch for the given paths. It only reads.
func (s *server) diffCommand(ctx context.Context, args []string) (string, error) {
	dir := s.scope.workspace
	if _, err := git(ctx, dir, nil, 10*time.Second, "rev-parse", "--is-inside-work-tree"); err != nil {
		return "", errors.New("not a git repository: /diff needs git")
	}
	const limit = 60000
	patch := len(args) > 0
	paths := args
	if len(args) > 0 && (args[0] == "--patch" || args[0] == "-p") {
		paths = args[1:]
	}
	for _, path := range paths {
		if strings.HasPrefix(path, "-") {
			return "", errors.New("usage: /diff [--patch] [path ...]")
		}
	}
	if !patch {
		status, err := git(ctx, dir, nil, 30*time.Second, "status", "--short")
		if err != nil {
			return "", err
		}
		if status == "" {
			return "No changes.", nil
		}
		stat, err := git(ctx, dir, nil, 30*time.Second, "diff", "HEAD", "--stat")
		if err != nil {
			// A repository with no commits yet has no HEAD to compare with.
			stat = ""
		}
		out := status
		if stat != "" {
			out += "\n\n" + stat
		}
		return capText(out+"\n\n/diff --patch shows the patch", limit), nil
	}
	gitArgs := append([]string{"diff", "HEAD", "--"}, paths...)
	out, err := git(ctx, dir, nil, 30*time.Second, gitArgs...)
	if err != nil {
		out, err = git(ctx, dir, nil, 30*time.Second, append([]string{"diff", "--"}, paths...)...)
		if err != nil {
			return "", err
		}
	}
	if out == "" {
		return "No changes.", nil
	}
	return capText(out, limit), nil
}

func table(title string, rows [][2]string) string {
	var b strings.Builder
	b.WriteString(title)
	b.WriteString("\n")
	for _, row := range rows {
		fmt.Fprintf(&b, "\n  %-14s %s", row[0], row[1])
	}
	return b.String()
}

func firstSentence(text string) string {
	if i := strings.Index(text, ". "); i >= 0 {
		return text[:i+1]
	}
	return text
}
