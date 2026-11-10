package main

import "strings"

// pythonSyntaxError reports the first structural syntax error in Python source.
// It follows the same finding the hosted Python review gets from ast.parse:
// the 1-based line and that line's text. It checks tokens, brackets, indentation
// and compound-statement headers. A file it accepts can still be rejected by
// CPython for an expression it does not parse, such as assigning to a lambda.
func pythonSyntaxError(source string) (line int, evidence string, found bool) {
	source = strings.ReplaceAll(source, "\r\n", "\n")
	source = strings.ReplaceAll(source, "\r", "\n")
	tokens, fail := pythonTokens(source)
	if fail > 0 {
		return fail, pythonEvidence(source, fail), true
	}
	if fail = pythonStatements(tokens); fail > 0 {
		return fail, pythonEvidence(source, fail), true
	}
	return 0, "", false
}

func pythonEvidence(source string, line int) string {
	lines := strings.Split(source, "\n")
	if line < 1 || line > len(lines) {
		return ""
	}
	return strings.TrimSpace(lines[line-1])
}

type pyKind int

const (
	pyName pyKind = iota
	pyOp
	pyNewline
	pyIndent
	pyDedent
	pyEnd
)

type pyTok struct {
	kind pyKind
	line int
	text string
}

func pythonTokens(source string) ([]pyTok, int) {
	indents := []int{0}
	var out []pyTok
	i, line := 0, 1
	var openers []struct {
		line int
		kind byte
	}
	atStart := true
	for i < len(source) {
		if atStart && len(openers) == 0 {
			start := i
			for i < len(source) && (source[i] == ' ' || source[i] == '\t') {
				i++
			}
			if i < len(source) && (source[i] == '\n' || source[i] == '#') {
				if source[i] == '#' {
					for i < len(source) && source[i] != '\n' {
						i++
					}
				}
				if i < len(source) && source[i] == '\n' {
					i++
					line++
				}
				continue
			}
			column, bad := indentColumn(source[start:i])
			if bad {
				return nil, line
			}
			switch {
			case column == indents[len(indents)-1]:
			case column > indents[len(indents)-1]:
				indents = append(indents, column)
				out = append(out, pyTok{pyIndent, line, ""})
			default:
				for len(indents) > 1 && column < indents[len(indents)-1] {
					indents = indents[:len(indents)-1]
					out = append(out, pyTok{pyDedent, line, ""})
				}
				if column != indents[len(indents)-1] {
					return nil, line
				}
			}
			atStart = false
			if i >= len(source) {
				break
			}
		}
		if source[i] == ' ' || source[i] == '\t' || source[i] == '\f' {
			i++
			continue
		}
		if source[i] == '#' && len(openers) == 0 {
			for i < len(source) && source[i] != '\n' {
				i++
			}
			continue
		}
		if source[i] == '\\' && i+1 < len(source) && source[i+1] == '\n' {
			i += 2
			line++
			continue
		}
		if source[i] == '\n' {
			if len(openers) == 0 {
				out = append(out, pyTok{pyNewline, line, ""})
				atStart = true
			}
			i++
			line++
			continue
		}
		if isStringStart(source, i) {
			next, nextLine, ok := scanPythonString(source, i, line)
			if !ok {
				return nil, line
			}
			out = append(out, pyTok{pyName, line, "str"})
			i, line = next, nextLine
			continue
		}
		switch source[i] {
		case '(', '[', '{':
			openers = append(openers, struct {
				line int
				kind byte
			}{line, source[i]})
			out = append(out, pyTok{pyOp, line, source[i : i+1]})
			i++
			continue
		case ')', ']', '}':
			if len(openers) == 0 || !bracketCloses(openers[len(openers)-1].kind, source[i]) {
				return nil, line
			}
			openers = openers[:len(openers)-1]
			out = append(out, pyTok{pyOp, line, source[i : i+1]})
			i++
			continue
		}
		if isNameStart(source[i]) {
			start, startLine := i, line
			i++
			for i < len(source) && isNameCont(source[i]) {
				i++
			}
			out = append(out, pyTok{pyName, startLine, source[start:i]})
			continue
		}
		if source[i] >= '0' && source[i] <= '9' || (source[i] == '.' && i+1 < len(source) && source[i+1] >= '0' && source[i+1] <= '9') {
			startLine := line
			for i < len(source) && isNumberCont(source[i]) {
				if source[i] == '\n' {
					line++
				}
				i++
			}
			out = append(out, pyTok{pyName, startLine, "num"})
			continue
		}
		if op, n := pythonOp(source, i); n > 0 {
			out = append(out, pyTok{pyOp, line, op})
			i += n
			continue
		}
		return nil, line
	}
	if len(openers) > 0 {
		return nil, openers[0].line
	}
	// A non-empty file without a trailing newline still ends the last statement.
	if len(out) > 0 && out[len(out)-1].kind != pyNewline {
		out = append(out, pyTok{pyNewline, line, ""})
	}
	for len(indents) > 1 {
		indents = indents[:len(indents)-1]
		out = append(out, pyTok{pyDedent, line, ""})
	}
	out = append(out, pyTok{pyEnd, line, ""})
	return out, 0
}

func indentColumn(prefix string) (int, bool) {
	column := 0
	sawSpace, sawTab := false, false
	for i := 0; i < len(prefix); i++ {
		switch prefix[i] {
		case ' ':
			if sawTab {
				return 0, true
			}
			sawSpace = true
			column++
		case '\t':
			if sawSpace {
				return 0, true
			}
			sawTab = true
			column += 8 - column%8
		}
	}
	return column, false
}

func bracketCloses(open, closing byte) bool {
	switch open {
	case '(':
		return closing == ')'
	case '[':
		return closing == ']'
	case '{':
		return closing == '}'
	default:
		return false
	}
}

func isNameStart(b byte) bool {
	return b == '_' || (b >= 'A' && b <= 'Z') || (b >= 'a' && b <= 'z') || b >= 0x80
}

func isNameCont(b byte) bool {
	return isNameStart(b) || (b >= '0' && b <= '9')
}

func isNumberCont(b byte) bool {
	return (b >= '0' && b <= '9') || (b >= 'A' && b <= 'Z') || (b >= 'a' && b <= 'z') || b == '_' || b == '.' || b == '+' || b == '-'
}

func pythonOp(source string, i int) (string, int) {
	ops := []string{"**=", "//=", ">>=", "<<=", "**=", "...", "->", ":=", "**", "//", "<<", ">>", "<=", ">=", "==", "!=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "@="}
	for _, op := range ops {
		if strings.HasPrefix(source[i:], op) {
			return op, len(op)
		}
	}
	switch source[i] {
	case '+', '-', '*', '/', '%', '&', '|', '^', '~', '@', '=', '<', '>', ':', '.', ',', ';':
		return source[i : i+1], 1
	default:
		return "", 0
	}
}

func isStringStart(source string, i int) bool {
	j := i
	for j < len(source) && strings.ContainsRune("rRuUbBfF", rune(source[j])) {
		j++
	}
	if j == i || j >= len(source) {
		return source[i] == '\'' || source[i] == '"'
	}
	return source[j] == '\'' || source[j] == '"'
}

func scanPythonString(source string, i, line int) (int, int, bool) {
	j := i
	raw, formatted := false, false
	for j < len(source) && strings.ContainsRune("rRuUbBfF", rune(source[j])) {
		switch source[j] {
		case 'r', 'R':
			raw = true
		case 'f', 'F':
			formatted = true
		}
		j++
	}
	if j >= len(source) || (source[j] != '\'' && source[j] != '"') {
		return 0, 0, false
	}
	quote := source[j]
	triple := j+2 < len(source) && source[j+1] == quote && source[j+2] == quote
	if triple {
		j += 3
	} else {
		j++
	}
	for j < len(source) {
		if source[j] == '\n' {
			if !triple {
				return 0, 0, false
			}
			line++
			j++
			continue
		}
		if !raw && source[j] == '\\' && j+1 < len(source) {
			if source[j+1] == '\n' {
				line++
			}
			j += 2
			continue
		}
		if formatted && source[j] == '{' {
			if j+1 < len(source) && source[j+1] == '{' {
				j += 2
				continue
			}
			next, nextLine, ok := scanFExpression(source, j+1, line)
			if !ok {
				return 0, 0, false
			}
			j, line = next, nextLine
			continue
		}
		if formatted && source[j] == '}' && j+1 < len(source) && source[j+1] == '}' {
			j += 2
			continue
		}
		if triple && j+2 < len(source) && source[j] == quote && source[j+1] == quote && source[j+2] == quote {
			return j + 3, line, true
		}
		if !triple && source[j] == quote {
			return j + 1, line, true
		}
		j++
	}
	return 0, 0, false
}

func scanFExpression(source string, i, line int) (int, int, bool) {
	depth := 1
	for i < len(source) {
		if isStringStart(source, i) {
			next, nextLine, ok := scanPythonString(source, i, line)
			if !ok {
				return 0, 0, false
			}
			i, line = next, nextLine
			continue
		}
		switch source[i] {
		case '{':
			depth++
		case '}':
			depth--
			i++
			if depth == 0 {
				return i, line, true
			}
			continue
		case '\n':
			line++
		}
		i++
	}
	return 0, 0, false
}

var pyCompound = map[string]bool{
	"if": true, "elif": true, "else": true, "for": true, "while": true, "try": true,
	"except": true, "finally": true, "with": true, "def": true, "class": true,
	"async": true, "match": true, "case": true,
}

func pythonStatements(tokens []pyTok) int {
	i := 0
	return parseSuite(tokens, &i)
}

func parseSuite(tokens []pyTok, i *int) int {
	for {
		for *i < len(tokens) && tokens[*i].kind == pyNewline {
			*i++
		}
		if *i >= len(tokens) || tokens[*i].kind == pyEnd || tokens[*i].kind == pyDedent {
			return 0
		}
		if tokens[*i].kind == pyIndent {
			return tokens[*i].line
		}
		if bad := parseStmt(tokens, i); bad > 0 {
			return bad
		}
	}
}

func parseStmt(tokens []pyTok, i *int) int {
	if tokens[*i].kind == pyOp && tokens[*i].text == "@" {
		for *i < len(tokens) && tokens[*i].kind != pyNewline && tokens[*i].kind != pyEnd {
			*i++
		}
		if *i < len(tokens) && tokens[*i].kind == pyNewline {
			*i++
		}
		return 0
	}
	if tokens[*i].kind == pyName && pyCompound[tokens[*i].text] {
		return parseCompound(tokens, i)
	}
	for *i < len(tokens) && tokens[*i].kind != pyNewline && tokens[*i].kind != pyEnd && tokens[*i].kind != pyDedent {
		*i++
	}
	if *i < len(tokens) && tokens[*i].kind == pyNewline {
		*i++
	}
	return 0
}

func parseCompound(tokens []pyTok, i *int) int {
	start := tokens[*i].line
	kw := tokens[*i].text
	*i++
	if kw == "async" {
		if *i >= len(tokens) || tokens[*i].kind != pyName || (tokens[*i].text != "def" && tokens[*i].text != "for" && tokens[*i].text != "with") {
			return start
		}
		kw = tokens[*i].text
		*i++
	}
	if kw == "def" || kw == "class" {
		if *i >= len(tokens) || tokens[*i].kind != pyName || pyCompound[tokens[*i].text] {
			if *i < len(tokens) {
				return tokens[*i].line
			}
			return start
		}
		*i++
	}
	needsHead := map[string]bool{"if": true, "elif": true, "for": true, "while": true, "with": true, "match": true, "case": true}
	sawHead := false
	sawColon := false
	for *i < len(tokens) && tokens[*i].kind != pyNewline && tokens[*i].kind != pyEnd && tokens[*i].kind != pyDedent {
		if tokens[*i].kind == pyOp && tokens[*i].text == ":" {
			sawColon = true
			*i++
			break
		}
		sawHead = true
		*i++
	}
	if !sawColon {
		return start
	}
	if needsHead[kw] && !sawHead {
		return start
	}
	if *i < len(tokens) && tokens[*i].kind != pyNewline && tokens[*i].kind != pyEnd {
		for *i < len(tokens) && tokens[*i].kind != pyNewline && tokens[*i].kind != pyEnd {
			*i++
		}
		if *i < len(tokens) && tokens[*i].kind == pyNewline {
			*i++
		}
		return 0
	}
	if *i < len(tokens) && tokens[*i].kind == pyNewline {
		*i++
	}
	if *i < len(tokens) && tokens[*i].kind == pyIndent {
		*i++
		if bad := parseSuite(tokens, i); bad > 0 {
			return bad
		}
		if *i >= len(tokens) || tokens[*i].kind != pyDedent {
			if *i < len(tokens) {
				return tokens[*i].line
			}
			return start
		}
		*i++
		return 0
	}
	if *i < len(tokens) {
		return tokens[*i].line
	}
	return start
}
