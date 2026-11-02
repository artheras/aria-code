package main

// Code-tool commands ported from the Python worker (file_cmds.py,
// core_cmds.py, engineering_cmds.py): /search, /read, /ls, /write, /run,
// /build, and the /edit and /code turns. They call the same tools the model
// uses, under the same scope checks and approvals.

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// readTool runs one of the model's read tools for the user.
func (s *server) readTool(ctx context.Context, name string, params map[string]any) (map[string]any, error) {
	allowed, reason, err := s.approve(ctx, name, params, fileChange{})
	if err != nil {
		return nil, err
	}
	if !allowed {
		return nil, errors.New("access declined: " + reason)
	}
	var result toolResult
	switch name {
	case "read_file":
		result = fileRead(ctx, s.scope, params)
	case "list_files":
		result = fileList(ctx, s.scope, params)
	case "search_code":
		result = searchCode(ctx, s.scope, params)
	}
	if !result.Success {
		return nil, errors.New(result.Error)
	}
	return result.Data, nil
}

func (s *server) searchCommand(ctx context.Context, args []string) (string, error) {
	if len(args) == 0 || len(args) > 2 {
		return "", errors.New("usage: /search <regex> [glob] — e.g. /search \"func main\" **/*.go")
	}
	glob := "**/*"
	if len(args) == 2 {
		glob = args[1]
	}
	data, err := s.readTool(ctx, "search_code", map[string]any{"pattern": args[0], "glob": glob, "path": s.scope.workspace})
	if err != nil {
		return "", err
	}
	matches, _ := data["matches"].([]map[string]any)
	if len(matches) == 0 {
		return fmt.Sprintf("No matches for %q.", args[0]), nil
	}
	var b strings.Builder
	fmt.Fprintf(&b, "%d match(es) for %q", len(matches), args[0])
	for _, m := range matches {
		fmt.Fprintf(&b, "\n  %v:%v  %v", m["file"], m["line"], m["content"])
	}
	if len(matches) >= 50 {
		b.WriteString("\n  … first 50 shown; narrow the pattern or the glob")
	}
	return b.String(), nil
}

func (s *server) readCommand(ctx context.Context, args []string) (string, error) {
	if len(args) == 0 || len(args) > 3 {
		return "", errors.New("usage: /read <path> [first line] [line count]")
	}
	params := map[string]any{"path": args[0]}
	for i, key := range []string{"offset", "limit"} {
		if len(args) > i+1 {
			n, err := strconv.Atoi(args[i+1])
			if err != nil || n < 0 {
				return "", errors.New("usage: /read <path> [first line] [line count]")
			}
			if key == "offset" && n > 0 {
				n-- // the user counts lines from 1
			}
			params[key] = float64(n)
		}
	}
	data, err := s.readTool(ctx, "read_file", params)
	if err != nil {
		return "", err
	}
	content, _ := data["content"].(string)
	if content == "" {
		return fmt.Sprintf("%v is empty.", data["path"]), nil
	}
	return fmt.Sprintf("%v\n\n%s", data["path"], content), nil
}

func (s *server) lsCommand(ctx context.Context, args []string) (string, error) {
	if len(args) > 2 {
		return "", errors.New("usage: /ls [path] [pattern]")
	}
	params := map[string]any{"path": s.scope.workspace}
	if len(args) > 0 {
		params["path"] = args[0]
	}
	if len(args) > 1 {
		params["pattern"] = args[1]
	}
	data, err := s.readTool(ctx, "list_files", params)
	if err != nil {
		return "", err
	}
	items, _ := data["items"].([]map[string]any)
	var b strings.Builder
	fmt.Fprintf(&b, "%v", data["path"])
	if len(items) == 0 {
		b.WriteString("\n  (empty)")
	}
	for _, item := range items {
		if item["type"] == "dir" {
			fmt.Fprintf(&b, "\n  %v%c", item["name"], filepath.Separator)
		} else {
			fmt.Fprintf(&b, "\n  %-48v %v", item["name"], humanBytes(item["size"]))
		}
	}
	return b.String(), nil
}

func humanBytes(value any) string {
	n, _ := value.(int64)
	switch {
	case n >= 1<<20:
		return fmt.Sprintf("%.1f MB", float64(n)/(1<<20))
	case n >= 1<<10:
		return fmt.Sprintf("%.1f KB", float64(n)/(1<<10))
	}
	return fmt.Sprintf("%d B", n)
}

// writeCommand writes the lines after "/write <path>" to the file, through
// the write tool's preview, approval and checkpoint. --stage keeps it as a
// pending change instead.
func (s *server) writeCommand(ctx context.Context, turn, text string) (string, error) {
	head, body, _ := strings.Cut(strings.TrimSpace(text), "\n")
	words := strings.Fields(head)[1:]
	stage := false
	if len(words) > 0 && words[0] == "--stage" {
		stage, words = true, words[1:]
	}
	if len(words) != 1 || body == "" {
		return "", errors.New("usage: /write [--stage] <path>, then the content on the following lines")
	}
	params := map[string]any{"path": words[0], "content": strings.TrimRight(body, "\n") + "\n"}
	change, err := propose(s.scope, "write_file", params)
	if err != nil {
		return "", err
	}
	allowed, reason, err := s.approve(ctx, "write_file", params, change)
	if err != nil {
		return "", err
	}
	if !allowed {
		return "Not written: " + reason, nil
	}
	if stage {
		result := stageRecordedChange(ctx, s.scope, s.opts.root, s.snap.ID, "write_file", change)
		if !result.Success {
			return "", errors.New(result.Error)
		}
		return fmt.Sprintf("Staged change %v. /changes reviews it; /apply-change %v applies it.", result.Data["change_id"], result.Data["change_id"]), nil
	}
	result := applyRecordedChange(ctx, s.scope, s.opts.root, s.snap.ID, "write_file", change)
	if !result.Success {
		return "", errors.New(result.Error)
	}
	return fmt.Sprintf("Wrote %v (%v bytes). /rewind can undo it.", result.Data["path"], result.Data["bytes"]), nil
}

// buildCommands is the project's build: build_command from /config, or
// what its manifest implies.
func buildCommands(cfg config, root string) []string {
	if command := strings.TrimSpace(cfg.text("build_command", "")); command != "" {
		return []string{command}
	}
	has := func(name string) bool {
		_, err := os.Stat(filepath.Join(root, name))
		return err == nil
	}
	switch {
	case has("Cargo.toml"):
		return []string{"cargo build"}
	case has("go.mod"):
		return []string{"go build ./..."}
	case has("package.json"):
		if _, ok := packageScripts(root)["build"]; ok {
			return []string{"npm run build"}
		}
		if has("tsconfig.json") {
			return []string{"npx tsc --noEmit"}
		}
	case has("Makefile"):
		return []string{"make"}
	case has("pyproject.toml"):
		return []string{"python3 -m compileall -q ."}
	}
	return nil
}

func (s *server) buildCommand(ctx context.Context, turn string) (string, error) {
	commands := buildCommands(s.cfg, s.scope.workspace)
	if len(commands) == 0 {
		return "No build found. /config set build_command=<command> sets one.", nil
	}
	return s.userCommand(ctx, turn, commands[0])
}

// codePrompt turns /edit and /code into model turns.
func codePrompt(name, extra string) (string, error) {
	switch name {
	case "/edit":
		path, instruction, _ := strings.Cut(extra, " ")
		if path == "" || strings.TrimSpace(instruction) == "" {
			return "", errors.New("usage: /edit <path> <what to change>")
		}
		return fmt.Sprintf("Edit %s: %s\n\nRead the file first, change only what this asks, and keep the rest as it is.", path, strings.TrimSpace(instruction)), nil
	case "/code":
		if extra == "" {
			return "", errors.New("usage: /code <what to write> [--save path]")
		}
		description, save, _ := strings.Cut(extra, "--save")
		prompt := "Write code for: " + strings.TrimSpace(description) + "\n\nFollow the project's language and conventions; read related files first if this belongs in the project."
		if path := strings.TrimSpace(save); path != "" {
			prompt += " Save it to " + path + "."
		} else {
			prompt += " Show the code in the answer; do not write files unless asked."
		}
		return prompt, nil
	}
	return "", nil
}
