package main

import (
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// Symbol names on a delivery report. The current file is read and this turn's
// diffs are undone newest first. A diff that does not fit, or a file that
// cannot be read, leaves the names empty rather than guessing.

type deliveryChange struct {
	kind, name, symbolKind string
}

var deliveryHunk = regexp.MustCompile(`^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@`)

func (l *DeliveryLedger) symbolChanges(path string) []deliveryChange {
	diffs := l.fileDiffs[path]
	language := graphLanguageOf(path)
	if len(diffs) == 0 || language == "" {
		return nil
	}
	body, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	after := string(body)
	before := after
	for i := len(diffs) - 1; i >= 0; i-- {
		if strings.Contains(diffs[i], "--- /dev/null") {
			before = ""
			break
		}
		previous, ok := reverseApply(before, diffs[i])
		if !ok {
			return nil
		}
		before = previous
	}
	return compareSymbolBodies(before, after, language)
}

func symbolLabels(changes []deliveryChange) []string {
	if len(changes) == 0 {
		return nil
	}
	labels := make([]string, len(changes))
	for i, change := range changes {
		suffix := ""
		switch change.symbolKind {
		case "def", "async def", "function", "method", "func", "fn":
			suffix = "()"
		}
		labels[i] = change.name + suffix + " " + change.kind
	}
	return labels
}

func (l *DeliveryLedger) testedBy(changes []deliveryChange) []string {
	if l.root == "" || len(changes) == 0 {
		return nil
	}
	graph := loadCachedProjectGraph(l.root)
	if graph == nil {
		return nil
	}
	found := map[string]bool{}
	for _, change := range changes {
		if change.kind == "removed" {
			continue
		}
		parts := strings.Split(change.name, ".")
		files := stringSet(graph.refs[parts[len(parts)-1]])
		for _, outer := range parts[:len(parts)-1] {
			files = intersectSets(files, stringSet(graph.refs[outer]))
		}
		for file := range files {
			if isTestPath(file) {
				found[file] = true
			}
		}
	}
	if len(found) == 0 {
		return nil
	}
	out := make([]string, 0, len(found))
	for file := range found {
		out = append(out, file)
	}
	sort.Strings(out)
	return out
}

func stringSet(items []string) map[string]bool {
	out := map[string]bool{}
	for _, item := range items {
		out[item] = true
	}
	return out
}

func intersectSets(a, b map[string]bool) map[string]bool {
	out := map[string]bool{}
	for item := range a {
		if b[item] {
			out[item] = true
		}
	}
	return out
}

type deliveryDef struct {
	qualified, kind, parent string
	line                    int
}

func compareSymbolBodies(before, after, language string) []deliveryChange {
	old := symbolBodies(before, language)
	newBodies := symbolBodies(after, language)
	var changes []deliveryChange
	for name, body := range newBodies {
		previous, existed := old[name]
		if !existed {
			parent := parentSymbol(name)
			if parent != "" && newBodies[parent].kind != "" && old[parent].kind == "" {
				continue
			}
			changes = append(changes, deliveryChange{"added", name, body.kind})
			continue
		}
		if previous.text != body.text {
			changes = append(changes, deliveryChange{"modified", name, body.kind})
		}
	}
	for name, body := range old {
		if _, exists := newBodies[name]; exists {
			continue
		}
		parent := parentSymbol(name)
		if parent != "" && old[parent].kind != "" && newBodies[parent].kind == "" {
			continue
		}
		changes = append(changes, deliveryChange{"removed", name, body.kind})
	}
	sort.Slice(changes, func(i, j int) bool {
		if rank(changes[i].kind) != rank(changes[j].kind) {
			return rank(changes[i].kind) < rank(changes[j].kind)
		}
		return changes[i].name < changes[j].name
	})
	return changes
}

func rank(kind string) int {
	switch kind {
	case "added":
		return 0
	case "modified":
		return 1
	default:
		return 2
	}
}

func parentSymbol(name string) string {
	if i := strings.LastIndex(name, "."); i >= 0 {
		return name[:i]
	}
	return ""
}

type symbolBody struct {
	kind, text string
}

func symbolBodies(source, language string) map[string]symbolBody {
	defs := deliveryDefs(source, language)
	sort.Slice(defs, func(i, j int) bool { return defs[i].line < defs[j].line })
	lines := strings.Split(strings.TrimSuffix(strings.ReplaceAll(source, "\r\n", "\n"), "\n"), "\n")
	if source == "" {
		lines = nil
	}
	out := map[string]symbolBody{}
	for index, symbol := range defs {
		end := len(lines)
		for _, later := range defs[index+1:] {
			if later.parent != symbol.qualified && !strings.HasPrefix(later.parent, symbol.qualified+".") {
				end = later.line - 1
				break
			}
		}
		if end > len(lines) {
			end = len(lines)
		}
		start := symbol.line - 1
		if start < 0 {
			start = 0
		}
		var own []string
		if start < end {
			for offset, line := range lines[start:end] {
				if !insideChild(symbol.line+offset, symbol.qualified, defs) {
					own = append(own, line)
				}
			}
		}
		for len(own) > 0 {
			last := strings.TrimSpace(own[len(own)-1])
			if last != "" && !strings.HasPrefix(last, "#") && !strings.HasPrefix(last, "//") {
				break
			}
			own = own[:len(own)-1]
		}
		out[symbol.qualified] = symbolBody{symbol.kind, strings.Join(own, "\n")}
	}
	return out
}

func insideChild(number int, parent string, defs []deliveryDef) bool {
	for _, child := range defs {
		if child.parent != parent {
			continue
		}
		next := len(defs) + 1000000
		for _, later := range defs {
			if later.line > child.line && later.line < next {
				next = later.line
			}
		}
		if child.line <= number && number < next {
			return true
		}
	}
	return false
}

func deliveryDefs(source, language string) []deliveryDef {
	var out []deliveryDef
	switch language {
	case "python":
		type open struct {
			indent int
			name   string
		}
		var classes []open
		for _, index := range graphPyDef.FindAllStringSubmatchIndex(source, -1) {
			groups := submatches(source, index)
			if len(groups) < 4 {
				continue
			}
			indent := len(strings.ReplaceAll(groups[1], "\t", "    "))
			for len(classes) > 0 && classes[len(classes)-1].indent >= indent {
				classes = classes[:len(classes)-1]
			}
			parent := ""
			if len(classes) > 0 {
				parent = classes[len(classes)-1].name
			}
			kind := groups[2]
			if strings.Contains(groups[0], "async") {
				kind = "async def"
			}
			name := groups[3]
			qualified := name
			if parent != "" {
				qualified = parent + "." + name
			}
			out = append(out, deliveryDef{qualified: qualified, kind: kind, parent: parent, line: 1 + strings.Count(source[:index[0]], "\n")})
			if groups[2] == "class" {
				classes = append(classes, open{indent, qualified})
			}
		}
	case "javascript":
		for _, index := range graphJsDef.FindAllStringSubmatchIndex(source, -1) {
			groups := submatches(source, index)
			if len(groups) < 3 {
				continue
			}
			out = append(out, deliveryDef{qualified: groups[2], kind: strings.TrimSuffix(groups[1], "*"), line: 1 + strings.Count(source[:index[0]], "\n")})
		}
	case "go":
		for _, index := range graphGoFunc.FindAllStringSubmatchIndex(source, -1) {
			groups := submatches(source, index)
			if len(groups) < 3 {
				continue
			}
			name := groups[2]
			if groups[1] != "" {
				name = groups[1] + "." + groups[2]
			}
			out = append(out, deliveryDef{qualified: name, kind: "func", parent: groups[1], line: 1 + strings.Count(source[:index[0]], "\n")})
		}
		for _, index := range graphGoType.FindAllStringSubmatchIndex(source, -1) {
			groups := submatches(source, index)
			if len(groups) < 2 {
				continue
			}
			out = append(out, deliveryDef{qualified: groups[1], kind: "type", line: 1 + strings.Count(source[:index[0]], "\n")})
		}
	case "rust":
		for _, index := range graphRustDef.FindAllStringSubmatchIndex(source, -1) {
			groups := submatches(source, index)
			if len(groups) < 3 {
				continue
			}
			out = append(out, deliveryDef{qualified: groups[2], kind: groups[1], line: 1 + strings.Count(source[:index[0]], "\n")})
		}
	}
	return out
}

func submatches(source string, index []int) []string {
	out := make([]string, len(index)/2)
	for i := 0; i < len(index); i += 2 {
		if index[i] < 0 {
			continue
		}
		out[i/2] = source[index[i]:index[i+1]]
	}
	return out
}

type applyHunk struct {
	newStart int
	old, new []string
}

func reverseApply(after, diff string) (string, bool) {
	lines := splitKeep(after)
	var hunks []applyHunk
	current := -1
	for _, raw := range splitKeep(diff) {
		header := strings.TrimRight(raw, "\r\n")
		if deliveryHunk.MatchString(header) {
			parts := deliveryHunk.FindStringSubmatch(header)
			start, _ := strconv.Atoi(parts[3])
			hunks = append(hunks, applyHunk{newStart: start})
			current = len(hunks) - 1
			continue
		}
		if current < 0 || strings.HasPrefix(raw, "---") || strings.HasPrefix(raw, "+++") || strings.HasPrefix(raw, `\`) {
			continue
		}
		tag, body := "", raw
		if raw != "" {
			_, size := firstRune(raw)
			tag, body = raw[:size], raw[size:]
		}
		switch tag {
		case " ":
			hunks[current].old = append(hunks[current].old, body)
			hunks[current].new = append(hunks[current].new, body)
		case "-":
			hunks[current].old = append(hunks[current].old, body)
		case "+":
			hunks[current].new = append(hunks[current].new, body)
		default:
			if strings.TrimSpace(raw) == "" {
				hunks[current].old = append(hunks[current].old, raw)
				hunks[current].new = append(hunks[current].new, raw)
			}
		}
	}
	if len(hunks) == 0 {
		return "", false
	}
	for i := len(hunks) - 1; i >= 0; i-- {
		hunk := hunks[i]
		start := hunk.newStart
		if len(hunk.new) > 0 && start > 0 {
			start--
		}
		end := start + len(hunk.new)
		if end > len(lines) {
			return "", false
		}
		for n := range hunk.new {
			if trimNewline(lines[start+n]) != trimNewline(hunk.new[n]) {
				return "", false
			}
		}
		lines = append(lines[:start], append(append([]string{}, hunk.old...), lines[end:]...)...)
	}
	return strings.Join(lines, ""), true
}

func firstRune(s string) (rune, int) {
	for _, r := range s {
		return r, len(string(r))
	}
	return 0, 0
}

func trimNewline(line string) string { return strings.TrimRight(line, "\r\n") }

func splitKeep(source string) []string {
	if source == "" {
		return nil
	}
	var lines []string
	rest := source
	for rest != "" {
		n := strings.IndexAny(rest, "\n\r")
		if n < 0 {
			lines = append(lines, rest)
			break
		}
		end := n + 1
		if strings.HasPrefix(rest[n:], "\r\n") {
			end = n + 2
		}
		lines = append(lines, rest[:end])
		rest = rest[end:]
	}
	return lines
}
