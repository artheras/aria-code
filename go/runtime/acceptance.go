package main

// The acceptance gate — a port of src/aria_code/runtime/acceptance.py, with
// the verification planner (workspace/verify.py) and the declared commands
// (config acceptance_commands, .ariarc acceptance.default). The gate arms
// when a turn writes a file and fires when the model says it is done: it runs
// the checks, and a red result goes back to the model with the failing
// output instead of ending the turn. It is bounded by acceptance_max_attempts
// and runs commands through the same command policy as run_command.
//
// A red check is rerun on the code before the change (baseline.go) so a
// pre-existing failure is not sent back to be repaired. Domain-pack commands
// are not ported yet.
// tests/test_native_go_acceptance.py compares the rest with Python.

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const (
	trimHeadLines = 20
	trimTailLines = 45
	trimMaxChars  = 6000
)

var (
	pyAnchor    = pyRegexp(`File "([^"]+)", line (\d+)`)
	colonAnchor = pyRegexp(`(?m)^\s*([\w./\\-]+\.[A-Za-z]{1,4}):(\d+)(?::\d+)?`)
)

// trimOutput is acceptance._trim: head and tail of a failing log.
func trimOutput(text string) string {
	body := strings.TrimSpace(text)
	if body == "" {
		return ""
	}
	lines := pySplitLines(body)
	if len(lines) > trimHeadLines+trimTailLines {
		omitted := len(lines) - trimHeadLines - trimTailLines
		kept := append([]string{}, lines[:trimHeadLines]...)
		kept = append(kept, fmt.Sprintf("… [%d lines omitted] …", omitted))
		lines = append(kept, lines[len(lines)-trimTailLines:]...)
	}
	joined := strings.Join(lines, "\n")
	if runes := []rune(joined); len(runes) > trimMaxChars {
		keep := trimMaxChars / 2
		joined = string(runes[:keep]) + "\n… [truncated] …\n" + string(runes[len(runes)-keep:])
	}
	return joined
}

// failureAnchors is acceptance._anchors: file:line locations in a failure,
// the last (innermost) five.
func failureAnchors(text string) []string {
	var out []string
	for _, pattern := range []*regexp.Regexp{pyAnchor, colonAnchor} {
		for _, m := range pattern.FindAllStringSubmatch(text, -1) {
			out = appendNew(out, m[1]+":"+m[2])
		}
	}
	if len(out) > 5 {
		out = out[len(out)-5:]
	}
	return out
}

type checkResult struct {
	Command  string
	ExitCode int
	Passed   bool
	Output   string
	Duration float64
	Error    string
	// From the test baseline when the check failed: whether every failure
	// was already there before the change, and which test ids are which.
	Preexisting  bool
	NewFailures  []string
	OldFailures  []string
	BaselineNote string
}

func (c checkResult) anchors() []string {
	if c.Passed {
		return nil
	}
	return failureAnchors(c.Output)
}

func (c checkResult) summary() map[string]any {
	data := map[string]any{"command": c.Command, "exit_code": c.ExitCode, "passed": c.Passed,
		"duration": float64(int64(c.Duration*1000+0.5)) / 1000, "error": c.Error}
	if !c.Passed && (c.BaselineNote != "" || len(c.NewFailures) > 0 || len(c.OldFailures) > 0) {
		data["preexisting"] = c.Preexisting
		data["new_failures"] = anySlice(orEmpty(c.NewFailures))
		data["old_failures"] = anySlice(orEmpty(c.OldFailures))
		data["baseline"] = c.BaselineNote
	}
	return data
}

type acceptanceReport struct {
	Attempt           int
	Checks            []checkResult
	Reason            string
	Paths             []string
	AttemptsRemaining int
}

func (r acceptanceReport) ran() bool { return len(r.Checks) > 0 }

// passed is true only when a check ran and every check passed.
func (r acceptanceReport) passed() bool {
	if !r.ran() {
		return false
	}
	for _, c := range r.Checks {
		if !c.Passed {
			return false
		}
	}
	return true
}

func (r acceptanceReport) failures() []checkResult {
	var out []checkResult
	for _, c := range r.Checks {
		if !c.Passed {
			out = append(out, c)
		}
	}
	return out
}

// regressed: a check failed that was not already failing before the change.
// Without a baseline every failure is the change's.
func (r acceptanceReport) regressed() bool {
	for _, c := range r.failures() {
		if !c.Preexisting {
			return true
		}
	}
	return false
}

func backticked(items []string) string {
	quoted := make([]string, len(items))
	for i, item := range items {
		quoted[i] = "`" + item + "`"
	}
	return strings.Join(quoted, ", ")
}

func (r acceptanceReport) headline() string {
	if !r.ran() {
		return "验收未执行：未推断出检查命令"
	}
	if r.passed() {
		var names []string
		for _, c := range r.Checks {
			names = append(names, c.Command)
		}
		return "验收通过 (" + strings.Join(names, ", ") + ")"
	}
	first := r.failures()[0]
	if !r.regressed() {
		return fmt.Sprintf("验收未通过，但失败在改动前就已存在：`%s` exit %d", first.Command, first.ExitCode)
	}
	return fmt.Sprintf("验收失败：`%s` exit %d", first.Command, first.ExitCode)
}

// repairDirective is what the model is told when the check came back red.
func (r acceptanceReport) repairDirective() string {
	blocks := []string{
		"## 验收未通过 (Acceptance check failed)",
		"",
		"你声明任务已完成，但改动过的文件没有通过自动验收检查。以下是**实际执行**的命令和真实输出：",
		"",
	}
	for _, c := range r.failures() {
		if c.Preexisting {
			continue
		}
		blocks = append(blocks, fmt.Sprintf("### `%s` → exit %d", c.Command, c.ExitCode))
		if len(c.NewFailures) > 0 {
			blocks = append(blocks, "本次改动新引入的失败："+backticked(c.NewFailures))
		}
		if len(c.OldFailures) > 0 {
			blocks = append(blocks, "以下失败在改动前就存在，与本任务无关，不要修改："+backticked(c.OldFailures))
		}
		if c.Error != "" {
			blocks = append(blocks, "（执行错误："+c.Error+"）")
		}
		if body := trimOutput(c.Output); body != "" {
			blocks = append(blocks, "```\n"+body+"\n```")
		}
		if anchors := c.anchors(); len(anchors) > 0 {
			blocks = append(blocks, "定位："+backticked(anchors))
		}
		blocks = append(blocks, "")
	}
	blocks = append(blocks, "请依次：(1) 读出错位置的代码确认原因，(2) 用 `edit_file` 定点修补——不要整文件重写，(3) 修好后直接说明修了什么。修改后验收会自动重跑，你**不需要**自己调用验收命令。")
	if r.AttemptsRemaining <= 0 {
		blocks = append(blocks, "注意：这是最后一次修复机会。若仍未通过，请如实说明失败原因，不要声称任务已完成。")
	} else {
		blocks = append(blocks, fmt.Sprintf("剩余自动重试次数：%d。", r.AttemptsRemaining))
	}
	return strings.Join(blocks, "\n")
}

func (r acceptanceReport) summary() map[string]any {
	checks := make([]any, len(r.Checks))
	for i, c := range r.Checks {
		checks[i] = c.summary()
	}
	return map[string]any{"attempt": r.Attempt, "passed": r.passed(), "regressed": r.regressed(), "ran": r.ran(),
		"reason": r.Reason, "paths": anySlice(orEmpty(r.Paths)), "checks": checks}
}

func anySlice(items []string) []any {
	out := make([]any, len(items))
	for i, s := range items {
		out[i] = s
	}
	return out
}

// readRunResult is acceptance._read_run_result: (exit code, output, error),
// never reading an unreadable result as exit 0.
func readRunResult(raw map[string]any) (int, string, string) {
	if len(raw) == 0 {
		return 1, "", "runner returned no result"
	}
	if success, ok := raw["success"].(bool); ok && !success {
		output, reason := "", "command failed"
		if truthy(raw["output"]) {
			output = pyStr(raw["output"])
		}
		if truthy(raw["error"]) {
			reason = pyStr(raw["error"])
		}
		return 1, output, reason
	}
	data := resultData(raw)
	code, found := 0, false
	for _, key := range []string{"exit_code", "returncode", "code", "status"} {
		value, present := data[key]
		if !present {
			continue
		}
		if n, ok := pyIntStrict(value); ok {
			code, found = n, true
			break
		}
	}
	if !found {
		return 1, "", "runner returned no exit status"
	}
	stdout := ""
	if truthy(data["stdout"]) {
		stdout = pyStr(data["stdout"])
	} else if truthy(data["output"]) {
		stdout = pyStr(data["output"])
	}
	stderr := ""
	if truthy(data["stderr"]) {
		stderr = pyStr(data["stderr"])
	}
	var parts []string
	for _, part := range []string{stdout, stderr} {
		if strings.TrimSpace(part) != "" {
			parts = append(parts, part)
		}
	}
	return code, strings.Join(parts, "\n"), ""
}

// pyIntStrict is int() for an exit status: ints, integral floats truncate,
// numeric strings parse; None and the rest fail.
func pyIntStrict(value any) (int, bool) {
	if value == nil {
		return 0, false
	}
	switch value.(type) {
	case []any, map[string]any:
		return 0, false
	}
	return pyInt(value)
}

// ── planning ────────────────────────────────────────────────────────────────

type verificationPlan struct {
	Commands []string `json:"commands"`
	Reason   string   `json:"reason"`
}

var shellUnsafe = regexp.MustCompile(`[^A-Za-z0-9_@%+=:,./-]`)

// shellQuote is shlex.quote.
func shellQuote(s string) string {
	if s == "" {
		return "''"
	}
	if !shellUnsafe.MatchString(s) {
		return s
	}
	return "'" + strings.ReplaceAll(s, "'", `'"'"'`) + "'"
}

func hasSuffix(path string, suffixes ...string) bool {
	for _, s := range suffixes {
		if strings.HasSuffix(path, s) {
			return true
		}
	}
	return false
}

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// planVerification is VerificationPlanner.infer.
func planVerification(root string, paths []string) verificationPlan {
	root = resolveLoose(expandUser(root))
	var changed []string
	for _, p := range paths {
		if p != "" {
			changed = append(changed, p)
		}
	}
	if len(changed) == 0 {
		changed = gitChangedFiles(root)
	}
	var commands, reasons []string
	var py []string
	for _, p := range changed {
		if strings.HasSuffix(p, ".py") {
			py = append(py, p)
		}
	}
	if len(py) > 0 {
		quoted := make([]string, 0, 20)
		for _, p := range py[:min(20, len(py))] {
			quoted = append(quoted, shellQuote(p))
		}
		commands = append(commands, "python3 -m py_compile "+strings.Join(quoted, " "))
		reasons = append(reasons, "Python files changed")
		if hasAny(root, "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini") || exists(filepath.Join(root, "tests")) || hasPythonTests(root) {
			commands = append(commands, pythonTestCommand(root))
		}
	}
	frontend := false
	for _, p := range changed {
		if hasSuffix(p, ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css") {
			frontend = true
		}
	}
	if frontend && exists(filepath.Join(root, "package.json")) {
		scripts := packageScripts(root)
		reasons = append(reasons, "Node/TypeScript files changed")
		if _, ok := scripts["test"]; ok {
			commands = append(commands, "npm test")
		}
		if _, ok := scripts["build"]; ok {
			commands = append(commands, "npm run build")
		} else if exists(filepath.Join(root, "tsconfig.json")) {
			commands = append(commands, "npx tsc --noEmit")
		}
	}
	if len(commands) == 0 {
		if exists(filepath.Join(root, "pyproject.toml")) || exists(filepath.Join(root, "tests")) {
			commands = append(commands, pythonTestCommand(root))
			reasons = append(reasons, "Python project detected")
		} else if exists(filepath.Join(root, "package.json")) {
			scripts := packageScripts(root)
			if _, ok := scripts["test"]; ok {
				commands = append(commands, "npm test")
				reasons = append(reasons, "Node project detected")
			} else if _, ok := scripts["build"]; ok {
				commands = append(commands, "npm run build")
				reasons = append(reasons, "Node project detected")
			}
		}
	}
	reason := "No focused check inferred"
	if len(reasons) > 0 {
		reason = strings.Join(appendNew(nil, reasons...), "; ")
	}
	return verificationPlan{Commands: orEmpty(appendNew(nil, commands...)), Reason: reason}
}

var skipTestDirs = map[string]bool{
	".git": true, "node_modules": true, ".venv": true, "venv": true, "env": true,
	"__pycache__": true, "build": true, "dist": true, "site-packages": true,
}

// hasPythonTests is VerificationPlanner._has_python_tests: test files beside the
// code count as a test suite even with no tests/ directory and no config file.
// The root and two levels below it are looked at; virtualenvs and hidden
// directories are not.
func hasPythonTests(root string) bool {
	found := false
	filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil || found {
			return filepath.SkipDir
		}
		rel, relErr := filepath.Rel(root, path)
		if relErr != nil {
			return filepath.SkipDir
		}
		depth := 0
		if rel != "." {
			depth = strings.Count(rel, string(filepath.Separator)) + 1
		}
		if d.IsDir() {
			if depth > 0 && (skipTestDirs[d.Name()] || strings.HasPrefix(d.Name(), ".")) {
				return filepath.SkipDir
			}
			if depth > 2 {
				return filepath.SkipDir
			}
			return nil
		}
		name := d.Name()
		if strings.HasPrefix(name, "test_") && strings.HasSuffix(name, ".py") || strings.HasSuffix(name, "_test.py") || name == "conftest.py" {
			found = true
			return filepath.SkipAll
		}
		return nil
	})
	return found
}

func hasAny(root string, names ...string) bool {
	for _, name := range names {
		if exists(filepath.Join(root, name)) {
			return true
		}
	}
	return false
}

func packageScripts(root string) map[string]any {
	text, err := os.ReadFile(filepath.Join(root, "package.json"))
	if err != nil {
		return nil
	}
	var data map[string]any
	if json.Unmarshal(text, &data) != nil {
		return nil
	}
	scripts, _ := data["scripts"].(map[string]any)
	return scripts
}

var (
	pyImport      = regexp.MustCompile(`(?m)^[ \t]*import[ \t]+([^\n#;]+)`)
	pyFromImport  = regexp.MustCompile(`(?m)^[ \t]*from[ \t]+([A-Za-z_][\w.]*)[ \t]+import\b`)
	pyTopLevelDef = regexp.MustCompile(`(?m)^(?:async[ \t]+)?def[ \t]+test`)
)

// pythonTestCommand is VerificationPlanner._python_test_command: the
// project's runner, chosen without importing its tests. Python reads the test
// files' syntax trees; here imports and top-level test functions are read
// line by line, which agrees on ordinary test files.
func pythonTestCommand(root string) string {
	for _, name := range []string{"pytest.ini", "pyproject.toml", "setup.cfg"} {
		path := filepath.Join(root, name)
		if info, err := os.Stat(path); err == nil && info.Mode().IsRegular() {
			text, _ := os.ReadFile(path)
			if name == "pytest.ini" || strings.Contains(string(text), "[tool.pytest") || strings.Contains(string(text), "[tool:pytest]") {
				return "python3 -m pytest -q"
			}
		}
	}
	testsRoot := filepath.Join(root, "tests")
	var candidates []string
	if info, err := os.Stat(testsRoot); err == nil && info.IsDir() {
		filepath.WalkDir(testsRoot, func(path string, d os.DirEntry, err error) error {
			if err == nil && !d.IsDir() && strings.HasPrefix(d.Name(), "test") && strings.HasSuffix(d.Name(), ".py") && len(candidates) < 100 {
				candidates = append(candidates, path)
			}
			return nil
		})
	} else {
		testsRoot = root
		matches, _ := filepath.Glob(filepath.Join(root, "test*.py"))
		suffixed, _ := filepath.Glob(filepath.Join(root, "*_test.py"))
		matches = append(matches, suffixed...)
		candidates = matches[:min(100, len(matches))]
	}
	usesUnittest := false
	for _, path := range candidates {
		body, err := os.ReadFile(path)
		if err != nil || !utf8Valid(body) {
			return "python3 -m pytest -q"
		}
		text := string(body)
		var modules []string
		for _, m := range pyImport.FindAllStringSubmatch(text, -1) {
			for _, name := range strings.Split(m[1], ",") {
				name = strings.TrimSpace(strings.Fields(name + " ")[0])
				modules = append(modules, strings.Split(name, ".")[0])
			}
		}
		for _, m := range pyFromImport.FindAllStringSubmatch(text, -1) {
			modules = append(modules, strings.Split(m[1], ".")[0])
		}
		if contains(modules, "pytest") {
			return "python3 -m pytest -q"
		}
		usesUnittest = usesUnittest || contains(modules, "unittest")
		if pyTopLevelDef.MatchString(text) {
			return "python3 -m pytest -q"
		}
	}
	if usesUnittest {
		directory := "."
		if testsRoot != root {
			directory = "tests"
		}
		return "python3 -m unittest discover -s " + directory + " -v"
	}
	return "python3 -m pytest -q"
}

func utf8Valid(b []byte) bool { return strings.ToValidUTF8(string(b), "�") == string(b) }

// gitChangedFiles is the planner's fallback when no path was recorded.
func gitChangedFiles(root string) []string {
	run := func(args ...string) (string, bool) {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, "git", args...)
		cmd.Dir = root
		out, err := cmd.Output()
		return string(out), err == nil
	}
	var files []string
	if out, ok := run("diff", "--name-only", "HEAD"); ok && strings.TrimSpace(out) != "" {
		for _, line := range pySplitLines(out) {
			if strings.TrimSpace(line) != "" {
				files = append(files, strings.TrimSpace(line))
			}
		}
		return files
	}
	if out, ok := run("status", "--short"); ok {
		for _, line := range pySplitLines(out) {
			if strings.TrimSpace(line) != "" && len(line) > 3 {
				files = append(files, strings.TrimSpace(line[3:]))
			}
		}
	}
	return files
}

// ── declared commands ───────────────────────────────────────────────────────

// declaredAcceptanceCommands is what the session or workspace says "green"
// means: acceptance_commands in the config, else .ariarc acceptance.default.
func declaredAcceptanceCommands(cfg config, workspace string) []string {
	if explicit := asSlice(cfg["acceptance_commands"]); len(explicit) > 0 {
		var out []string
		for _, item := range explicit {
			out = append(out, pyStr(item))
		}
		return out
	}
	data := loadAriarc(workspace)
	table, _ := data["acceptance"].(map[string]any)
	switch value := table["default"].(type) {
	case string:
		if text := strings.TrimSpace(value); text != "" {
			return []string{text}
		}
	case []any:
		var out []string
		for _, item := range value {
			if text, ok := item.(string); ok && strings.TrimSpace(text) != "" {
				out = appendNew(out, strings.TrimSpace(text))
			}
		}
		return out
	}
	return nil
}

// loadAriarc is ariarc.AriaRC.load from start: the nearest .ariarc,
// .ariarc.json or .ariarc.jsonc walking up to $HOME, as JSON with comments.
func loadAriarc(start string) map[string]any {
	home, _ := os.UserHomeDir()
	current := resolveLoose(start)
	for {
		for _, name := range []string{".ariarc", ".ariarc.json", ".ariarc.jsonc"} {
			path := filepath.Join(current, name)
			if info, err := os.Stat(path); err == nil && info.Mode().IsRegular() {
				text, err := os.ReadFile(path)
				if err != nil {
					return nil
				}
				var data map[string]any
				if json.Unmarshal([]byte(stripJSONComments(string(text))), &data) != nil {
					return nil
				}
				return data
			}
		}
		parent := filepath.Dir(current)
		if current == home || parent == current {
			return nil
		}
		current = parent
	}
}

var blockComment = regexp.MustCompile(`(?s)/\*.*?\*/`)

// stripJSONComments is ariarc._strip_comments: /* */ blocks, then // to the
// end of the line unless it follows ":" or "https" (a URL).
func stripJSONComments(text string) string {
	text = blockComment.ReplaceAllString(text, "")
	var out strings.Builder
	for i := 0; i < len(text); {
		if strings.HasPrefix(text[i:], "//") && !(i > 0 && text[i-1] == ':') && !strings.HasSuffix(text[:i], "https") {
			end := strings.IndexByte(text[i:], '\n')
			if end < 0 {
				break
			}
			i += end
			continue
		}
		out.WriteByte(text[i])
		i++
	}
	return out.String()
}

// ── the gate ────────────────────────────────────────────────────────────────

type checkRunner func(ctx context.Context, command string) map[string]any

type acceptanceGate struct {
	root        string
	maxAttempts int
	commands    []string
	attempts    int
	reports     []acceptanceReport
	pending     []string
	allPaths    []string
	// baseline reruns a red check on the code before the change, so a
	// pre-existing failure is not sent back to be repaired; nil when off.
	baseline *testBaseline
}

// newAcceptanceGate is runtime_bridge.build_acceptance_gate: on unless
// acceptance_gate is false or the session is read-only.
func newAcceptanceGate(cfg config, workspace string) *acceptanceGate {
	if !cfg.boolean("acceptance_gate", true) || cfg.text("permission_mode", "workspace-write") == "read-only" {
		return nil
	}
	attempts, ok := pyInt(cfg["acceptance_max_attempts"])
	if !ok || attempts == 0 {
		attempts = 2
	}
	return &acceptanceGate{root: workspace, maxAttempts: max(0, attempts), commands: declaredAcceptanceCommands(cfg, workspace)}
}

func (g *acceptanceGate) record(paths ...string) {
	for _, p := range paths {
		g.pending = appendNew(g.pending, p)
		g.allPaths = appendNew(g.allPaths, p)
	}
}

func (g *acceptanceGate) armed() bool     { return len(g.pending) > 0 }
func (g *acceptanceGate) shouldRun() bool { return g.armed() && g.attempts < g.maxAttempts }

func (g *acceptanceGate) plan(paths []string) ([]string, string) {
	if len(g.commands) > 0 {
		return g.commands, "configured acceptance commands"
	}
	plan := planVerification(g.root, paths)
	return plan.Commands, plan.Reason
}

// run runs the checks once, stopping at the first red: later checks usually
// fail for the same cause and each failing log costs context. It disarms
// first, so a repair that writes a file re-arms it and one that does not
// cannot re-fire the same checks forever.
func (g *acceptanceGate) run(ctx context.Context, runner checkRunner) *acceptanceReport {
	if !g.shouldRun() {
		return nil
	}
	paths := g.pending
	g.pending = nil
	g.attempts++
	commands, reason := g.plan(paths)
	var checks []checkResult
	for _, command := range commands {
		started := time.Now()
		code, output, errText := readRunResult(runner(ctx, command))
		check := checkResult{Command: command, ExitCode: code, Passed: code == 0 && errText == "", Output: output,
			Error: errText, Duration: time.Since(started).Seconds()}
		checks = append(checks, check)
		if !check.Passed {
			break
		}
	}
	if g.baseline != nil {
		for i, check := range checks {
			if check.Passed {
				continue
			}
			comparison := compareBaseline(check.Output, true, g.baseline.result(ctx, check.Command))
			checks[i].Preexisting, checks[i].NewFailures = comparison.Preexisting, comparison.NewFailures
			checks[i].OldFailures, checks[i].BaselineNote = comparison.OldFailures, comparison.Note
		}
	}
	report := acceptanceReport{Attempt: g.attempts, Checks: checks, Reason: reason, Paths: paths,
		AttemptsRemaining: max(0, g.maxAttempts-g.attempts)}
	g.reports = append(g.reports, report)
	return &report
}

// summary is AcceptanceGate.summary; verified is true, false, or absent when
// nothing ran.
func (g *acceptanceGate) summary() map[string]any {
	reports := make([]any, len(g.reports))
	for i, r := range g.reports {
		reports[i] = r.summary()
	}
	out := map[string]any{"verified": nil, "regressions": nil, "attempts": g.attempts, "paths": anySlice(orEmpty(g.allPaths)),
		"reports": reports, "pending_paths": anySlice(orEmpty(g.pending)), "headline": ""}
	if len(g.reports) > 0 {
		last := g.reports[len(g.reports)-1]
		if last.ran() {
			out["verified"] = last.passed() && !g.armed()
			if !last.passed() {
				out["regressions"] = last.regressed()
			}
		}
		out["headline"] = last.headline()
	}
	if g.armed() {
		out["headline"] = "改动后尚未重新验收"
	}
	return out
}

// acceptanceCommand answers `aria-runtime acceptance` for the parity tests:
// {"plan": {"root", "paths"}}, {"results": [...]}, {"reports": [...]},
// {"texts": [...]} for trimming and anchors, {"ariarc": "<dir>"} and
// {"jsonc": [...]}.
func acceptanceCommand(stdin io.Reader, stdout, stderr io.Writer) int {
	var request struct {
		Plan *struct {
			Root  string   `json:"root"`
			Paths []string `json:"paths"`
		} `json:"plan"`
		Results []map[string]any `json:"results"`
		Reports []struct {
			Attempt           int    `json:"attempt"`
			AttemptsRemaining int    `json:"attempts_remaining"`
			Reason            string `json:"reason"`
			Paths             []string
			Checks            []struct {
				Command     string   `json:"command"`
				ExitCode    int      `json:"exit_code"`
				Passed      bool     `json:"passed"`
				Output      string   `json:"output"`
				Error       string   `json:"error"`
				Preexisting bool     `json:"preexisting"`
				NewFailures []string `json:"new_failures"`
				OldFailures []string `json:"old_failures"`
				Note        string   `json:"baseline_note"`
			} `json:"checks"`
		} `json:"reports"`
		Compare []struct {
			After  string `json:"after_output"`
			Failed bool   `json:"after_failed"`
			Before *[]any `json:"before"`
		} `json:"compare"`
		Keep   []string `json:"keep"`
		Texts  []string `json:"texts"`
		Ariarc *string  `json:"ariarc"`
		JSONC  []string `json:"jsonc"`
	}
	if err := json.NewDecoder(io.LimitReader(stdin, 4<<20)).Decode(&request); err != nil {
		fmt.Fprintln(stderr, "acceptance: expected a JSON request:", err)
		return 2
	}
	out := map[string]any{}
	if request.Plan != nil {
		out["plan"] = planVerification(request.Plan.Root, request.Plan.Paths)
	}
	results := []any{}
	for _, raw := range request.Results {
		code, output, errText := readRunResult(raw)
		results = append(results, []any{code, output, errText})
	}
	out["results"] = results
	reports := []any{}
	for _, r := range request.Reports {
		report := acceptanceReport{Attempt: r.Attempt, AttemptsRemaining: r.AttemptsRemaining, Reason: r.Reason, Paths: r.Paths}
		for _, c := range r.Checks {
			report.Checks = append(report.Checks, checkResult{Command: c.Command, ExitCode: c.ExitCode, Passed: c.Passed, Output: c.Output, Error: c.Error,
				Preexisting: c.Preexisting, NewFailures: c.NewFailures, OldFailures: c.OldFailures, BaselineNote: c.Note})
		}
		summary := report.summary()
		for _, c := range asSlice(summary["checks"]) {
			delete(c.(map[string]any), "duration")
		}
		reports = append(reports, map[string]any{"headline": report.headline(), "directive": report.repairDirective(), "summary": summary})
	}
	out["reports"] = reports
	texts := []any{}
	for _, t := range request.Texts {
		texts = append(texts, map[string]any{"trim": trimOutput(t), "anchors": anySlice(orEmpty(failureAnchors(t)))})
	}
	out["texts"] = texts
	comparisons := []any{}
	for _, c := range request.Compare {
		var before *baselineRun
		if c.Before != nil {
			code, _ := pyInt((*c.Before)[0])
			output, _ := (*c.Before)[1].(string)
			before = &baselineRun{code: code, output: output}
		}
		comparisons = append(comparisons, compareBaseline(c.After, c.Failed, before))
	}
	out["compare"] = comparisons
	kept := []any{}
	for _, command := range request.Keep {
		kept = append(kept, keepCollecting(command))
	}
	out["keep"] = kept
	if request.Ariarc != nil {
		out["ariarc"] = declaredAcceptanceCommands(config{}, *request.Ariarc)
	}
	jsonc := []any{}
	for _, t := range request.JSONC {
		jsonc = append(jsonc, stripJSONComments(t))
	}
	out["jsonc"] = jsonc
	if err := json.NewEncoder(stdout).Encode(out); err != nil {
		return 2
	}
	return 0
}
