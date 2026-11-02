package main

// /review and `aria-runtime code-review`: a port of
// src/aria_code/review_service.py. One named target (uncommitted changes with
// the contents of untracked files, staged changes, a branch's merge base, one
// commit, or one file), one isolated reviewer (the rubric, the project's own
// rules and the change; no history, no tools), structured findings with a
// priority, a confidence and a line range, and an overall verdict. A change
// too large for one review is cut at a file boundary and the reviewer is told
// which files it did not see.
//
// The deterministic pattern hints of agents/code_review.py are not ported;
// they were leads for the reviewer, not findings.

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"google.golang.org/genai"
)

const (
	reviewMaxChars        = 60000
	reviewMaxUntracked    = 40
	reviewMaxUntrackedLen = 200000
	reviewMaxRuleChars    = 8000
)

var reviewRuleFiles = []string{"AGENTS.override.md", "AGENTS.md", "ARIA.md"}

type reviewTarget struct {
	kind  string // uncommitted | staged | base | commit | file
	value string
}

func (t reviewTarget) describe() string {
	switch t.kind {
	case "staged":
		return "staged changes"
	case "base":
		return "changes against " + t.value
	case "commit":
		return "commit " + t.value
	case "file":
		return "file " + t.value
	}
	return "uncommitted changes (staged, unstaged and untracked)"
}

// reviewTargetFromArgs: --staged | --base BRANCH | --commit SHA | PATH | nothing.
func reviewTargetFromArgs(args []string) (reviewTarget, error) {
	if len(args) == 0 {
		return reviewTarget{kind: "uncommitted"}, nil
	}
	head := args[0]
	switch head {
	case "--staged", "--cached":
		if len(args) != 1 {
			return reviewTarget{}, errors.New(reviewUsage)
		}
		return reviewTarget{kind: "staged"}, nil
	case "--uncommitted":
		return reviewTarget{kind: "uncommitted"}, nil
	case "--base", "--commit":
		if len(args) != 2 || strings.HasPrefix(args[1], "-") {
			return reviewTarget{}, fmt.Errorf("%s needs a value\n%s", head, reviewUsage)
		}
		return reviewTarget{kind: head[2:], value: args[1]}, nil
	}
	if strings.HasPrefix(head, "-") || len(args) != 1 {
		return reviewTarget{}, errors.New(reviewUsage)
	}
	return reviewTarget{kind: "file", value: head}, nil
}

const reviewUsage = "usage: /review [--staged | --base BRANCH | --commit SHA | PATH]"

type reviewRange struct{ start, end int }

type codeReviewInput struct {
	target      reviewTarget
	root        string
	diff        string
	files       []string
	omitted     []string
	added       int
	removed     int
	baseSHA     string
	commitTitle string
	hunks       map[string][]reviewRange
}

var (
	reviewFileHeader = regexp.MustCompile(`(?m)^diff --git a/(.+?) b/(.+)$`)
	reviewHunk       = regexp.MustCompile(`(?m)^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@`)
)

type reviewSection struct{ name, text string }

func splitReviewFiles(diff string) []reviewSection {
	matches := reviewFileHeader.FindAllStringSubmatchIndex(diff, -1)
	out := make([]reviewSection, 0, len(matches))
	for i, m := range matches {
		end := len(diff)
		if i+1 < len(matches) {
			end = matches[i+1][0]
		}
		out = append(out, reviewSection{name: diff[m[4]:m[5]], text: diff[m[0]:end]})
	}
	return out
}

func reviewHunks(section string) []reviewRange {
	var out []reviewRange
	for _, m := range reviewHunk.FindAllStringSubmatch(section, -1) {
		start, _ := strconv.Atoi(m[1])
		count := 1
		if m[2] != "" {
			count, _ = strconv.Atoi(m[2])
		}
		if count > 0 {
			out = append(out, reviewRange{start, start + count - 1})
		}
	}
	return out
}

// fitReview keeps whole files until the budget is spent; the first file is
// always kept so a single huge file is still reviewed.
func fitReview(target reviewTarget, root, diff string, maxChars int) codeReviewInput {
	in := codeReviewInput{target: target, root: root, hunks: map[string][]reviewRange{}}
	var kept strings.Builder
	for _, sec := range splitReviewFiles(diff) {
		if kept.Len()+len(sec.text) > maxChars && kept.Len() > 0 {
			in.omitted = append(in.omitted, sec.name)
			continue
		}
		kept.WriteString(sec.text)
		in.files = append(in.files, sec.name)
		in.hunks[sec.name] = reviewHunks(sec.text)
	}
	in.diff = kept.String()
	for _, line := range strings.Split(in.diff, "\n") {
		switch {
		case strings.HasPrefix(line, "+") && !strings.HasPrefix(line, "+++"):
			in.added++
		case strings.HasPrefix(line, "-") && !strings.HasPrefix(line, "---"):
			in.removed++
		}
	}
	return in
}

func reviewGit(ctx context.Context, dir string, args ...string) (string, error) {
	out, err := git(ctx, dir, nil, time.Minute, args...)
	if err != nil {
		return "", fmt.Errorf("git %s failed: %w", strings.Join(args, " "), err)
	}
	if out != "" {
		out += "\n"
	}
	return out, nil
}

func untrackedAsDiff(ctx context.Context, root string) string {
	list, err := git(ctx, root, nil, time.Minute, "ls-files", "--others", "--exclude-standard", "-z")
	if err != nil {
		return ""
	}
	var names []string
	for _, name := range strings.Split(list, "\x00") {
		if name != "" {
			names = append(names, name)
		}
	}
	var b strings.Builder
	for i, name := range names {
		if i == reviewMaxUntracked {
			fmt.Fprintf(&b, "# %d more untracked files not shown\n", len(names)-reviewMaxUntracked)
			break
		}
		data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(name)))
		if err != nil {
			continue
		}
		head := data
		if len(head) > 8000 {
			head = head[:8000]
		}
		if strings.IndexByte(string(head), 0) >= 0 || len(data) > reviewMaxUntrackedLen {
			fmt.Fprintf(&b, "diff --git a/%s b/%s\nnew file (binary or larger than %d bytes; not shown)\n", name, name, reviewMaxUntrackedLen)
			continue
		}
		lines := strings.Split(strings.TrimRight(strings.ReplaceAll(string(data), "\r\n", "\n"), "\n"), "\n")
		if len(data) == 0 {
			lines = nil
		}
		fmt.Fprintf(&b, "diff --git a/%s b/%s\nnew file mode 100644\n--- /dev/null\n+++ b/%s\n@@ -0,0 +1,%d @@\n", name, name, name, len(lines))
		for _, line := range lines {
			b.WriteString("+" + line + "\n")
		}
	}
	return b.String()
}

// collectReview gathers exactly what the target names, as a unified diff.
func collectReview(ctx context.Context, cwd string, target reviewTarget, maxChars int) (codeReviewInput, error) {
	if target.kind == "file" {
		path := target.value
		if !filepath.IsAbs(path) {
			path = filepath.Join(cwd, path)
		}
		info, err := os.Stat(path)
		if err != nil || !info.Mode().IsRegular() {
			return codeReviewInput{}, fmt.Errorf("file not found: %s", target.value)
		}
		root, name := filepath.Dir(path), filepath.Base(path)
		if top, err := git(ctx, filepath.Dir(path), nil, time.Minute, "rev-parse", "--show-toplevel"); err == nil && top != "" {
			if rel, err := filepath.Rel(top, path); err == nil && !strings.HasPrefix(rel, "..") {
				root, name = top, filepath.ToSlash(rel)
			}
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return codeReviewInput{}, err
		}
		lines := strings.Split(strings.TrimRight(strings.ReplaceAll(string(data), "\r\n", "\n"), "\n"), "\n")
		var b strings.Builder
		fmt.Fprintf(&b, "diff --git a/%s b/%s\n--- a/%s\n+++ b/%s\n@@ -1,%d +1,%d @@\n", name, name, name, name, len(lines), len(lines))
		for _, line := range lines {
			b.WriteString(" " + line + "\n")
		}
		return fitReview(target, root, b.String(), maxChars), nil
	}
	root, err := git(ctx, cwd, nil, time.Minute, "rev-parse", "--show-toplevel")
	if err != nil || root == "" {
		return codeReviewInput{}, fmt.Errorf("%s is not inside a git repository", cwd)
	}
	switch target.kind {
	case "uncommitted":
		tracked, err := reviewGit(ctx, root, "diff", "--no-ext-diff", "HEAD")
		if _, headErr := git(ctx, root, nil, time.Minute, "rev-parse", "--verify", "-q", "HEAD"); headErr != nil {
			tracked, err = reviewGit(ctx, root, "diff", "--no-ext-diff", "--cached")
		}
		if err != nil {
			return codeReviewInput{}, err
		}
		return fitReview(target, root, tracked+untrackedAsDiff(ctx, root), maxChars), nil
	case "staged":
		diff, err := reviewGit(ctx, root, "diff", "--no-ext-diff", "--cached")
		if err != nil {
			return codeReviewInput{}, err
		}
		return fitReview(target, root, diff, maxChars), nil
	case "base":
		upstream, _ := git(ctx, root, nil, time.Minute, "rev-parse", "--abbrev-ref", target.value+"@{upstream}")
		var base string
		for _, candidate := range []string{upstream, target.value} {
			if candidate == "" {
				continue
			}
			if sha, err := git(ctx, root, nil, time.Minute, "merge-base", "HEAD", candidate); err == nil && sha != "" {
				base = sha
				break
			}
		}
		if base == "" {
			return codeReviewInput{}, fmt.Errorf("no merge base between HEAD and %s", target.value)
		}
		diff, err := reviewGit(ctx, root, "diff", "--no-ext-diff", base)
		if err != nil {
			return codeReviewInput{}, err
		}
		in := fitReview(target, root, diff, maxChars)
		in.baseSHA = base
		return in, nil
	case "commit":
		sha, err := git(ctx, root, nil, time.Minute, "rev-parse", "--verify", target.value+"^{commit}")
		if err != nil {
			return codeReviewInput{}, fmt.Errorf("unknown commit %s", target.value)
		}
		title, _ := git(ctx, root, nil, time.Minute, "log", "-1", "--format=%s", sha)
		patch, err := reviewGit(ctx, root, "show", "--no-ext-diff", "--format=", "--patch", sha)
		if err != nil {
			return codeReviewInput{}, err
		}
		in := fitReview(target, root, patch, maxChars)
		in.baseSHA, in.commitTitle = sha, title
		return in, nil
	}
	return codeReviewInput{}, fmt.Errorf("unknown review target %q", target.kind)
}

// projectReviewRules is the project's own guidance, most specific file first.
func projectReviewRules(root string) string {
	for _, name := range reviewRuleFiles {
		data, err := os.ReadFile(filepath.Join(root, name))
		if err != nil {
			continue
		}
		if text := strings.TrimSpace(string(data)); text != "" {
			return name + ":\n" + capText(text, reviewMaxRuleChars)
		}
	}
	return ""
}

const reviewRubric = `You review one code change for the engineer who wrote it. Report only problems they would want to fix before merging.

What counts as a finding:
- It changes behaviour for the worse: a wrong result, a crash, lost or leaked data, a security hole, a serious slowdown, or code that will clearly be hard to maintain.
- It was introduced by this change. Problems that were already there are out of scope.
- It is specific: one problem, one place, one fix. Say which inputs, settings or environments trigger it; if it depends on them, say so in the first sentence.
- It is shown, not guessed. "This might affect X" is not a finding unless you can point to the code in X that breaks.
- It is not an obvious, deliberate choice by the author.

What does not: style, naming, formatting, typos and missing comments, unless a documented project rule requires them.

Priorities, at the start of each title:
[P0] blocks a release or breaks the main path for everyone, whatever the input.
[P1] urgent: fix in the next cycle.
[P2] normal: fix when convenient.
[P3] minor.

Each finding: a title of at most 80 characters that names the problem; a body of one short paragraph saying why it is wrong and when it happens; at most three lines of code, in backticks; the narrowest line range (new-side line numbers of the change, no more than about ten lines); and a confidence from 0 to 1. List every qualifying problem — and none when there are none. A short empty review is better than padding.

Then give an overall verdict: "patch is correct" when existing code and tests will keep working and nothing above P3 is wrong, otherwise "patch is incorrect", with a one-to-three sentence explanation and a confidence.

Respond with JSON only, no prose and no code fences, in exactly this shape:
{"findings": [{"title": "[P1] ...", "body": "...", "confidence_score": 0.8, "priority": 1,
  "code_location": {"file_path": "path/relative/to/repo", "line_range": {"start": 10, "end": 12}}}],
 "overall_correctness": "patch is correct" | "patch is incorrect",
 "overall_explanation": "...",
 "overall_confidence_score": 0.8}`

func reviewLangZH(lang string) bool { return strings.HasPrefix(strings.ToLower(lang), "zh") }

// buildCodeReview is the reviewer's whole world: rubric, project rules, the change.
func buildCodeReview(in codeReviewInput, lang, rules string) (system, user string) {
	system = reviewRubric + "\n\n"
	if reviewLangZH(lang) {
		system += "标题、正文和总体说明用简体中文书写；JSON 字段名和优先级标签保持英文。"
	} else {
		system += "Write titles, bodies and the explanation in English."
	}
	if rules != "" {
		system += "\n\nThis project's own guidance follows. It overrides the defaults above where they conflict; when a finding rests on it, cite the file.\n\n" + rules
	}
	parts := []string{"Review target: " + in.target.describe() + "."}
	if in.commitTitle != "" {
		parts = append(parts, fmt.Sprintf("Commit title: %q.", in.commitTitle))
	}
	if in.baseSHA != "" && in.target.kind == "base" {
		parts = append(parts, "Merge base: "+in.baseSHA+".")
	}
	files := strings.Join(in.files, ", ")
	if files == "" {
		files = "(none)"
	}
	parts = append(parts, fmt.Sprintf("Files: %s (+%d -%d).", files, in.added, in.removed))
	if len(in.omitted) > 0 {
		parts = append(parts, "The change was too large to show whole. These files were NOT shown and must not be reviewed or guessed about: "+strings.Join(in.omitted, ", ")+".")
	}
	parts = append(parts, "The change:\n"+in.diff)
	return system, strings.Join(parts, "\n\n")
}

type codeReviewFinding struct {
	Title      string  `json:"title"`
	Body       string  `json:"body"`
	Priority   *int    `json:"priority"`
	Confidence float64 `json:"confidence"`
	FilePath   string  `json:"file_path"`
	Start      int     `json:"start"`
	End        int     `json:"end"`
	InChange   bool    `json:"in_change"`
}

func (f codeReviewFinding) label() string {
	if f.Priority == nil {
		return "P?"
	}
	return fmt.Sprintf("P%d", *f.Priority)
}

func (f codeReviewFinding) location() string {
	if f.End != f.Start {
		return fmt.Sprintf("%s:%d-%d", f.FilePath, f.Start, f.End)
	}
	return fmt.Sprintf("%s:%d", f.FilePath, f.Start)
}

type codeReviewResult struct {
	findings    []codeReviewFinding
	correct     *bool
	explanation string
	confidence  float64
	structured  bool
	target      string
	files       []string
	omitted     []string
}

func (r codeReviewResult) worstPriority() *int {
	var worst *int
	for _, f := range r.findings {
		if f.Priority != nil && f.InChange && (worst == nil || *f.Priority < *worst) {
			p := *f.Priority
			worst = &p
		}
	}
	return worst
}

func (r codeReviewResult) asDict() map[string]any {
	var verdict any
	if r.correct != nil {
		verdict = map[bool]string{true: "patch is correct", false: "patch is incorrect"}[*r.correct]
	}
	findings := make([]map[string]any, 0, len(r.findings))
	for _, f := range r.findings {
		var priority any
		if f.Priority != nil {
			priority = *f.Priority
		}
		findings = append(findings, map[string]any{"title": f.Title, "body": f.Body, "priority": priority,
			"priority_label": f.label(), "confidence": f.Confidence, "file_path": f.FilePath,
			"start": f.Start, "end": f.End, "in_change": f.InChange})
	}
	return map[string]any{"target": r.target, "files": nonNil(r.files), "omitted_files": nonNil(r.omitted),
		"overall_correctness": verdict, "overall_explanation": r.explanation,
		"overall_confidence_score": r.confidence, "structured": r.structured, "findings": findings}
}

func nonNil(values []string) []string {
	if values == nil {
		return []string{}
	}
	return values
}

func clampUnit(value any, fallback float64) float64 {
	var number float64
	switch v := value.(type) {
	case float64:
		number = v
	case string:
		parsed, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
		if err != nil {
			return fallback
		}
		number = parsed
	default:
		return fallback
	}
	if math.IsNaN(number) {
		return fallback
	}
	return math.Max(0, math.Min(1, number))
}

var (
	reviewTitlePriority = regexp.MustCompile(`^\s*\[P([0-3])\]`)
	reviewTitlePrefix   = regexp.MustCompile(`^\s*\[P[0-3?]\]\s*`)
	reviewFenced        = regexp.MustCompile("(?s)```(?:json)?\\s*(\\{.*\\})\\s*```")
)

func reviewJSON(text string) map[string]any {
	text = strings.TrimSpace(text)
	candidates := []string{text}
	if m := reviewFenced.FindStringSubmatch(text); m != nil {
		candidates = append(candidates, m[1])
	}
	if i, j := strings.Index(text, "{"), strings.LastIndex(text, "}"); i >= 0 && j > i {
		candidates = append(candidates, text[i:j+1])
	}
	for _, c := range candidates {
		var data map[string]any
		if json.Unmarshal([]byte(c), &data) == nil && data != nil {
			return data
		}
	}
	return nil
}

func reviewPriority(raw map[string]any) *int {
	if v, ok := raw["priority"].(float64); ok && v >= 0 && v <= 3 {
		p := int(v)
		return &p
	}
	title, _ := raw["title"].(string)
	if m := reviewTitlePriority.FindStringSubmatch(title); m != nil {
		p, _ := strconv.Atoi(m[1])
		return &p
	}
	return nil
}

func reviewInt(value any) (int, bool) {
	switch v := value.(type) {
	case float64:
		return int(v), true
	case string:
		n, err := strconv.Atoi(strings.TrimSpace(v))
		return n, err == nil
	}
	return 0, false
}

// parseCodeReview never fails: a reply that is not the JSON asked for keeps
// its text as the explanation and is marked unstructured.
func parseCodeReview(text string, in *codeReviewInput) codeReviewResult {
	data := reviewJSON(text)
	if data == nil {
		explanation := strings.TrimSpace(text)
		if explanation == "" {
			explanation = "The reviewer returned nothing."
		}
		r := codeReviewResult{explanation: explanation}
		if in != nil {
			r.target, r.files, r.omitted = in.target.describe(), in.files, in.omitted
		}
		return r
	}
	var findings []codeReviewFinding
	raws, _ := data["findings"].([]any)
	for _, item := range raws {
		raw, ok := item.(map[string]any)
		if !ok {
			continue
		}
		location, _ := raw["code_location"].(map[string]any)
		lines, _ := location["line_range"].(map[string]any)
		path, _ := location["file_path"].(string)
		if path == "" {
			path, _ = location["absolute_file_path"].(string)
		}
		path = strings.TrimSpace(path)
		if in != nil && filepath.IsAbs(path) {
			if rel, err := filepath.Rel(in.root, path); err == nil && !strings.HasPrefix(rel, "..") {
				path = filepath.ToSlash(rel)
			}
		}
		start, okStart := reviewInt(lines["start"])
		end, okEnd := reviewInt(lines["end"])
		if !okStart {
			start = 0
		}
		if !okEnd || end == 0 {
			end = start
		}
		if start > end {
			start, end = end, start
		}
		title, _ := raw["title"].(string)
		title = reviewTitlePrefix.ReplaceAllString(strings.TrimSpace(title), "")
		body, _ := raw["body"].(string)
		f := codeReviewFinding{Title: capRunes(title, 120), Body: strings.TrimSpace(body), Priority: reviewPriority(raw),
			Confidence: clampUnit(raw["confidence_score"], 0.5), FilePath: path, Start: start, End: end, InChange: true}
		if in != nil {
			f.InChange = false
			for _, r := range in.hunks[path] {
				if start <= r.end && end >= r.start {
					f.InChange = true
					break
				}
			}
		}
		findings = append(findings, f)
	}
	sort.SliceStable(findings, func(i, j int) bool {
		a, b := findings[i], findings[j]
		if a.InChange != b.InChange {
			return a.InChange
		}
		pa, pb := 9, 9
		if a.Priority != nil {
			pa = *a.Priority
		}
		if b.Priority != nil {
			pb = *b.Priority
		}
		if pa != pb {
			return pa < pb
		}
		return a.Confidence > b.Confidence
	})
	r := codeReviewResult{findings: findings, structured: true, confidence: clampUnit(data["overall_confidence_score"], 0)}
	verdict, _ := data["overall_correctness"].(string)
	switch strings.ToLower(strings.TrimSpace(verdict)) {
	case "patch is correct":
		t := true
		r.correct = &t
	case "patch is incorrect":
		f := false
		r.correct = &f
	}
	explanation, _ := data["overall_explanation"].(string)
	r.explanation = strings.TrimSpace(explanation)
	if in != nil {
		r.target, r.files, r.omitted = in.target.describe(), in.files, in.omitted
	}
	return r
}

func capRunes(text string, limit int) string {
	if runes := []rune(text); len(runes) > limit {
		return string(runes[:limit])
	}
	return text
}

func renderCodeReview(r codeReviewResult, lang string) string {
	t := map[string]string{"title": "Review", "correct": "Looks correct", "incorrect": "Needs changes",
		"unknown": "No verdict", "none": "No findings.", "outside": "Outside this change",
		"omitted": "Not reviewed (too large to include)", "confidence": "confidence",
		"unstructured": "The reviewer did not answer in the expected format; its reply:"}
	if reviewLangZH(lang) {
		t = map[string]string{"title": "审查", "correct": "可以合并", "incorrect": "需要修改", "unknown": "无结论",
			"none": "没有发现问题。", "outside": "不在本次改动范围内", "omitted": "未审查（改动过大未纳入）",
			"confidence": "置信度", "unstructured": "审查模型未按约定格式回答，原文如下："}
	}
	head := t["title"]
	if r.target != "" {
		head += " · " + r.target
	}
	lines := []string{head}
	if !r.structured {
		return strings.Join(append(lines, t["unstructured"], r.explanation), "\n")
	}
	verdict := t["unknown"]
	if r.correct != nil {
		verdict = map[bool]string{true: t["correct"], false: t["incorrect"]}[*r.correct]
	}
	lines = append(lines, fmt.Sprintf("%s · %s %.2f", verdict, t["confidence"], r.confidence))
	if r.explanation != "" {
		lines = append(lines, r.explanation)
	}
	if len(r.findings) == 0 {
		lines = append(lines, t["none"])
	}
	outside := false
	for i, f := range r.findings {
		if !f.InChange && !outside {
			lines = append(lines, "— "+t["outside"]+" —")
			outside = true
		}
		lines = append(lines, "", fmt.Sprintf("%d. [%s] %s  %s  (%.2f)", i+1, f.label(), f.Title, f.location(), f.Confidence))
		for _, line := range strings.Split(f.Body, "\n") {
			if strings.TrimSpace(line) != "" {
				lines = append(lines, "   "+line)
			}
		}
	}
	if len(r.omitted) > 0 {
		lines = append(lines, "", t["omitted"]+": "+strings.Join(r.omitted, ", "))
	}
	return strings.Join(lines, "\n")
}

// reviewHistoryNote is what the conversation remembers, so "fix finding 2"
// means something in the next turn.
func reviewHistoryNote(r codeReviewResult) string {
	verdict := "no verdict"
	if r.correct != nil {
		verdict = map[bool]string{true: "patch is correct", false: "patch is incorrect"}[*r.correct]
	}
	row := "Code review of " + r.target + ": " + verdict
	if r.explanation != "" {
		row += ". " + r.explanation
	}
	rows := []string{row}
	for i, f := range r.findings {
		rows = append(rows, fmt.Sprintf("%d. [%s] %s — %s: %s", i+1, f.label(), f.Title, f.location(), f.Body))
	}
	return strings.Join(rows, "\n")
}

// runCodeReview asks one isolated model call: no history, no tools.
func runCodeReview(ctx context.Context, provider modelProvider, in codeReviewInput, lang string) (codeReviewResult, error) {
	if strings.TrimSpace(in.diff) == "" {
		t := true
		return codeReviewResult{correct: &t, explanation: "Nothing to review: the target has no changes.", confidence: 1,
			structured: true, target: in.target.describe()}, nil
	}
	system, user := buildCodeReview(in, lang, projectReviewRules(in.root))
	reply, err := provider.Generate(ctx, []*genai.Content{genai.NewContentFromText(user, genai.RoleUser)}, system, nil, func(string) {})
	if err != nil {
		return codeReviewResult{}, err
	}
	return parseCodeReview(contentText(reply), &in), nil
}

// reviewProvider is review_model when set, otherwise the session's model.
func reviewProvider(cfg config, current modelProvider) (modelProvider, error) {
	model := strings.TrimSpace(cfg.text("review_model", ""))
	if model == "" && current != nil {
		return current, nil
	}
	next := config{}
	for key, value := range cfg {
		next[key] = value
	}
	if model != "" {
		next["model"] = model
	}
	return modelProviderFor(next)
}

func parseReviewPriority(value string) (int, error) {
	value = strings.ToUpper(strings.TrimSpace(value))
	if len(value) == 2 && value[0] == 'P' && value[1] >= '0' && value[1] <= '3' {
		return int(value[1] - '0'), nil
	}
	return 0, errors.New("priority must be one of P0, P1, P2, P3")
}

// codeReviewCommand answers `aria-runtime code-review`, the headless review
// for CI. Exit 0: reviewed, nothing at or above --fail-on. 1: a finding at
// or above it. 2: the review could not run, or the reply was not a
// structured review while --fail-on was set; a gate never passes on a review
// that did not happen.
func codeReviewCommand(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("code-review", flag.ContinueOnError)
	flags.SetOutput(stderr)
	staged := flags.Bool("staged", false, "review staged changes")
	base := flags.String("base", "", "review everything since the merge base with this branch")
	commit := flags.String("commit", "", "review one commit")
	failOn := flags.String("fail-on", "", "exit 1 when a finding in the change is at or above this priority (P0..P3)")
	asJSON := flags.Bool("json", false, "print the review as JSON")
	model := flags.String("model", "", "model for the reviewer (default: review_model, then model)")
	lang := flags.String("lang", "", "language of the review (en or zh)")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	var targetArgs []string
	switch {
	case *staged:
		targetArgs = []string{"--staged"}
	case *base != "":
		targetArgs = []string{"--base", *base}
	case *commit != "":
		targetArgs = []string{"--commit", *commit}
	}
	targetArgs = append(targetArgs, flags.Args()...)
	target, err := reviewTargetFromArgs(targetArgs)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	}
	threshold := -1
	if *failOn != "" {
		if threshold, err = parseReviewPriority(*failOn); err != nil {
			fmt.Fprintln(stderr, err)
			return 2
		}
	}
	root, err := stateRoot()
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	}
	cfg, err := loadConfig(root)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	}
	if *model != "" {
		cfg["review_model"] = *model
	}
	if *lang == "" {
		*lang = cfg.text("ui_lang", "en")
	}
	provider, err := reviewProvider(cfg, nil)
	if err != nil {
		fmt.Fprintln(stderr, "review model:", err)
		return 2
	}
	cwd, err := os.Getwd()
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	}
	in, err := collectReview(ctx, cwd, target, reviewMaxChars)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	}
	result, err := runCodeReview(ctx, provider, in, *lang)
	if err != nil {
		fmt.Fprintln(stderr, "review failed:", err)
		return 2
	}
	if *asJSON {
		body, _ := json.MarshalIndent(result.asDict(), "", "  ")
		fmt.Fprintln(stdout, string(body))
	} else {
		fmt.Fprintln(stdout, renderCodeReview(result, *lang))
	}
	if threshold < 0 {
		return 0
	}
	if !result.structured {
		return 2
	}
	if worst := result.worstPriority(); worst != nil && *worst <= threshold {
		return 1
	}
	return 0
}
