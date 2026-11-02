package main

// Lifecycle hooks, ported from src/aria_code/apps/cli/hooks.py and read
// from the same files: <state>/hooks.json and the project's
// .aria/hooks.json.
//
//	{"PreToolUse":   [{"tool": "write_file", "command": "./check.sh", "blocking": true}],
//	 "PostToolUse":  [{"command": "echo $ARIA_TOOL >> log"}],
//	 "ResponseDone": [{"command": "notify-send done"}],
//	 "SessionStart": [{"command": "echo $ARIA_SESSION"}]}
//
// A hook runs in the workspace with ARIA_EVENT, ARIA_TOOL,
// ARIA_TOOL_PARAMS, ARIA_RESULT, ARIA_RESPONSE and ARIA_SESSION set, for at
// most timeout seconds (default 10). A blocking PreToolUse hook that exits
// non-zero stops the call, and the model is told why.
//
// The Python worker ran a project's hooks as soon as it opened the project.
// Here they are the user's own when they come from the state directory; a
// project's file runs only after the user has approved its exact contents
// (/hooks trust), like a workflow's first run.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

var hookEvents = []string{"PreToolUse", "PostToolUse", "ResponseDone", "SessionStart", "SessionEnd"}

type hookEntry struct {
	Command  string  `json:"command"`
	Tool     string  `json:"tool"`
	Timeout  float64 `json:"timeout"`
	Blocking bool    `json:"blocking"`
	source   string
}

type hookFile struct {
	path, digest string
	entries      map[string][]hookEntry
	err          error
}

func readHookFile(path, source string) *hookFile {
	body, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return &hookFile{path: path, err: err}
	}
	var raw map[string][]hookEntry
	if err := json.Unmarshal(body, &raw); err != nil {
		return &hookFile{path: path, err: err}
	}
	sum := sha256.Sum256(body)
	f := &hookFile{path: path, digest: hex.EncodeToString(sum[:]), entries: map[string][]hookEntry{}}
	for _, event := range hookEvents {
		for _, e := range raw[event] {
			if strings.TrimSpace(e.Command) != "" {
				e.source = source
				f.entries[event] = append(f.entries[event], e)
			}
		}
	}
	return f
}

func (s *server) hookFiles() (user, project *hookFile) {
	return readHookFile(filepath.Join(s.opts.root, "hooks.json"), "user"),
		readHookFile(filepath.Join(s.scope.workspace, ".aria", "hooks.json"), "project")
}

func (s *server) trustedHooks() map[string]string {
	trusted := map[string]string{}
	readJSON(filepath.Join(s.opts.root, "trusted_hooks.json"), &trusted)
	return trusted
}

// activeHooks are the entries that may run for event and tool.
func (s *server) activeHooks(event, tool string) []hookEntry {
	user, project := s.hookFiles()
	var out []hookEntry
	for _, f := range []*hookFile{user, project} {
		if f == nil || f.err != nil {
			continue
		}
		if f == project && s.trustedHooks()[f.path] != f.digest {
			continue
		}
		for _, e := range f.entries[event] {
			if e.Tool == "" || e.Tool == "*" || e.Tool == tool {
				out = append(out, e)
			}
		}
	}
	return out
}

// fireHooks runs event's hooks; it reports false, with the reason, when a
// blocking PreToolUse hook vetoes the call.
func (s *server) fireHooks(ctx context.Context, event, tool string, params map[string]any, result *toolResult, response string) (bool, string) {
	if remoteScope() {
		return true, ""
	}
	entries := s.activeHooks(event, tool)
	if len(entries) == 0 {
		return true, ""
	}
	env := append(os.Environ(), "ARIA_EVENT="+event, "ARIA_SESSION="+s.snap.ID)
	if tool != "" {
		body, _ := json.Marshal(params)
		env = append(env, "ARIA_TOOL="+tool, "ARIA_TOOL_PARAMS="+capText(string(body), 32000))
	}
	if result != nil {
		body, _ := json.Marshal(result)
		env = append(env, "ARIA_RESULT="+capText(string(body), 32000))
	}
	if response != "" {
		env = append(env, "ARIA_RESPONSE="+capText(response, 500))
	}
	for _, e := range entries {
		timeout := 10 * time.Second
		if e.Timeout > 0 {
			timeout = time.Duration(e.Timeout * float64(time.Second))
		}
		bounded, cancel := context.WithTimeout(ctx, timeout)
		cmd := shellCommand(bounded, e.Command)
		cmd.Dir, cmd.Env, cmd.WaitDelay = s.scope.workspace, env, time.Second
		stderr := &outputTail{limit: 2000}
		cmd.Stdout, cmd.Stderr = &outputTail{limit: 2000}, stderr
		err := cmd.Run()
		timedOut := bounded.Err() != nil
		cancel()
		if event == "PreToolUse" && e.Blocking && (err != nil || timedOut) {
			reason, _ := stderr.text()
			reason = strings.TrimSpace(reason)
			if timedOut {
				reason = "timed out after " + timeout.String()
			} else if reason == "" {
				reason = err.Error()
			}
			return false, fmt.Sprintf("blocked by %s hook `%s`: %s", e.source, capText(e.Command, 120), capText(reason, 400))
		}
	}
	return true, ""
}

func (f *hookFile) describe() string {
	var b strings.Builder
	for _, event := range hookEvents {
		for _, e := range f.entries[event] {
			tool := ""
			if e.Tool != "" && e.Tool != "*" {
				tool = "[" + e.Tool + "] "
			}
			blocking := ""
			if e.Blocking && event == "PreToolUse" {
				blocking = "  (blocking)"
			}
			fmt.Fprintf(&b, "\n    %-13s %s%s%s", event, tool, capText(e.Command, 120), blocking)
		}
	}
	return b.String()
}

func (s *server) hooksCommand(ctx context.Context, args []string) (string, error) {
	user, project := s.hookFiles()
	sub := "list"
	if len(args) > 0 {
		sub = args[0]
	}
	switch sub {
	case "list":
		if user == nil && project == nil {
			return "No hooks. Put them in " + filepath.Join(s.opts.root, "hooks.json") + " or the project's .aria/hooks.json:\n" +
				`{"PreToolUse": [{"tool": "write_file", "command": "./check.sh", "blocking": true}]}`, nil
		}
		var b strings.Builder
		b.WriteString("Hooks")
		for _, f := range []*hookFile{user, project} {
			if f == nil {
				continue
			}
			label := "yours"
			if f == project {
				label = "project, not trusted: /hooks trust shows and approves them"
				if s.trustedHooks()[f.path] == f.digest {
					label = "project, trusted"
				}
			}
			if f.err != nil {
				fmt.Fprintf(&b, "\n  ✗ %s: %v", f.path, f.err)
				continue
			}
			fmt.Fprintf(&b, "\n  %s (%s)%s", f.path, label, f.describe())
		}
		return b.String(), nil
	case "trust":
		if project == nil || project.err != nil {
			return "", errors.New("this project has no readable .aria/hooks.json")
		}
		if s.trustedHooks()[project.path] == project.digest {
			return "Already trusted.", nil
		}
		allowed, reason, err := s.approve(ctx, "run_workflow", map[string]any{"preview": "Run these project hooks on this machine?\n" + project.path + project.describe()}, fileChange{})
		if err != nil {
			return "", err
		}
		if !allowed {
			return "Not trusted: " + reason, nil
		}
		trusted := s.trustedHooks()
		trusted[project.path] = project.digest
		if err := atomicJSON(filepath.Join(s.opts.root, "trusted_hooks.json"), trusted); err != nil {
			return "", err
		}
		return "Trusted. They run until the file changes.", nil
	}
	return "", errors.New("usage: /hooks [list | trust]")
}

// hookCounts is used by /doctor.
func (s *server) hookCounts() (int, int, bool) {
	user, project := s.hookFiles()
	count := func(f *hookFile) int {
		n := 0
		if f != nil {
			keys := make([]string, 0, len(f.entries))
			for k := range f.entries {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for _, k := range keys {
				n += len(f.entries[k])
			}
		}
		return n
	}
	untrusted := project != nil && project.err == nil && s.trustedHooks()[project.path] != project.digest
	return count(user), count(project), untrusted
}
