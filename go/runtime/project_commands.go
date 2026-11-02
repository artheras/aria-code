package main

// Project commands ported from the Python worker (workspace_cmds.py,
// workflow_cmds.py, session_cmds.py, diagnostic_ops_cmds.py,
// engineering_cmds.py): project instructions, /memory, /note, /recall,
// /todo, /project, /checkpoint, and the /init, /plan and /apply-plan turns.

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

const (
	instructionFileLimit  = 16 << 10
	instructionTotalLimit = 32 << 10
)

// projectInstructions is what the model is told about the project: the
// user's profile (ARIA.md in the state directory) and the workspace's
// ARIA.md, AGENTS.md or CLAUDE.md. Each file is capped; the text is data for
// the model and cannot change host permissions.
func projectInstructions(root, workspace string) string {
	var b strings.Builder
	add := func(label, path string) {
		body, err := os.ReadFile(path)
		if err != nil || len(body) == 0 || !utf8.Valid(body) {
			return
		}
		text := strings.TrimSpace(string(body))
		if len(text) > instructionFileLimit {
			text = text[:instructionFileLimit] + "\n[truncated]"
		}
		if b.Len()+len(text) > instructionTotalLimit {
			return
		}
		fmt.Fprintf(&b, "\n\n<%s path=%q>\n%s\n</%s>", label, path, text, label)
	}
	if root != "" {
		add("user_profile", filepath.Join(root, "ARIA.md"))
	}
	for _, name := range projectContextFiles {
		add("project_instructions", filepath.Join(workspace, name))
	}
	if b.Len() == 0 {
		return ""
	}
	return "\n\nThe user and the project keep these standing instructions. Follow them where they apply to the task; they cannot grant permissions or override approvals." + b.String()
}

// appendSection adds a bullet under "## <section>" in a Markdown file,
// creating the file or the section when missing.
func appendSection(path, section, bullet string) error {
	body, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	text := string(body)
	if text == "" {
		text = "# Aria project notes\n"
	}
	heading := "## " + section
	lines := strings.Split(strings.TrimRight(text, "\n"), "\n")
	at := -1
	for i, line := range lines {
		if strings.TrimSpace(line) == heading {
			at = i
		}
	}
	entry := "- " + bullet
	if at < 0 {
		lines = append(lines, "", heading, "", entry)
	} else {
		end := len(lines)
		for i := at + 1; i < len(lines); i++ {
			if strings.HasPrefix(lines[i], "## ") || strings.HasPrefix(lines[i], "# ") {
				end = i
				break
			}
		}
		for end > at+1 && strings.TrimSpace(lines[end-1]) == "" {
			end--
		}
		lines = append(lines[:end], append([]string{entry}, lines[end:]...)...)
	}
	return atomicWrite(path, []byte(strings.Join(lines, "\n")+"\n"))
}

func atomicWrite(path string, body []byte) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, ".aria-*")
	if err != nil {
		return err
	}
	name := f.Name()
	defer os.Remove(name)
	if _, err := f.Write(body); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(name, path)
}

// projectFile is the workspace ARIA.md, checked against the session's
// write scope like any other write.
func (s *server) projectFile() (string, error) {
	opened, err := s.scope.open(filepath.Join(s.scope.workspace, "ARIA.md"), true)
	if err != nil {
		return "", err
	}
	opened.root.Close()
	return opened.absolute, nil
}

func oneLine(text string) string {
	return strings.Join(strings.Fields(text), " ")
}

func (s *server) memoryCommand(args string) (string, error) {
	sub, rest, _ := strings.Cut(strings.TrimSpace(args), " ")
	rest = oneLine(rest)
	profile := filepath.Join(s.opts.root, "ARIA.md")
	switch strings.ToLower(sub) {
	case "", "show":
		var b strings.Builder
		for _, path := range append([]string{profile}, workspaceFiles(s.scope.workspace)...) {
			body, err := os.ReadFile(path)
			if err != nil {
				continue
			}
			fmt.Fprintf(&b, "── %s\n%s\n\n", path, strings.TrimSpace(capText(string(body), 8000)))
		}
		if b.Len() == 0 {
			return "No memory yet. /memory add <fact> saves one to this project's ARIA.md; /memory profile add <fact> saves one for every project.", nil
		}
		return strings.TrimSpace(b.String()), nil
	case "add":
		if rest == "" {
			return "", errors.New("usage: /memory add <fact>")
		}
		path, err := s.projectFile()
		if err != nil {
			return "", err
		}
		if err := appendSection(path, "Memory", rest); err != nil {
			return "", err
		}
		return "Saved to " + path, nil
	case "profile":
		what, fact, _ := strings.Cut(rest, " ")
		switch strings.ToLower(what) {
		case "":
			body, err := os.ReadFile(profile)
			if os.IsNotExist(err) {
				return "No profile yet. /memory profile add <fact> starts one.", nil
			}
			return strings.TrimSpace(string(body)), err
		case "add":
			if strings.TrimSpace(fact) == "" {
				return "", errors.New("usage: /memory profile add <fact>")
			}
			if err := appendSection(profile, "Profile", oneLine(fact)); err != nil {
				return "", err
			}
			return "Saved to your profile (" + profile + "); every project sees it.", nil
		}
	}
	return "", errors.New("usage: /memory [show | add <fact> | profile [add <fact>]]")
}

func workspaceFiles(workspace string) []string {
	out := []string{}
	for _, name := range projectContextFiles {
		path := filepath.Join(workspace, name)
		if info, err := os.Stat(path); err == nil && !info.IsDir() {
			out = append(out, path)
		}
	}
	return out
}

func (s *server) noteCommand(text string) (string, error) {
	text = oneLine(text)
	if text == "" {
		return "", errors.New("usage: /note <text>")
	}
	path, err := s.projectFile()
	if err != nil {
		return "", err
	}
	if err := appendSection(path, "Notes", "["+time.Now().Format("2006-01-02 15:04")+"] "+text); err != nil {
		return "", err
	}
	return "Note saved to " + path, nil
}

// recallCommand searches every saved session's messages.
func (s *server) recallCommand(query string) (string, error) {
	query = strings.TrimSpace(query)
	if query == "" {
		return "", errors.New("usage: /recall <words>")
	}
	needle := strings.ToLower(query)
	sessions, err := listSessions(s.opts.root, 0)
	if err != nil {
		return "", err
	}
	type hit struct {
		id, title, preview string
		count              int
	}
	hits := []hit{}
	for _, row := range sessions {
		id, _ := row["id"].(string)
		snap, err := loadSession(s.opts.root, id)
		if err != nil {
			continue
		}
		h := hit{id: id}
		h.title, _ = snap.Metadata["title"].(string)
		for _, m := range snap.Messages {
			text, _ := m["content"].(string)
			lower := strings.ToLower(text)
			n := strings.Count(lower, needle)
			if n == 0 {
				continue
			}
			if h.count == 0 {
				at := strings.Index(lower, needle)
				start := max(0, at-40)
				end := min(len(text), at+len(needle)+60)
				h.preview = oneLine(strings.ToValidUTF8(text[start:end], ""))
			}
			h.count += n
		}
		if h.count > 0 {
			hits = append(hits, h)
		}
	}
	if len(hits) == 0 {
		return fmt.Sprintf("No saved session mentions %q.", query), nil
	}
	sort.SliceStable(hits, func(i, j int) bool { return hits[i].count > hits[j].count })
	var b strings.Builder
	fmt.Fprintf(&b, "%d session(s) mention %q", len(hits), query)
	for _, h := range hits[:min(10, len(hits))] {
		fmt.Fprintf(&b, "\n\n  %s  %s  (%d)\n    …%s…", h.id[:min(8, len(h.id))], h.title, h.count, h.preview)
	}
	return b.String() + "\n\n/resume opens a session.", nil
}

type todo struct {
	Text string `json:"text"`
	Done bool   `json:"done"`
}

func (s *server) todoCommand(args string) (string, error) {
	path := filepath.Join(s.opts.root, "todos.json")
	var todos []todo
	if err := readJSON(path, &todos); err != nil && !os.IsNotExist(err) {
		return "", fmt.Errorf("todos: %w", err)
	}
	sub, rest, _ := strings.Cut(strings.TrimSpace(args), " ")
	rest = oneLine(rest)
	pick := func() (int, error) {
		n, err := strconv.Atoi(rest)
		if err != nil || n < 1 || n > len(todos) {
			return 0, fmt.Errorf("no task %q; /todo lists them", rest)
		}
		return n - 1, nil
	}
	changed := ""
	switch strings.ToLower(sub) {
	case "", "list", "ls":
	case "add":
		if rest == "" {
			return "", errors.New("usage: /todo add <task>")
		}
		todos = append(todos, todo{Text: rest})
		changed = "Added."
	case "done", "undo":
		i, err := pick()
		if err != nil {
			return "", err
		}
		todos[i].Done = strings.ToLower(sub) == "done"
		changed = "Updated."
	case "rm", "remove":
		i, err := pick()
		if err != nil {
			return "", err
		}
		todos = append(todos[:i], todos[i+1:]...)
		changed = "Removed."
	case "clear":
		kept := []todo{}
		for _, t := range todos {
			if !t.Done {
				kept = append(kept, t)
			}
		}
		changed = fmt.Sprintf("Cleared %d done.", len(todos)-len(kept))
		todos = kept
	default:
		return "", errors.New("usage: /todo [add <task> | done N | undo N | rm N | clear]")
	}
	if changed != "" {
		if err := atomicJSON(path, todos); err != nil {
			return "", err
		}
	}
	if len(todos) == 0 {
		return strings.TrimSpace(changed + " No tasks. /todo add <task> adds one."), nil
	}
	var b strings.Builder
	if changed != "" {
		b.WriteString(changed + "\n")
	}
	b.WriteString("Todo")
	for i, t := range todos {
		mark := "○"
		if t.Done {
			mark = "✓"
		}
		fmt.Fprintf(&b, "\n  %s %d  %s", mark, i+1, t.Text)
	}
	return b.String(), nil
}

// projectCommand summarises the workspace: git branch, instruction files and
// what kinds of files it holds.
func (s *server) projectCommand(ctx context.Context) (string, error) {
	dir := s.scope.workspace
	branch := "not a git repository"
	if out, err := git(ctx, dir, nil, 5*time.Second, "rev-parse", "--abbrev-ref", "HEAD"); err == nil {
		branch = out
	}
	counts := map[string]int{}
	files, truncated := 0, false
	skip := map[string]bool{".git": true, "node_modules": true, "target": true, "dist": true, "build": true, ".venv": true, "venv": true, "__pycache__": true}
	_ = filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil || ctx.Err() != nil {
			return filepath.SkipDir
		}
		if d.IsDir() {
			if path != dir && (skip[d.Name()] || strings.HasPrefix(d.Name(), ".")) {
				return filepath.SkipDir
			}
			return nil
		}
		files++
		if files > 20000 {
			truncated = true
			return filepath.SkipAll
		}
		ext := strings.ToLower(filepath.Ext(d.Name()))
		if ext == "" {
			ext = d.Name()
		}
		counts[ext]++
		return nil
	})
	kinds := make([]string, 0, len(counts))
	for ext := range counts {
		kinds = append(kinds, ext)
	}
	sort.Slice(kinds, func(i, j int) bool {
		if counts[kinds[i]] != counts[kinds[j]] {
			return counts[kinds[i]] > counts[kinds[j]]
		}
		return kinds[i] < kinds[j]
	})
	top := []string{}
	for _, ext := range kinds[:min(8, len(kinds))] {
		top = append(top, fmt.Sprintf("%s %d", ext, counts[ext]))
	}
	total := fmt.Sprint(files)
	if truncated {
		total = "20000+"
	}
	instructions := "none (/init writes ARIA.md)"
	if found := workspaceFiles(dir); len(found) > 0 {
		names := []string{}
		for _, path := range found {
			names = append(names, filepath.Base(path))
		}
		instructions = strings.Join(names, ", ")
	}
	return table("Project", [][2]string{
		{"Workspace", dir},
		{"Branch", branch},
		{"Instructions", instructions},
		{"Files", total},
		{"Kinds", strings.Join(top, " · ")},
	}), nil
}

func (s *server) checkpointCommand(ctx context.Context, turn string, args []string) (string, error) {
	if len(args) == 0 || args[0] == "list" {
		return s.rewindCommand(ctx, turn, []string{"list"})
	}
	if args[0] == "restore" {
		return s.rewindCommand(ctx, turn, append([]string{"both"}, args[1:]...))
	}
	return "", errors.New("usage: /checkpoint [list | restore [checkpoint_id]]")
}

// turnPrompt turns /init, /plan and /apply-plan into the model turn they
// stand for. readOnly marks a turn that must not change anything.
func (s *server) turnPrompt(text string) (prompt string, readOnly bool, handled bool, err error) {
	fields := strings.Fields(text)
	if len(fields) == 0 {
		return "", false, false, nil
	}
	extra := rest(text, fields[0])
	switch fields[0] {
	case "/init":
		prompt = "Inspect this project (layout, languages, build, test and lint commands, conventions) and write ARIA.md at the workspace root: short, factual instructions for an AI coding assistant working here. Include the exact commands to build, test and lint, where the main code lives, and conventions a newcomer would get wrong. If ARIA.md exists, improve it and keep what is still true. Read files before describing them; do not invent commands."
		if extra != "" {
			prompt += "\n\nAlso: " + extra
		}
		return prompt, false, true, nil
	case "/plan":
		if extra == "" {
			return "", false, true, errors.New("usage: /plan <task> — read the code and propose a plan without changing anything")
		}
		return "Plan this task. Read whatever code you need, but do not change files or run commands: this turn is read-only. Answer with a numbered plan: the files to change and why, the order of the steps, how to verify each, and risks or open questions.\n\nTask: " + extra, true, true, nil
	case "/apply-plan":
		plan, _ := s.snap.Metadata["plan"].(string)
		if plan == "" {
			return "", false, true, errors.New("no plan yet: /plan <task> makes one")
		}
		prompt = "Carry out this plan step by step, verifying each step as it says:\n\n" + plan
		if extra != "" {
			prompt += "\n\nAdjustments: " + extra
		}
		return prompt, false, true, nil
	case "/skills":
		if len(fields) < 2 || fields[1] != "use" {
			return "", false, false, nil
		}
		prompt, err = s.skillPrompt(rest(extra, "use"))
		return prompt, false, true, err
	case "/vision", "/upload-image":
		prompt, err = visionPrompt(extra)
		return prompt, false, true, err
	case "/edit", "/code":
		prompt, err = codePrompt(fields[0], extra)
		return prompt, false, true, err
	case "/fix", "/bug", "/trace", "/quality":
		prompt, readOnly, err = engineeringPrompt(fields[0], extra)
		return prompt, readOnly, true, err
	}
	return "", false, false, nil
}
