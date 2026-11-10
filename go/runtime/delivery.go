package main

// The delivery report — a port of src/aria_code/runtime/delivery.py. A coding
// turn ends with what the runtime knows happened, not with the model's own
// account: files changed and by how much, checks and how they came out, calls
// the change contract refused, the riskiest action and the next step. The
// status comes from evidence; a turn that stopped early or whose checks are
// red is INCOMPLETE however its last message reads.
//
// Definitions come from the current file and this turn's diffs. Tests that
// reference them come from a graph cache already on disk. A missing file, a
// diff that does not apply, or no cache leaves those fields empty. The impact
// line uses that same cache and does not scan the tree. The acceptance gate
// is separate: natively, the checks are the verification commands the model
// ran (see checkLedger). tests/test_native_go_delivery.py compares the report
// with Python. Python still parses with its own syntax tree when this answer
// is empty.

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"path/filepath"
	"regexp"
	"strings"

	"google.golang.org/genai"
)

var defaultMutatingTools = set("write_file", "edit_file", "multi_edit", "apply_patch", "notebook_edit", "str_replace", "create_file")

type changedFile struct {
	Path     string   `json:"path"`
	Added    int      `json:"added"`
	Removed  int      `json:"removed"`
	Created  bool     `json:"created"`
	Symbols  []string `json:"symbols"`
	TestedBy []string `json:"tested_by"`
}

func (c *changedFile) mark() string {
	if c.Created {
		return "A"
	}
	return "M"
}

type DeliveryReport struct {
	Status      string
	Changed     []*changedFile
	Checks      []map[string]any
	NotRun      []map[string]any
	Verified    *bool
	Review      string
	ReviewLines []string
	Behaviour   map[string]any
	RiskLevel   *int
	RiskSummary string
	Checkpoints []string
	Refused     []map[string]any
	StopReason  string
	Next        string
	Impact      string
}

func (r DeliveryReport) worthShowing() bool {
	return len(r.Changed) > 0 || len(r.Checks) > 0 || len(r.Refused) > 0
}

func (r DeliveryReport) totals() (added, removed int) {
	for _, c := range r.Changed {
		added += c.Added
		removed += c.Removed
	}
	return
}

// asDict is DeliveryReport.as_dict.
func (r DeliveryReport) asDict() map[string]any {
	var risk any
	if r.RiskLevel != nil {
		risk = map[string]any{"level": *r.RiskLevel, "name": levelNames[*r.RiskLevel], "summary": r.RiskSummary}
	}
	var verified any
	if r.Verified != nil {
		verified = *r.Verified
	}
	var behaviour any
	if len(r.Behaviour) > 0 {
		behaviour = r.Behaviour
	}
	changed := make([]*changedFile, 0, len(r.Changed))
	for _, c := range r.Changed {
		copied := *c
		copied.Symbols, copied.TestedBy = orEmpty(c.Symbols), orEmpty(c.TestedBy)
		changed = append(changed, &copied)
	}
	out := map[string]any{
		"status": r.Status, "changed": changed, "checks": orEmptyMaps(r.Checks), "verified": verified,
		"review": r.Review, "review_lines": orEmpty(r.ReviewLines), "behaviour": behaviour, "risk": risk,
		"checkpoints": orEmpty(r.Checkpoints), "refused": orEmptyMaps(r.Refused), "stop_reason": r.StopReason,
		"next": r.Next, "impact": r.Impact,
	}
	// Omitted when empty so a report with no missing tool stays field-for-field
	// comparable with one that never recorded the case.
	if len(r.NotRun) > 0 {
		out["not_run"] = r.NotRun
	}
	return out
}

func orEmptyMaps(list []map[string]any) []map[string]any {
	if list == nil {
		return []map[string]any{}
	}
	return list
}

// displayPath is delivery._display: relative to root when the path is inside it.
func displayPath(path, root string) string {
	if root == "" {
		return path
	}
	if rel, ok := relativeInside(path, root); ok {
		return rel
	}
	return path
}

// relativeInside is Path(path).resolve().relative_to(Path(root).resolve()):
// a relative path resolves against the working directory, not root.
func relativeInside(path, root string) (string, bool) {
	rel, err := filepath.Rel(resolveLoose(expandUser(root)), resolveLoose(path))
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", false
	}
	return filepath.ToSlash(rel), true
}

func pyValue(value any) string {
	switch v := value.(type) {
	case nil:
		return "None"
	case float64:
		if v == float64(int64(v)) {
			return fmt.Sprint(int64(v))
		}
	}
	return pyStr(value)
}

func truthy(value any) bool { return pyTruthy(value) }

func stringList(value any) []string {
	items, _ := value.([]any)
	var out []string
	for _, item := range items {
		out = append(out, pyStr(item))
	}
	if list, ok := value.([]string); ok {
		return list
	}
	return out
}

// Render is DeliveryReport.render.
func (r DeliveryReport) Render(rewindHint, root string) string {
	lines := []string{strings.ToUpper(r.Status)}
	if len(r.Changed) > 0 {
		width := 0
		for _, c := range r.Changed {
			width = max(width, len([]rune(displayPath(c.Path, root))))
		}
		lines = append(lines, "", "Changed")
		for _, c := range r.Changed {
			stats := fmt.Sprintf("+%d", c.Added)
			if c.Removed != 0 || !c.Created {
				stats += fmt.Sprintf(" -%d", c.Removed)
			}
			lines = append(lines, fmt.Sprintf("  %s %-*s  %s", c.mark(), width, displayPath(c.Path, root), stats))
			if len(c.Symbols) > 0 {
				shown := strings.Join(c.Symbols[:min(4, len(c.Symbols))], " · ")
				if len(c.Symbols) > 4 {
					shown += fmt.Sprintf(" · +%d", len(c.Symbols)-4)
				}
				lines = append(lines, "      "+shown)
			}
			if len(c.TestedBy) > 0 {
				shown := strings.Join(c.TestedBy[:min(3, len(c.TestedBy))], ", ")
				if len(c.TestedBy) > 3 {
					shown += fmt.Sprintf(" +%d", len(c.TestedBy)-3)
				}
				lines = append(lines, "      tested by "+shown)
			}
		}
		if len(r.Changed) > 1 {
			added, removed := r.totals()
			lines = append(lines, fmt.Sprintf("  %d files · +%d / -%d", len(r.Changed), added, removed))
		}
		if r.Impact != "" {
			lines = append(lines, "  impact "+r.Impact)
		}
	}
	lines = append(lines, "", "Verified")
	if len(r.Checks) > 0 {
		for _, check := range r.Checks {
			ok := truthy(check["passed"])
			tail, mark := "", "✓"
			if !ok {
				mark = "✗"
				tail = fmt.Sprintf("  (exit %s)", pyValue(check["exit_code"]))
				if truthy(check["preexisting"]) {
					tail += " — already failing before this change"
				}
			}
			command, _ := check["command"].(string)
			lines = append(lines, fmt.Sprintf("  %s %s%s", mark, command, tail))
			if failures := stringList(check["new_failures"]); len(failures) > 0 {
				shown := strings.Join(failures[:min(3, len(failures))], ", ")
				if len(failures) > 3 {
					shown += fmt.Sprintf(" … +%d", len(failures)-3)
				}
				lines = append(lines, "      new failures: "+shown)
			}
		}
		if r.Verified == nil && len(r.Changed) > 0 {
			lines = append(lines, "  ⚠ changed again after the last check")
		}
	} else if len(r.NotRun) == 0 && len(r.Changed) > 0 {
		lines = append(lines, "  — no check ran (none could be inferred)")
	} else if len(r.NotRun) == 0 {
		lines = append(lines, "  — no check ran")
	}
	for _, item := range r.NotRun {
		lines = append(lines, fmt.Sprintf("  · not run: %s — %s", pyStr(item["command"]), pyStr(item["reason"])))
	}
	if len(r.Behaviour) > 0 {
		lines = append(lines, "", "Behaviour")
		for _, key := range []string{"before", "after", "why", "impact"} {
			if truthy(r.Behaviour[key]) {
				lines = append(lines, fmt.Sprintf("  %-7s%s", strings.ToUpper(key[:1])+key[1:], pyStr(r.Behaviour[key])))
			}
		}
	}
	lines = append(lines, "", "Review")
	review := r.ReviewLines
	if len(review) == 0 {
		review = []string{r.Review}
	}
	for _, line := range review {
		lines = append(lines, "  "+line)
	}
	if r.RiskLevel != nil {
		risk := fmt.Sprintf("  L%d %s", *r.RiskLevel, levelNames[*r.RiskLevel])
		if r.RiskSummary != "" {
			risk += " · " + r.RiskSummary
		}
		lines = append(lines, "", "Risk", risk)
	}
	if len(r.Refused) > 0 {
		lines = append(lines, "", "Contract")
		for _, item := range r.Refused {
			lines = append(lines, fmt.Sprintf("  × %s: %s", pyStr(orNone(item["tool"])), pyStr(orNone(item["reason"]))))
		}
	}
	if len(r.Checkpoints) > 0 {
		label := fmt.Sprintf("%d checkpoint", len(r.Checkpoints))
		if len(r.Checkpoints) != 1 {
			label += "s"
		}
		if rewindHint != "" {
			label += " · " + rewindHint
		}
		lines = append(lines, "", "Checkpoint", "  "+label)
	}
	if r.Next != "" {
		lines = append(lines, "", "Next", "  "+r.Next)
	}
	return strings.Join(lines, "\n")
}

// orNone stands in for dict.get(key, ""): a missing key renders as "".
func orNone(value any) any {
	if value == nil {
		return ""
	}
	return value
}

var testPath = regexp.MustCompile(`(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$`)

func isTestPath(path string) bool {
	return testPath.MatchString(strings.ReplaceAll(path, `\`, "/"))
}

// diffStats is delivery._diff_stats.
func diffStats(diff string) (added, removed int, created bool) {
	for _, line := range pySplitLines(diff) {
		if strings.HasPrefix(line, "+++") || strings.HasPrefix(line, "---") {
			if strings.HasPrefix(line, "--- /dev/null") {
				created = true
			}
			continue
		}
		if strings.HasPrefix(line, "+") {
			added++
		} else if strings.HasPrefix(line, "-") {
			removed++
		}
	}
	return
}

// pySplitLines is str.splitlines() for the separators diffs use.
func pySplitLines(text string) []string {
	text = strings.NewReplacer("\r\n", "\n", "\r", "\n").Replace(text)
	text = strings.TrimSuffix(text, "\n")
	if text == "" {
		return nil
	}
	return strings.Split(text, "\n")
}

func asMap(value any) map[string]any {
	m, _ := value.(map[string]any)
	return m
}

func resultData(result map[string]any) map[string]any {
	if data := asMap(result["data"]); len(data) > 0 {
		return data
	}
	return result
}

// mutatedPaths is acceptance.extract_mutated_paths: paths a tool result
// actually wrote to disk.
func mutatedPaths(tool string, result map[string]any) []string {
	canonical := tool
	if i := strings.LastIndex(tool, "__"); i >= 0 {
		canonical = tool[i+2:]
	}
	if !defaultMutatingTools[canonical] || result == nil {
		return nil
	}
	if success, ok := result["success"].(bool); (ok && !success) || truthy(result["error"]) {
		return nil
	}
	data := resultData(result)
	if applied, ok := data["applied"].(bool); ok && !applied {
		return nil
	}
	if staged, ok := data["staged"].(bool); ok && staged && data["applied"] != true {
		return nil
	}
	var found []string
	add := func(value any) {
		if !truthy(value) {
			return
		}
		if text := strings.TrimSpace(pyStr(value)); text != "" {
			found = appendNew(found, text)
		}
	}
	add(data["path"])
	add(data["file_path"])
	if paths, ok := data["paths"].([]any); ok {
		for _, p := range paths {
			add(p)
		}
	}
	if edits, ok := data["edits"].([]any); ok {
		for _, e := range edits {
			add(asMap(e)["path"])
		}
	}
	return found
}

// DeliveryLedger records what a turn did as it happens.
type DeliveryLedger struct {
	root        string
	order       []string
	changed     map[string]*changedFile
	checkpoints []string
	diffs       []string
	fileDiffs   map[string][]string
	risk        *RiskAssessment
}

func newDeliveryLedger(root string) *DeliveryLedger {
	return &DeliveryLedger{root: root, changed: map[string]*changedFile{}}
}

// Record is DeliveryLedger.record for one tool call and its result.
func (l *DeliveryLedger) Record(tool string, params, result map[string]any) {
	if result != nil && truthy(result["contract_violation"]) {
		return
	}
	if success, ok := result["success"].(bool); result != nil && (!ok || success) {
		a := AssessTool(tool, params, l.root)
		if l.risk == nil || a.Level > l.risk.Level {
			l.risk = &a
		}
	}
	paths := mutatedPaths(tool, result)
	if len(paths) == 0 {
		return
	}
	data := resultData(result)
	diff := ""
	if data["diff"] != nil {
		diff = pyStr(data["diff"])
	}
	if strings.TrimSpace(diff) != "" {
		if !strings.HasSuffix(diff, "\n") {
			diff += "\n"
		}
		l.diffs = append(l.diffs, diff)
	}
	added, removed, created := diffStats(diff)
	action := ""
	if data["action"] != nil {
		action = strings.ToLower(pyStr(data["action"]))
	}
	created = created || action == "created" || action == "create" || action == "write new file"
	for i, path := range paths {
		entry := l.changed[path]
		if entry == nil {
			entry = &changedFile{Path: path, Created: created}
			l.changed[path] = entry
			l.order = append(l.order, path)
		}
		if i == 0 { // one diff per call; attribute it to the first path
			entry.Added += added
			entry.Removed += removed
			if strings.TrimSpace(diff) != "" {
				if l.fileDiffs == nil {
					l.fileDiffs = map[string][]string{}
				}
				l.fileDiffs[path] = append(l.fileDiffs[path], diff)
			}
		}
	}
	if checkpoint := data["checkpoint_id"]; truthy(checkpoint) {
		l.checkpoints = appendNew(l.checkpoints, pyStr(checkpoint))
	}
}

// DiffText is every applied change's diff in order — what a reviewer reads.
func (l *DeliveryLedger) DiffText() string { return strings.Join(l.diffs, "") }

var stopReasons = map[string]string{
	"max_rounds":       "Ran out of rounds — continue, or narrow the task",
	"budget_exhausted": "Budget reached — raise it or narrow the task",
	"loop_guard":       "Stopped repeating a failing call — look at the error above",
	"checks_failed":    "Fix the failing checks",
	"text_tool_calls":  "The model wrote tool calls as text — retry the turn",
	"prompt_echo":      "The model stopped without an answer — retry the turn",
}

// Report is DeliveryLedger.report. acceptance, contract and review are the
// mappings Python passes: {"reports": [{"checks": […]}], "verified": …},
// {"refused": […]} and the reviewer's {"verdict", "headline", "lines", "behaviour"}.
func (l *DeliveryLedger) Report(acceptance, contract, review map[string]any, stopReason string) DeliveryReport {
	var checks, notRun []map[string]any
	if reports, _ := acceptance["reports"].([]any); len(reports) > 0 {
		last := asMap(reports[len(reports)-1])
		for _, raw := range asSlice(last["checks"]) {
			c := asMap(raw)
			if truthy(c["not_run"]) {
				notRun = append(notRun, map[string]any{"command": orNone(c["command"]), "reason": pyStr(c["not_run"])})
				continue
			}
			check := map[string]any{"command": orNone(c["command"]), "passed": truthy(c["passed"]), "exit_code": c["exit_code"]}
			if truthy(c["preexisting"]) {
				check["preexisting"] = true
			}
			if truthy(c["new_failures"]) {
				check["new_failures"] = c["new_failures"]
			}
			checks = append(checks, check)
		}
	}
	var verified *bool
	if v, ok := acceptance["verified"].(bool); ok {
		verified = &v
	}
	var refused []map[string]any
	for _, raw := range asSlice(contract["refused"]) {
		refused = append(refused, asMap(raw))
	}
	var changed []*changedFile
	for _, path := range l.order {
		item := l.changed[path]
		changes := l.symbolChanges(path)
		item.Symbols = symbolLabels(changes)
		item.TestedBy = l.testedBy(changes)
		changed = append(changed, item)
	}
	allPassed := func(allowPreexisting bool) bool {
		for _, c := range checks {
			if !c["passed"].(bool) && !(allowPreexisting && truthy(c["preexisting"])) {
				return false
			}
		}
		return true
	}
	var status, next string
	switch {
	case stopReason != "completed":
		status = "incomplete"
		if text, ok := stopReasons[stopReason]; ok {
			next = text
		} else {
			next = "Stopped: " + stopReason
		}
	case verified != nil && !*verified && len(checks) > 0 && allPassed(true):
		status, next = "done", "Ready to commit — the failing checks already failed before this change"
	case verified != nil && !*verified || !allPassed(false):
		status, next = "incomplete", "Fix the failing checks"
	case len(notRun) > 0 && len(changed) > 0 && verified == nil:
		status, next = "done", "Run the tests yourself — "+pyStr(notRun[0]["reason"])
	case len(changed) > 0:
		status = "done"
		switch {
		case verified != nil && *verified:
			next = "Ready to commit"
		case len(checks) == 0:
			next = "Review the changes — no check ran"
		default:
			next = "Re-run the checks: files changed after the last run"
		}
	default:
		status = "done"
	}
	if review["verdict"] == "blocking" && status == "done" {
		status, next = "incomplete", "Address the review's blocking findings"
	}
	if len(refused) > 0 && status == "done" {
		if len(changed) == 0 {
			next = "Some calls were refused by the change contract — see above"
		} else {
			next += " · some calls were refused by the contract"
		}
	}
	r := DeliveryReport{
		Status: status, Changed: changed, Checks: checks, NotRun: notRun, Verified: verified, Review: "Not reviewed",
		Checkpoints: l.checkpoints, Refused: refused, StopReason: stopReason, Next: next,
	}
	if truthy(review["headline"]) {
		r.Review = pyStr(review["headline"])
	}
	r.ReviewLines = stringList(review["lines"])
	if behaviour := asMap(review["behaviour"]); len(behaviour) > 0 {
		r.Behaviour = behaviour
	}
	if l.risk != nil {
		level := l.risk.Level
		r.RiskLevel, r.RiskSummary = &level, l.risk.Summary
	}
	var sources []string
	for _, item := range changed {
		if item != nil && !isTestPath(item.Path) {
			sources = append(sources, item.Path)
		}
	}
	r.Impact = cachedImpactLine(sources)
	return r
}

func asSlice(value any) []any {
	s, _ := value.([]any)
	return s
}

// checkLedger stands in for the acceptance gate until it is ported: the
// verification commands the model ran (tests, type checks, builds), grouped
// into one run per change. A change after a run makes it stale, so a turn is
// verified only by checks that ran after its last edit.
type checkLedger struct {
	checks []map[string]any
	stale  bool // files changed after the latest check
	edited bool
}

func (c *checkLedger) changed() {
	c.stale = true
	c.edited = true
}

func (c *checkLedger) ran(command string, exitCode int) {
	if c.stale {
		c.checks, c.stale = nil, false
	}
	check := map[string]any{"command": command, "passed": exitCode == 0, "exit_code": exitCode}
	for i, existing := range c.checks {
		if existing["command"] == command {
			c.checks[i] = check
			return
		}
	}
	c.checks = append(c.checks, check)
}

// acceptance is the gate's summary shape for DeliveryLedger.Report.
func (c *checkLedger) acceptance() map[string]any {
	if len(c.checks) == 0 {
		return nil
	}
	checks := make([]any, len(c.checks))
	passed := true
	for i, check := range c.checks {
		checks[i] = check
		passed = passed && check["passed"].(bool)
	}
	out := map[string]any{"reports": []any{map[string]any{"checks": checks}}}
	if !c.stale {
		out["verified"] = passed
	}
	return out
}

// lineDiff is a unified diff of two texts and its line counts, for the
// report and the reviewer; the model never sees it. Common leading and
// trailing lines are trimmed and the rest compared line by line, falling
// back to replace-all for very large rewrites.
func lineDiff(path string, before, after []byte, existed bool) string {
	split := func(b []byte) []string {
		text := strings.ReplaceAll(string(b), "\r\n", "\n")
		if text == "" {
			return nil
		}
		return strings.Split(strings.TrimSuffix(text, "\n"), "\n")
	}
	a, b := split(before), split(after)
	start := 0
	for start < len(a) && start < len(b) && a[start] == b[start] {
		start++
	}
	endA, endB := len(a), len(b)
	for endA > start && endB > start && a[endA-1] == b[endB-1] {
		endA--
		endB--
	}
	if start == endA && start == endB {
		return ""
	}
	var ops []string
	midA, midB := a[start:endA], b[start:endB]
	if len(midA)*len(midB) <= 4_000_000 {
		lcs := make([][]int32, len(midA)+1)
		for i := range lcs {
			lcs[i] = make([]int32, len(midB)+1)
		}
		for i := len(midA) - 1; i >= 0; i-- {
			for j := len(midB) - 1; j >= 0; j-- {
				if midA[i] == midB[j] {
					lcs[i][j] = lcs[i+1][j+1] + 1
				} else {
					lcs[i][j] = max(lcs[i+1][j], lcs[i][j+1])
				}
			}
		}
		i, j := 0, 0
		for i < len(midA) || j < len(midB) {
			switch {
			case i < len(midA) && j < len(midB) && midA[i] == midB[j]:
				ops = append(ops, " "+midA[i])
				i++
				j++
			case j < len(midB) && (i == len(midA) || lcs[i][j+1] >= lcs[i+1][j]):
				ops = append(ops, "+"+midB[j])
				j++
			default:
				ops = append(ops, "-"+midA[i])
				i++
			}
		}
	} else {
		for _, line := range midA {
			ops = append(ops, "-"+line)
		}
		for _, line := range midB {
			ops = append(ops, "+"+line)
		}
	}
	lead := max(0, start-3)
	tail := min(3, len(a)-endA)
	var body []string
	for _, line := range a[lead:start] {
		body = append(body, " "+line)
	}
	body = append(body, ops...)
	for _, line := range a[endA : endA+tail] {
		body = append(body, " "+line)
	}
	oldCount, newCount := 0, 0
	for _, line := range body {
		if line[0] != '+' {
			oldCount++
		}
		if line[0] != '-' {
			newCount++
		}
	}
	from := "a/" + filepath.ToSlash(path)
	if !existed {
		from = "/dev/null"
	}
	header := fmt.Sprintf("--- %s\n+++ b/%s\n@@ -%d,%d +%d,%d @@\n", from, filepath.ToSlash(path), lead+min(1, oldCount), oldCount, lead+min(1, newCount), newCount)
	return header + strings.Join(body, "\n") + "\n"
}

// deliveryCommand answers `aria-runtime delivery`: {"root", "activities":
// [{"tool", "params", "result"}], "acceptance", "contract", "review",
// "stop_reason", "rewind_hint"} on stdin; the report as data and text on stdout.
func deliveryCommand(stdin io.Reader, stdout, stderr io.Writer) int {
	var request struct {
		Root       string `json:"root"`
		Activities []struct {
			Tool   string         `json:"tool"`
			Params map[string]any `json:"params"`
			Result map[string]any `json:"result"`
		} `json:"activities"`
		Acceptance map[string]any `json:"acceptance"`
		Contract   map[string]any `json:"contract"`
		Review     map[string]any `json:"review"`
		StopReason string         `json:"stop_reason"`
		RewindHint string         `json:"rewind_hint"`
	}
	if err := json.NewDecoder(io.LimitReader(stdin, 4<<20)).Decode(&request); err != nil {
		fmt.Fprintln(stderr, "delivery: expected a JSON request:", err)
		return 2
	}
	if request.StopReason == "" {
		request.StopReason = "completed"
	}
	ledger := newDeliveryLedger(request.Root)
	for _, a := range request.Activities {
		ledger.Record(a.Tool, a.Params, a.Result)
	}
	report := ledger.Report(request.Acceptance, request.Contract, request.Review, request.StopReason)
	out := map[string]any{"report": report.asDict(), "text": report.Render(request.RewindHint, request.Root),
		"worth_showing": report.worthShowing(), "diff": ledger.DiffText()}
	if err := json.NewEncoder(stdout).Encode(out); err != nil {
		return 2
	}
	return 0
}

// turnEvidence is what one native turn did, for its delivery report. Its
// methods accept a nil receiver, so a turn without a report records nothing.
type turnEvidence struct {
	ledger  *DeliveryLedger
	checks  checkLedger
	refused []map[string]any
	// The independent review: the request is its goal, the contract as
	// the model read it, and the gate when review_gate is on.
	goal, contract string
	gate           *reviewGate
	// The acceptance gate and how it runs a check, when it is on.
	accept *acceptanceGate
	runner checkRunner
	// Reads already sent to the model this turn, keyed by path and window.
	// A later read of the same bytes is not sent again.
	readPaths  []string
	readDigest map[string]string
	// Numbers returned by financial-service during this turn.
	financeFigures map[string]bool
}

func newTurnEvidence(root string) *turnEvidence {
	return &turnEvidence{ledger: newDeliveryLedger(root)}
}

// applied records a change that reached the disk, with the diff the model
// never sees but the report counts and a reviewer reads.
func (e *turnEvidence) applied(tool string, params map[string]any, c fileChange) {
	if e == nil {
		return
	}
	data := map[string]any{"path": c.path, "diff": lineDiff(c.path, c.before, c.after, c.existed)}
	if !c.existed {
		data["action"] = "created"
	}
	e.ledger.Record(tool, params, map[string]any{"success": true, "data": data})
	e.checks.changed()
	if e.accept != nil {
		e.accept.record(c.path)
		e.accept.baseline.remember(c)
	}
}

// finished records every other call once its result is known.
func (e *turnEvidence) finished(tool string, params map[string]any, result toolResult) {
	if e == nil {
		return
	}
	if violation := asMap(result.Data["contract_violation"]); violation != nil {
		e.refused = append(e.refused, violation)
		return
	}
	if defaultMutatingTools[tool] && result.Success {
		return // recorded with its diff by applied
	}
	code, ran := result.Data["exit_code"].(int)
	success := result.Success && (!ran || code == 0)
	e.ledger.Record(tool, params, map[string]any{"success": success})
	if command, _ := params["command"].(string); tool == "run_command" && ran && isVerificationCommand(normalizeCommand(command)) {
		e.checks.ran(command, code)
	}
	if tool == "read_file" && result.Success {
		if path, _ := result.Data["path"].(string); path != "" {
			e.readPaths = appendNew(e.readPaths, path)
		}
	}
	if success {
		e.rememberFinance(tool, result)
	}
}

// report is the turn's delivery report; stopReason is "completed" unless the
// turn ended early.
func (e *turnEvidence) report(stopReason string) DeliveryReport {
	var contract map[string]any
	if len(e.refused) > 0 {
		refused := make([]any, len(e.refused))
		for i, item := range e.refused {
			refused[i] = item
		}
		contract = map[string]any{"refused": refused}
	}
	var review map[string]any
	if e.gate != nil && e.gate.last() != nil {
		review = e.gate.last().asDict()
	}
	return e.ledger.Report(e.acceptance(), contract, review, stopReason)
}

// acceptance is the gate's summary once it has run, else the verification
// commands the model ran itself.
func (e *turnEvidence) acceptance() map[string]any {
	if e.accept != nil && len(e.accept.reports) > 0 {
		return e.accept.summary()
	}
	return e.checks.acceptance()
}

// verify runs the acceptance gate when the model says it is done and returns
// the repair directive for a red result, or "".
func (e *turnEvidence) verify(ctx context.Context, emit func(string, map[string]any)) string {
	if e == nil || e.accept == nil || e.runner == nil || !e.accept.shouldRun() {
		return ""
	}
	emit("turn.status", map[string]any{"state": "acceptance_started", "message": "Running the checks for the change"})
	report := e.accept.run(ctx, e.runner)
	if report == nil {
		return ""
	}
	state := "acceptance_failed"
	if report.passed() {
		state = "acceptance_passed"
	}
	emit("turn.status", map[string]any{"state": state, "message": report.headline()})
	unstable := report.RevisionStatus == "changed_during_checks" || report.RevisionStatus == "unavailable"
	if report.ran() && !report.passed() && (report.regressed() || unstable) && ctx.Err() == nil {
		return report.repairDirective()
	}
	return ""
}

// review runs the independent reviewer when the turn's change is ready for
// it and returns the directive to send back to the builder, or "". It runs
// after the checks, never instead of them: a red run is repaired first.
func (e *turnEvidence) review(ctx context.Context, p modelProvider, emit func(string, map[string]any)) string {
	if e == nil || e.gate == nil {
		return ""
	}
	diff := e.ledger.DiffText()
	acceptance := e.acceptance()
	if acceptance["verified"] == false || !e.gate.shouldRun(len(e.ledger.order) > 0, diff) {
		return ""
	}
	var checks []map[string]any
	if reports := asSlice(acceptance["reports"]); len(reports) > 0 {
		for _, c := range asSlice(asMap(reports[len(reports)-1])["checks"]) {
			checks = append(checks, asMap(c))
		}
	}
	emit("turn.status", map[string]any{"state": "review_started", "message": "Independent review of the change"})
	prompt := buildReviewPrompt(e.goal, diff, checks, e.contract, e.gate.maxDiffChars)
	// Same model, fresh context: no history, no tools, no project context.
	content, err := p.Generate(ctx, []*genai.Content{genai.NewContentFromText(prompt, genai.RoleUser)}, reviewerSystem, nil, func(string) {})
	var text strings.Builder
	if content != nil {
		for _, part := range content.Parts {
			if !part.Thought {
				text.WriteString(part.Text)
			}
		}
	}
	report := e.gate.record(text.String(), err, diff)
	state := map[string]string{"pass": "review_passed", "blocking": "review_blocking"}[report.Verdict]
	if state == "" {
		state = "review_error"
	}
	emit("turn.status", map[string]any{"state": state, "message": report.headline()})
	if blocking, _ := report.split(); len(blocking) > 0 && e.gate.canRepair() && ctx.Err() == nil {
		return report.repairDirective()
	}
	return ""
}
