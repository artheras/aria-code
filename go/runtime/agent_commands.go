package main

// Agent commands ported from the Python worker (core_cmds.py,
// engineering_cmds.py, workflow_cmds.py, runtime/workflows.py,
// runtime/consult.py): /task and /worktree (a coding task in its own git
// worktree), /tasks, /delegate (a fresh-context subagent), /agents,
// /workflow and /wf, and /consult.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"go.yaml.in/yaml/v3"
	"google.golang.org/genai"
)

// ── /task: a coding task in its own worktree ───────────────────────────────

type taskRecord struct {
	ID      string `json:"id"`
	Goal    string `json:"goal"`
	Path    string `json:"path"`
	Branch  string `json:"branch"`
	Repo    string `json:"repo"`
	Created string `json:"created"`
	Answer  string `json:"answer,omitempty"`
}

func (s *server) tasksFile() string { return filepath.Join(s.opts.root, "tasks.json") }

func (s *server) loadTasks() ([]taskRecord, error) {
	var tasks []taskRecord
	if err := readJSON(s.tasksFile(), &tasks); err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("tasks: %w", err)
	}
	return tasks, nil
}

// findTask is the task with this ID (or ID prefix), or the newest for this
// workspace's repository when ref is empty.
func (s *server) findTask(ctx context.Context, ref string) (taskRecord, []taskRecord, error) {
	tasks, err := s.loadTasks()
	if err != nil {
		return taskRecord{}, nil, err
	}
	repo, _ := git(ctx, s.scope.workspace, nil, 5*time.Second, "rev-parse", "--show-toplevel")
	for i := len(tasks) - 1; i >= 0; i-- {
		t := tasks[i]
		if (ref == "" && t.Repo == repo) || (ref != "" && strings.HasPrefix(t.ID, ref)) {
			return t, tasks, nil
		}
	}
	if ref == "" {
		return taskRecord{}, tasks, errors.New("no task for this repository: /task <goal> starts one")
	}
	return taskRecord{}, tasks, fmt.Errorf("no task %q; /tasks lists them", ref)
}

func (s *server) taskCommand(ctx context.Context, turn, text string) (string, error) {
	args := strings.Fields(text)[1:]
	if len(args) == 0 {
		return s.tasksCommand(ctx)
	}
	ref := ""
	if len(args) == 2 {
		ref = args[1]
	}
	switch args[0] {
	case "status", "diff", "apply", "discard":
		if len(args) > 2 {
			break
		}
		task, tasks, err := s.findTask(ctx, ref)
		if err != nil {
			return "", err
		}
		switch args[0] {
		case "status":
			stat, err := s.taskDiff(ctx, task, true)
			if err != nil {
				return "", err
			}
			return fmt.Sprintf("Task %s: %s\nWorktree %s (branch %s)\n\n%s", task.ID, task.Goal, task.Path, task.Branch, stat), nil
		case "diff":
			return s.taskDiff(ctx, task, false)
		case "apply":
			return s.applyTask(ctx, turn, task)
		case "discard":
			return s.discardTask(ctx, task, tasks)
		}
	}
	return s.startTask(ctx, turn, rest(text, strings.Fields(text)[0]))
}

func (s *server) startTask(ctx context.Context, turn, goal string) (string, error) {
	if err := s.requireGit(ctx); err != nil {
		return "", err
	}
	repo, err := git(ctx, s.scope.workspace, nil, 5*time.Second, "rev-parse", "--show-toplevel")
	if err != nil {
		return "", err
	}
	full, err := newID()
	if err != nil {
		return "", err
	}
	id := full[:8]
	path := filepath.Join(s.opts.root, "worktrees", id)
	branch := "aria/task-" + id
	if _, err := git(ctx, repo, nil, 60*time.Second, "worktree", "add", "-q", "-b", branch, path, "HEAD"); err != nil {
		return "", fmt.Errorf("could not create the worktree: %w", err)
	}
	task := taskRecord{ID: id, Goal: goal, Path: path, Branch: branch, Repo: repo, Created: time.Now().UTC().Format(time.RFC3339)}
	tasks, err := s.loadTasks()
	if err != nil {
		return "", err
	}
	tasks = append(tasks, task)
	if err := atomicJSON(s.tasksFile(), tasks); err != nil {
		return "", err
	}
	// The task works in the worktree at the same position inside the repo.
	workspace := path
	if rel, ok := within(s.scope.workspace, repo); ok && rel != "." {
		workspace = filepath.Join(path, rel)
	}
	sc, err := newScope(workspace, nil, nil, !remoteScope())
	if err != nil {
		return "", err
	}
	sc.readOnly = s.scope.readOnly
	prompt := goal + "\n\nYou are working in an isolated git worktree of this project (branch " + branch + "). Make and verify the change here; the user reviews it with /task diff and brings it over with /task apply."
	_, answer, err := nativeTurn(ctx, s.provider, sc, s.cfg, nil, prompt, s.turnHooks(turn, sc, func() {}))
	tail := fmt.Sprintf("\n\nTask %s ran in %s. Uncommitted changes in your checkout were not copied.\n/task diff · /task apply · /task discard", id, path)
	if err != nil {
		return strings.TrimSpace(answer + tail), err
	}
	for i := range tasks {
		if tasks[i].ID == id {
			tasks[i].Answer = capText(answer, 4000)
		}
	}
	_ = atomicJSON(s.tasksFile(), tasks)
	return strings.TrimSpace(answer) + tail, nil
}

// taskDiff stages everything in the worktree (it is Aria's own checkout)
// and shows it against the commit the task started from.
func (s *server) taskDiff(ctx context.Context, task taskRecord, stat bool) (string, error) {
	if _, err := git(ctx, task.Path, nil, 30*time.Second, "add", "-A"); err != nil {
		return "", fmt.Errorf("task worktree %s: %w", task.Path, err)
	}
	args := []string{"diff", "--cached", "--no-ext-diff", "HEAD"}
	if stat {
		args = append(args, "--stat")
	} else {
		args = append(args, "--binary")
	}
	out, err := git(ctx, task.Path, nil, 30*time.Second, args...)
	if err != nil {
		return "", err
	}
	if out == "" {
		return "The task changed nothing.", nil
	}
	return capText(out, 60000), nil
}

func (s *server) applyTask(ctx context.Context, turn string, task taskRecord) (string, error) {
	patch, err := s.taskDiff(ctx, task, false)
	if err != nil {
		return "", err
	}
	if patch == "The task changed nothing." {
		return patch, nil
	}
	file := filepath.Join(s.opts.root, "worktrees", task.ID+".patch")
	if err := atomicWrite(file, []byte(patch+"\n")); err != nil {
		return "", err
	}
	out, err := s.userCommand(ctx, turn, "git apply --3way "+argQuote(file))
	if err == nil && !strings.HasPrefix(out, "Command was not executed") {
		out += "\nApplied task " + task.ID + " to your checkout. /task discard removes its worktree."
	}
	return out, err
}

func (s *server) discardTask(ctx context.Context, task taskRecord, tasks []taskRecord) (string, error) {
	if _, err := git(ctx, task.Repo, nil, 60*time.Second, "worktree", "remove", "--force", task.Path); err != nil && !os.IsNotExist(statErr(task.Path)) {
		return "", err
	}
	_, _ = git(ctx, task.Repo, nil, 30*time.Second, "branch", "-D", task.Branch)
	kept := tasks[:0]
	for _, t := range tasks {
		if t.ID != task.ID {
			kept = append(kept, t)
		}
	}
	if err := atomicJSON(s.tasksFile(), kept); err != nil {
		return "", err
	}
	os.Remove(filepath.Join(s.opts.root, "worktrees", task.ID+".patch"))
	return "Discarded task " + task.ID + ".", nil
}

func statErr(path string) error {
	_, err := os.Stat(path)
	return err
}

func (s *server) tasksCommand(ctx context.Context) (string, error) {
	tasks, err := s.loadTasks()
	if err != nil {
		return "", err
	}
	if len(tasks) == 0 {
		return "No tasks. /task <goal> runs one in its own git worktree.", nil
	}
	var b strings.Builder
	b.WriteString("Tasks")
	for i := len(tasks) - 1; i >= 0; i-- {
		t := tasks[i]
		state := "ready"
		if statErr(t.Path) != nil {
			state = "worktree missing"
		}
		fmt.Fprintf(&b, "\n  %s  %-16s %s  %s", t.ID, state, t.Created[:min(10, len(t.Created))], oneLine(capText(t.Goal, 80)))
	}
	return b.String() + "\n\n/task diff|apply|discard [id]", nil
}

// ── /delegate and /agents ─────────────────────────────────────────────────

type projectAgent struct {
	name, description, instructions, path string
}

var agentName = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]*$`)

// projectAgents are .aria/agents/<name>.md: a first line describing the
// agent, then its instructions.
func projectAgents(workspace string) []projectAgent {
	paths, _ := filepath.Glob(filepath.Join(workspace, ".aria", "agents", "*.md"))
	sort.Strings(paths)
	agents := []projectAgent{}
	for _, path := range paths {
		name := strings.ToLower(strings.TrimSuffix(filepath.Base(path), ".md"))
		body, err := os.ReadFile(path)
		if err != nil || !agentName.MatchString(name) {
			continue
		}
		text := strings.TrimSpace(capText(string(body), instructionFileLimit))
		first, _, _ := strings.Cut(text, "\n")
		agents = append(agents, projectAgent{name: name, description: strings.TrimLeft(first, "# "), instructions: text, path: path})
	}
	return agents
}

func (s *server) agentsCommand() (string, error) {
	agents := projectAgents(s.scope.workspace)
	if len(agents) == 0 {
		return "No project agents. Add .aria/agents/<name>.md (first line: what it does; then its instructions) and run it with /delegate --agent <name> <task>.", nil
	}
	var b strings.Builder
	b.WriteString("Project agents")
	for _, a := range agents {
		fmt.Fprintf(&b, "\n  %-16s %s", a.name, capText(a.description, 100))
	}
	return b.String() + "\n\n/delegate --agent <name> <task>", nil
}

// delegateCommand runs the task as a subagent: a fresh context with the same
// tools, scope and approvals. Its answer joins the conversation.
func (s *server) delegateCommand(ctx context.Context, turn, text string) (string, error) {
	task := text
	var agent *projectAgent
	if fields := strings.Fields(text); len(fields) >= 2 && fields[0] == "--agent" {
		for _, a := range projectAgents(s.scope.workspace) {
			if a.name == fields[1] {
				a := a
				agent = &a
			}
		}
		if agent == nil {
			return "", fmt.Errorf("no project agent %q; /agents lists them", fields[1])
		}
		task = rest(rest(text, "--agent"), fields[1])
	}
	if strings.TrimSpace(task) == "" {
		return "", errors.New("usage: /delegate [--agent <name>] <task>")
	}
	prompt := "You are a subagent with a fresh context. Do this task completely and report what you did and found, with file references:\n\n" + task
	if agent != nil {
		prompt = "Act as the project agent \"" + agent.name + "\", following its instructions:\n\n" + agent.instructions + "\n\n---\n\n" + prompt
	}
	_, answer, err := nativeTurn(ctx, s.provider, s.scope, s.cfg, nil, prompt, s.turnHooks(turn, s.scope, func() {}))
	if err != nil {
		return answer, err
	}
	label := "Subagent"
	if agent != nil {
		label = "Agent " + agent.name
	}
	s.contents = append(settlePending(s.contents),
		genai.NewContentFromText("I delegated this task to a subagent: "+task, genai.RoleUser),
		genai.NewContentFromText(label+" reported:\n"+answer, genai.RoleModel))
	return label + " reported:\n\n" + answer, nil
}

// ── /consult: a second opinion from another model ─────────────────────────

func (s *server) consultCommand(ctx context.Context, text string) (string, error) {
	target, question, _ := strings.Cut(strings.TrimSpace(text), " ")
	provider, model, ok := strings.Cut(target, "/")
	question = strings.TrimSpace(question)
	if !ok || provider == "" || model == "" || question == "" {
		return "", errors.New("usage: /consult <provider>/<model> <question> — e.g. /consult ollama/qwen3 is this migration safe?")
	}
	next := config{}
	for key, value := range s.cfg {
		next[key] = value
	}
	next["local_provider"], next["model"] = strings.ToLower(provider), model
	if strings.EqualFold(provider, "vertexai") || strings.EqualFold(provider, "gcp") {
		next["local_provider"], next["use_vertexai"] = "vertexai", true
	}
	other, err := modelProviderFor(next)
	if err != nil {
		return "", err
	}
	history := append(settlePending(append([]*genai.Content(nil), s.contents...)), genai.NewContentFromText(question, genai.RoleUser))
	reply, err := other.Generate(ctx, history, "You are giving a second opinion on a conversation between a user and another assistant. Be direct: agree or disagree, and say why. Do not use tools.", nil, func(string) {})
	if err != nil {
		return "", err
	}
	answer := strings.TrimSpace(contentText(reply))
	if answer == "" {
		return "", errors.New(target + " returned no text")
	}
	return target + " says:\n\n" + answer, nil
}

// ── /workflow: .aria/workflows/<name>.yaml ────────────────────────────────

type workflowStep struct {
	Name            string `yaml:"name"`
	Run             string `yaml:"run"`
	Prompt          string `yaml:"prompt"`
	Command         string `yaml:"command"`
	Confirm         bool   `yaml:"confirm"`
	ContinueOnError bool   `yaml:"continue_on_error"`
	Timeout         int    `yaml:"timeout"`
}

type workflow struct {
	Name         string         `yaml:"name"`
	Description  string         `yaml:"description"`
	Steps        []workflowStep `yaml:"steps"`
	path, digest string
}

var placeholder = regexp.MustCompile(`\{\{\s*(\w+)\s*\}\}`)

func workflowFiles(workspace string) map[string]string {
	dirs := []string{filepath.Join(workspace, ".aria", "workflows")}
	for dir := filepath.Dir(workspace); ; dir = filepath.Dir(dir) {
		if statErr(filepath.Join(workspace, ".git")) == nil {
			break
		}
		if statErr(filepath.Join(dir, ".git")) == nil {
			dirs = append(dirs, filepath.Join(dir, ".aria", "workflows"))
			break
		}
		if filepath.Dir(dir) == dir {
			break
		}
	}
	found := map[string]string{}
	for _, dir := range dirs {
		for _, pattern := range []string{"*.yaml", "*.yml"} {
			paths, _ := filepath.Glob(filepath.Join(dir, pattern))
			sort.Strings(paths)
			for _, path := range paths {
				name := strings.ToLower(strings.TrimSuffix(strings.TrimSuffix(filepath.Base(path), ".yaml"), ".yml"))
				if _, ok := found[name]; !ok {
					found[name] = path
				}
			}
		}
	}
	return found
}

func parseWorkflow(path string) (workflow, error) {
	body, err := os.ReadFile(path)
	if err != nil {
		return workflow{}, err
	}
	var w workflow
	decoder := yaml.NewDecoder(strings.NewReader(string(body)))
	decoder.KnownFields(true)
	if err := decoder.Decode(&w); err != nil {
		return workflow{}, fmt.Errorf("%s: %w", filepath.Base(path), err)
	}
	if w.Name == "" {
		w.Name = strings.TrimSuffix(strings.TrimSuffix(filepath.Base(path), ".yaml"), ".yml")
	}
	w.Name = strings.ToLower(w.Name)
	if !agentName.MatchString(w.Name) {
		return workflow{}, fmt.Errorf("%s: name %q must be lowercase letters, digits, - or _", filepath.Base(path), w.Name)
	}
	if len(w.Steps) == 0 {
		return workflow{}, fmt.Errorf("%s: steps must be a non-empty list", filepath.Base(path))
	}
	for i, step := range w.Steps {
		kinds := 0
		for _, body := range []string{step.Run, step.Prompt, step.Command} {
			if strings.TrimSpace(body) != "" {
				kinds++
			}
		}
		if kinds != 1 {
			return workflow{}, fmt.Errorf("%s, step %d: needs exactly one of run, prompt, command", filepath.Base(path), i+1)
		}
		if step.Command != "" && !strings.HasPrefix(strings.TrimSpace(step.Command), "/") {
			return workflow{}, fmt.Errorf("%s, step %d: command must be a slash command, e.g. /review", filepath.Base(path), i+1)
		}
		if strings.HasPrefix(strings.TrimSpace(step.Command), "/workflow") || strings.HasPrefix(strings.TrimSpace(step.Command), "/wf") {
			return workflow{}, fmt.Errorf("%s, step %d: a workflow cannot run another workflow", filepath.Base(path), i+1)
		}
		if step.Timeout <= 0 {
			w.Steps[i].Timeout = 300
		}
		if step.Name == "" {
			w.Steps[i].Name = capText(oneLine(step.Run+step.Prompt+step.Command), 40)
		}
	}
	sum := sha256.Sum256(body)
	w.path, w.digest = path, hex.EncodeToString(sum[:])
	return w, nil
}

func (w workflow) describe() string {
	var b strings.Builder
	fmt.Fprintf(&b, "/%s — %s\n%s", w.Name, w.Description, w.path)
	for i, step := range w.Steps {
		kind, body := "run", step.Run
		if step.Prompt != "" {
			kind, body = "prompt", step.Prompt
		} else if step.Command != "" {
			kind, body = "command", step.Command
		}
		fmt.Fprintf(&b, "\n  %d. %s — %s: %s", i+1, step.Name, kind, capText(oneLine(body), 160))
	}
	return b.String()
}

const workflowTemplate = `# /workflow run %s [args] — {{args}} is what you type after the name.
description: Test, review and summarise the current change
steps:
  - name: Tests
    run: go test ./...
  - name: Review
    command: /review
  - name: Summary
    prompt: Summarise the current change for a pull request description. {{args}}
    continue_on_error: true
`

func (s *server) workflowCommand(ctx context.Context, turn, text string) (string, error) {
	fields := strings.Fields(text)[1:]
	files := workflowFiles(s.scope.workspace)
	sub := "list"
	if len(fields) > 0 {
		sub = fields[0]
	}
	switch sub {
	case "list":
		if len(files) == 0 {
			return "No workflows. /workflow new <name> writes .aria/workflows/<name>.yaml to start from.", nil
		}
		names := make([]string, 0, len(files))
		for name := range files {
			names = append(names, name)
		}
		sort.Strings(names)
		var b strings.Builder
		b.WriteString("Workflows")
		for _, name := range names {
			w, err := parseWorkflow(files[name])
			if err != nil {
				fmt.Fprintf(&b, "\n  %-16s ✗ %s", name, err)
				continue
			}
			fmt.Fprintf(&b, "\n  %-16s %s (%d steps)", name, w.Description, len(w.Steps))
		}
		return b.String() + "\n\n/workflow show|run <name>", nil
	case "show", "run":
		if len(fields) < 2 {
			return "", fmt.Errorf("usage: /workflow %s <name>", sub)
		}
		path, ok := files[strings.ToLower(fields[1])]
		if !ok {
			return "", fmt.Errorf("no workflow %q; /workflow list shows them", fields[1])
		}
		w, err := parseWorkflow(path)
		if err != nil {
			return "", err
		}
		if sub == "show" {
			return w.describe(), nil
		}
		return s.runWorkflow(ctx, turn, w, rest(rest(rest(text, strings.Fields(text)[0]), "run"), fields[1]))
	case "new":
		if len(fields) != 2 || !agentName.MatchString(fields[1]) {
			return "", errors.New("usage: /workflow new <name> (lowercase letters, digits, - or _)")
		}
		path := filepath.Join(s.scope.workspace, ".aria", "workflows", fields[1]+".yaml")
		if statErr(path) == nil {
			return "", errors.New(path + " already exists")
		}
		opened, err := s.scope.open(path, true)
		if err != nil {
			return "", err
		}
		opened.root.Close()
		if err := atomicWrite(opened.absolute, []byte(fmt.Sprintf(workflowTemplate, fields[1]))); err != nil {
			return "", err
		}
		return "Wrote " + opened.absolute + ". Edit its steps, then /workflow run " + fields[1] + ".", nil
	}
	return "", errors.New("usage: /workflow [list | show <name> | run <name> [args] | new <name>]")
}

type trustedWorkflows map[string]string

func (s *server) runWorkflow(ctx context.Context, turn string, w workflow, args string) (string, error) {
	// The file comes with the repository: its first run, and the first after
	// it changes, shows every step and asks.
	trustPath := filepath.Join(s.opts.root, "trusted_workflows.json")
	trusted := trustedWorkflows{}
	if err := readJSON(trustPath, &trusted); err != nil && !os.IsNotExist(err) {
		return "", err
	}
	if trusted[w.path] != w.digest {
		allowed, reason, err := s.approve(ctx, "run_workflow", map[string]any{"preview": w.describe()}, fileChange{})
		if err != nil {
			return "", err
		}
		if !allowed {
			return "Workflow not run: " + reason, nil
		}
		trusted[w.path] = w.digest
		if err := atomicJSON(trustPath, trusted); err != nil {
			return "", err
		}
	}
	vars := map[string]string{"args": strings.TrimSpace(args), "date": time.Now().Format("2006-01-02"), "workflow": w.Name}
	fill := func(text string) string {
		return placeholder.ReplaceAllStringFunc(text, func(m string) string {
			key := placeholder.FindStringSubmatch(m)[1]
			if value, ok := vars[key]; ok {
				return value
			}
			return m
		})
	}
	var b strings.Builder
	fmt.Fprintf(&b, "Workflow /%s", w.Name)
	failed := false
	for i, step := range w.Steps {
		if ctx.Err() != nil {
			return b.String(), ctx.Err()
		}
		label := fmt.Sprintf("%d. %s", i+1, step.Name)
		if step.Confirm {
			allowed, reason, err := s.approve(ctx, "run_workflow", map[string]any{"preview": "Next step of /" + w.Name + ":\n" + label}, fileChange{})
			if err != nil {
				return b.String(), err
			}
			if !allowed {
				fmt.Fprintf(&b, "\n  – %s: skipped (%s); stopped", label, reason)
				return b.String(), nil
			}
		}
		var out string
		var err error
		switch {
		case step.Run != "":
			out, err = s.userCommandTimeout(ctx, turn, fill(step.Run), step.Timeout)
			if err == nil && strings.HasPrefix(out, "Command was not executed") {
				err = errors.New(out)
			}
		case step.Command != "":
			out, err = s.command(ctx, turn, fill(step.Command))
		default:
			var contents []*genai.Content
			contents, out, err = nativeTurn(ctx, s.provider, s.scope, s.cfg, s.contents, fill(step.Prompt), s.turnHooks(turn, s.scope, func() {}))
			if err == nil {
				s.contents = contents
			}
		}
		if err != nil {
			failed = true
			fmt.Fprintf(&b, "\n  ✗ %s: %s", label, capText(oneLine(err.Error()), 300))
			if out != "" {
				fmt.Fprintf(&b, "\n\n%s\n", capText(out, 8000))
			}
			if !step.ContinueOnError {
				return b.String(), fmt.Errorf("workflow /%s stopped at step %d", w.Name, i+1)
			}
			continue
		}
		fmt.Fprintf(&b, "\n  ✓ %s", label)
		if step.Prompt != "" || step.Command != "" {
			fmt.Fprintf(&b, "\n\n%s\n", capText(strings.TrimSpace(out), 8000))
		}
	}
	if failed {
		b.WriteString("\n\nFinished with failures.")
	}
	return b.String(), nil
}
