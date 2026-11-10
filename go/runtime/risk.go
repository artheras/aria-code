package main

// Risk assessment for agent actions — a port of src/aria_code/safety/risk.py
// and the helpers it uses from safety/permissions.py.
//
// The Python module is the oracle: tests/test_native_go_risk.py runs both on
// the same corpus and requires identical results, field by field. Where the
// languages differ the port matches Python by hand:
//
//   - shlex.split: posixSplit below, with the same fallback to whitespace
//     splitting when quoting is unbalanced.
//   - urlparse().hostname is lower-cased; url.Hostname() is not.
//   - Path(x).name returns "" for "/" and "" where filepath.Base does not.
//   - Path.resolve() resolves a path that does not exist yet; EvalSymlinks
//     does not, so resolveLoose resolves the deepest existing ancestor.
//
//   - Python's \w, \s and \d are Unicode-aware; pyRegexp rewrites them.
//
// One difference is accepted: Go's \b is ASCII-only, Python's is Unicode. A
// keyword written flush against a non-ASCII letter ("数据migrate") is a word
// boundary to Go and not to Python. Every \b pattern here raises the risk
// when it matches, so Go can only rate such a command higher, never lower.

import (
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

// pyRegexp compiles a pattern written for Python's re module. Python's \w, \s
// and \d are Unicode-aware and RE2's are ASCII-only, so they are rewritten to
// the Unicode classes Python uses: \w as letters, numbers and underscore; \s
// as str.isspace(); \d as decimal digits. \b stays ASCII (see above).
func pyRegexp(pattern string) *regexp.Regexp {
	const word, space, digit = `\p{L}\p{N}_`, `\s\v\x1c-\x1f\x{85}\p{Z}`, `\p{Nd}`
	var out strings.Builder
	inClass := false
	for i := 0; i < len(pattern); i++ {
		c := pattern[i]
		if c == '\\' && i+1 < len(pattern) {
			next := pattern[i+1]
			i++
			class, negated := "", false
			switch next {
			case 'w':
				class = word
			case 's':
				class = space
			case 'd':
				class = digit
			case 'S':
				class, negated = space, true
			}
			switch {
			case class == "":
				out.WriteByte('\\')
				out.WriteByte(next)
			case negated:
				if inClass {
					panic("pyRegexp: \\S inside a class: " + pattern)
				}
				out.WriteString("[^" + class + "]")
			case inClass:
				out.WriteString(class)
			default:
				out.WriteString("[" + class + "]")
			}
			continue
		}
		switch {
		case c == '[' && !inClass:
			inClass = true
			out.WriteByte(c)
			if i+1 < len(pattern) && pattern[i+1] == '^' {
				i++
				out.WriteByte('^')
			}
			if i+1 < len(pattern) && pattern[i+1] == ']' {
				i++
				out.WriteByte(']')
			}
			continue
		case c == ']' && inClass:
			inClass = false
		}
		out.WriteByte(c)
	}
	return regexp.MustCompile(out.String())
}

var levelNames = map[int]string{0: "none", 1: "low", 2: "medium", 3: "high", 4: "critical"}
var baseScore = map[int]int{0: 5, 1: 20, 2: 40, 3: 65, 4: 85}

// Capabilities lists every capability an assessment can name.
var Capabilities = set(
	"filesystem.read", "filesystem.write", "filesystem.delete",
	"process.execute",
	"network.read", "network.write",
	"package.install",
	"git.stage", "git.commit", "git.push", "git.history",
	"github.pr",
	"database.read", "database.write",
	"secret.read",
	"cloud.deploy",
	"system.admin",
	// Physical: acting on a device or a robot, which a checkpoint cannot undo.
	"hardware.flash", "actuator.command",
)

// RiskAssessment mirrors RiskAssessment.as_dict().
type RiskAssessment struct {
	Level        int      `json:"level"`
	Name         string   `json:"name"`
	Score        int      `json:"score"`
	Capabilities []string `json:"capabilities"`
	Reversible   bool     `json:"reversible"`
	Reasons      []string `json:"reasons"`
	Hosts        []string `json:"hosts"`
	Files        []string `json:"files"`
	Systems      []string `json:"systems"`
	Summary      string   `json:"summary"`
}

func (a RiskAssessment) uses(capability string) bool {
	for _, c := range a.Capabilities {
		if c == capability {
			return true
		}
	}
	return false
}

type riskDraft struct {
	level        int
	capabilities map[string]bool
	reversible   bool
	reasons      []string
	hosts        []string
	files        []string
	systems      []string
	summary      string
}

func newDraft() *riskDraft {
	return &riskDraft{capabilities: map[string]bool{}, reversible: true}
}

func (d *riskDraft) add(capabilities ...string) {
	for _, c := range capabilities {
		d.capabilities[c] = true
	}
}

func appendNew(list []string, items ...string) []string {
	for _, item := range items {
		found := false
		for _, existing := range list {
			if existing == item {
				found = true
				break
			}
		}
		if !found {
			list = append(list, item)
		}
	}
	return list
}

// raiseTo is _Draft.raise_to; irreversible marks the action as not reversible.
func (d *riskDraft) raiseTo(level int, reason string, irreversible bool) {
	if level > d.level {
		d.level = level
	}
	if reason != "" {
		d.reasons = appendNew(d.reasons, reason)
	}
	if irreversible {
		d.reversible = false
	}
}

func (d *riskDraft) merge(o *riskDraft) {
	if o.level > d.level {
		d.level = o.level
	}
	for c := range o.capabilities {
		d.capabilities[c] = true
	}
	d.reversible = d.reversible && o.reversible
	d.reasons = appendNew(d.reasons, o.reasons...)
	d.hosts = appendNew(d.hosts, o.hosts...)
	d.files = appendNew(d.files, o.files...)
	d.systems = appendNew(d.systems, o.systems...)
	if d.summary == "" {
		d.summary = o.summary
	}
}

func orEmpty(list []string) []string {
	if list == nil {
		return []string{}
	}
	return list
}

func (d *riskDraft) freeze(extra int) RiskAssessment {
	score := baseScore[d.level] + extra
	if !d.reversible {
		score += 8
	}
	if d.capabilities["network.write"] || d.capabilities["network.read"] {
		score += 3
	}
	if score < 0 {
		score = 0
	}
	if score > 100 {
		score = 100
	}
	caps := make([]string, 0, len(d.capabilities))
	for c := range d.capabilities {
		caps = append(caps, c)
	}
	sort.Strings(caps)
	return RiskAssessment{
		Level: d.level, Name: levelNames[d.level], Score: score, Capabilities: caps,
		Reversible: d.reversible, Reasons: orEmpty(d.reasons), Hosts: orEmpty(d.hosts),
		Files: orEmpty(d.files), Systems: orEmpty(d.systems), Summary: d.summary,
	}
}

// ── permissions.py helpers ──────────────────────────────────────────────────

var commandAliases = map[string]string{"python": "python3", "pip": "pip3"}

// normalizeCommand is permissions.normalize_command for a string command.
func normalizeCommand(command string) string {
	raw := strings.TrimSpace(command)
	head, rest, found := strings.Cut(raw, " ")
	if alias, ok := commandAliases[head]; ok {
		if found {
			return alias + " " + rest
		}
		return alias
	}
	return raw
}

var shellControl = pyRegexp("[|;&<>`\\n]|\\$\\(")
var trimSuffix = pyRegexp(`(\s+2>&1)?(\s*\|\s*(tail|head)(\s+-n)?\s+-?\d+)?\s*$`)

func hasShellControl(command string) bool { return shellControl.MatchString(command) }

// writesADevice is `>\s*/dev/(?!null\b|stdout\b|stderr\b|tty\b|fd/)` without
// the lookahead RE2 lacks: find every "> … /dev/" and test what follows.
var riskDeviceRedirect = pyRegexp(`>\s*/dev/`)

func writesADevice(command string) bool {
	for _, loc := range riskDeviceRedirect.FindAllStringIndex(command, -1) {
		after := command[loc[1]:]
		allowed := false
		for _, name := range []string{"null", "stdout", "stderr", "tty"} {
			if strings.HasPrefix(after, name) && !startsWithWordChar(after[len(name):]) {
				allowed = true
				break
			}
		}
		if !allowed && !strings.HasPrefix(after, "fd/") {
			return true
		}
	}
	return false
}

// startsWithWordChar is whether Python's Unicode \b would see no boundary.
func startsWithWordChar(s string) bool {
	r, size := utf8.DecodeRuneInString(s)
	return size > 0 && (r == '_' || unicode.IsLetter(r) || unicode.IsNumber(r))
}

var verificationPrefixes = []string{
	"python3 -m py_compile", "python -m py_compile", "python3 -m pytest", "python -m pytest", "pytest",
	"python3 -m unittest", "python -m unittest", "npm test", "npm run test", "yarn test", "pnpm test",
	"npm run build", "npx tsc --noemit", "tsc --noemit", "go test", "cargo test", "mypy", "ruff check",
}

func isVerificationCommand(command string) bool {
	lowered := strings.TrimSpace(strings.ToLower(command))
	if loc := trimSuffix.FindStringIndex(lowered); loc != nil {
		lowered = lowered[:loc[0]] + lowered[loc[1]:]
	}
	if hasShellControl(lowered) {
		return false
	}
	for _, prefix := range verificationPrefixes {
		if strings.HasPrefix(lowered, prefix) {
			return true
		}
	}
	return false
}

var highRiskPatterns = []string{
	" rm ", " rm -", "chmod ", "chown ", "mkfs", "dd if=", "docker ", "kubectl ",
	"shutdown", "reboot", "systemctl ", "launchctl ", "passwd", "sudo ",
	"git push", "git reset --hard", "git checkout --", "mv ",
	":(){ :", "fork bomb",
}
var lowRiskPrefixes = []string{
	"ls", "pwd", "echo", "cat ", "head ", "tail ", "rg ", "find ", "git status",
	"git diff", "git log", "which ", "whoami", "date", "uname ", "env",
	"python3 --version", "node --version", "npm --version",
}

// classifyCommandRisk is permissions.classify_command_risk.
func classifyCommandRisk(command string) string {
	normalized := " " + strings.TrimSpace(strings.ToLower(command)) + " "
	stripped := strings.TrimSpace(normalized)
	for _, p := range highRiskPatterns {
		if strings.Contains(normalized, p) {
			return "high"
		}
	}
	if writesADevice(normalized) {
		return "high"
	}
	if hasShellControl(stripped) {
		return "medium"
	}
	for _, p := range lowRiskPrefixes {
		if strings.HasPrefix(stripped, p) {
			return "low"
		}
	}
	return "medium"
}

// ── commands ────────────────────────────────────────────────────────────────

var (
	riskSegments   = pyRegexp(`\|\||&&|[|;&\n]`)
	envAssignment  = pyRegexp(`^\w+=`)
	sedInPlace     = pyRegexp(`\s-i\b`)
	redirectToFile = pyRegexp(`>>?\s*[^&\s]`)
	urlPattern     = pyRegexp(`https?://[^\s'"]+`)
	pipeToShell    = pyRegexp(`\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b`)
	httpWrite      = pyRegexp(`(?i)(\s-X\s*(POST|PUT|PATCH|DELETE)\b|\s--data\b|\s-d\s|\s--data-\w+|\s-F\s|\s--form\b)`)
	sqlWrite       = pyRegexp(`(?i)\b(insert|update|delete|drop|alter|truncate|create)\b`)
	secretPath     = pyRegexp(`(^|[\s/'"])(\.env(\.[\w-]+)?|\.npmrc|\.pypirc|\.netrc|id_rsa|id_ed25519|[\w.-]+\.pem|[\w.-]+\.key|credentials(\.json)?|\.aws/credentials|\.ssh/)(\s|$|['"])`)
	deployPatterns = []*regexp.Regexp{
		pyRegexp(`^vercel\b.*--prod`), pyRegexp(`^(fly|flyctl) deploy\b`),
		pyRegexp(`^netlify deploy\b.*--prod`), pyRegexp(`^firebase deploy\b`),
		pyRegexp(`^gcloud (app|run|functions) deploy\b`), pyRegexp(`^terraform (apply|destroy)\b`),
		pyRegexp(`^pulumi (up|destroy)\b`), pyRegexp(`^kubectl (apply|delete|rollout|scale)\b`),
		pyRegexp(`^helm (install|upgrade|uninstall)\b`), pyRegexp(`^docker push\b`),
		pyRegexp(`^(npm|pnpm|yarn) publish\b`), pyRegexp(`^twine upload\b`),
		pyRegexp(`^cargo publish\b`), pyRegexp(`^aws .*\b(deploy|update-function-code|s3 (rm|sync|cp))\b`),
		pyRegexp(`^serverless deploy\b`), pyRegexp(`^sls deploy\b`),
	}
	migrationPatterns = []*regexp.Regexp{
		pyRegexp(`\bmigrate\b`), pyRegexp(`\balembic (upgrade|downgrade)\b`),
		pyRegexp(`\bprisma (migrate|db push)\b`), pyRegexp(`\bdb:(migrate|rollback|drop|reset)\b`),
		pyRegexp(`\bknex migrate\b`), pyRegexp(`\bflyway migrate\b`),
	}
)

// firmwareVerbs are, per programmer, the words that mean "write or erase".
var firmwareVerbs = map[string][]string{
	"esptool":     {"write_flash", "write-flash", "erase_flash", "erase-flash", "erase_region", "erase-region"},
	"esptool.py":  {"write_flash", "write-flash", "erase_flash", "erase-flash", "erase_region", "erase-region"},
	"dfu-util":    {"-D", "--download"},
	"st-flash":    {"write", "erase"},
	"stm32flash":  {"-w"},
	"nrfjprog":    {"--program", "--eraseall", "--recover"},
	"picotool":    {"load", "erase"},
	"fastboot":    {"flash", "flashall", "erase", "update"},
	"arduino-cli": {"upload"},
	"pio":         {"upload"},
	"platformio":  {"upload"},
	"idf.py":      {"flash", "erase-flash", "erase_flash"},
	"west":        {"flash"},
}

var (
	openocdProgram = pyRegexp(`\b(program|flash write_image)\b`)
	avrdudeWrite   = pyRegexp(`flash:w`)
	// `ros2 <noun> <verb>` that send a command to a running robot.
	rosActuation = map[[2]string]bool{
		{"topic", "pub"}: true, {"service", "call"}: true, {"action", "send_goal"}: true,
		{"param", "set"}: true, {"lifecycle", "set"}: true,
	}
)

// writesFirmware is true for a programmer invoked to write or erase a device,
// not to read it.
func writesFirmware(head string, words []string, lowered string) bool {
	switch head {
	case "openocd":
		return openocdProgram.MatchString(lowered)
	case "avrdude":
		return avrdudeWrite.MatchString(lowered)
	}
	verbs, ok := firmwareVerbs[head]
	if !ok {
		return false
	}
	for _, w := range words[1:] {
		for _, v := range verbs {
			if w == v {
				return true
			}
		}
	}
	return false
}

func set(items ...string) map[string]bool {
	out := make(map[string]bool, len(items))
	for _, item := range items {
		out[item] = true
	}
	return out
}

var (
	readCommands = set("ls", "pwd", "echo", "cat", "head", "tail", "less", "more", "wc", "rg", "grep", "find",
		"fd", "tree", "which", "whoami", "date", "uname", "file", "stat", "du", "df", "diff",
		"sort", "uniq", "cut", "awk", "sed", "jq", "true", "false", "test", "basename", "dirname")
	gitRead   = set("status", "diff", "log", "show", "blame", "branch", "rev-parse", "ls-files", "remote", "describe", "shortlog", "grep", "config")
	dbClients = set("psql", "mysql", "sqlite3", "mongosh", "mongo", "redis-cli", "cqlsh")
)

type installer struct {
	subcommands map[string]bool
	files       []string
	host        string
}

var installers = map[string]installer{
	"npm":     {set("install", "i", "add", "ci", "update", "uninstall", "remove"), []string{"package.json", "package-lock.json"}, "registry.npmjs.org"},
	"pnpm":    {set("install", "i", "add", "update", "remove"), []string{"package.json", "pnpm-lock.yaml"}, "registry.npmjs.org"},
	"yarn":    {set("install", "add", "upgrade", "remove", ""), []string{"package.json", "yarn.lock"}, "registry.yarnpkg.com"},
	"bun":     {set("install", "add", "i", "remove"), []string{"package.json", "bun.lockb"}, "registry.npmjs.org"},
	"pip":     {set("install", "uninstall"), nil, "pypi.org"},
	"pip3":    {set("install", "uninstall"), nil, "pypi.org"},
	"uv":      {set("add", "pip", "sync", "remove"), []string{"pyproject.toml", "uv.lock"}, "pypi.org"},
	"poetry":  {set("add", "install", "update", "remove"), []string{"pyproject.toml", "poetry.lock"}, "pypi.org"},
	"cargo":   {set("add", "install", "update", "remove"), []string{"Cargo.toml", "Cargo.lock"}, "crates.io"},
	"go":      {set("get", "install"), []string{"go.mod", "go.sum"}, "proxy.golang.org"},
	"gem":     {set("install", "uninstall"), nil, "rubygems.org"},
	"bundle":  {set("install", "add", "update"), []string{"Gemfile", "Gemfile.lock"}, "rubygems.org"},
	"brew":    {set("install", "uninstall", "upgrade"), nil, "formulae.brew.sh"},
	"apt":     {set("install", "remove"), nil, "deb.debian.org"},
	"apt-get": {set("install", "remove"), nil, "deb.debian.org"},
}

// posixSplit is shlex.split (posix mode, whitespace splitting). ok is false
// where shlex raises ValueError: an unclosed quote or a trailing backslash.
func posixSplit(s string) (words []string, ok bool) {
	var word strings.Builder
	inWord := false
	runes := []rune(s)
	for i := 0; i < len(runes); i++ {
		c := runes[i]
		switch {
		case c == ' ' || c == '\t' || c == '\r' || c == '\n':
			if inWord {
				words = append(words, word.String())
				word.Reset()
				inWord = false
			}
		case c == '\\':
			if i+1 >= len(runes) {
				return nil, false
			}
			i++
			word.WriteRune(runes[i])
			inWord = true
		case c == '\'':
			inWord = true
			closed := false
			for i++; i < len(runes); i++ {
				if runes[i] == '\'' {
					closed = true
					break
				}
				word.WriteRune(runes[i])
			}
			if !closed {
				return nil, false
			}
		case c == '"':
			inWord = true
			closed := false
			for i++; i < len(runes); i++ {
				if runes[i] == '"' {
					closed = true
					break
				}
				if runes[i] == '\\' && i+1 < len(runes) && (runes[i+1] == '"' || runes[i+1] == '\\') {
					i++
				}
				word.WriteRune(runes[i])
			}
			if !closed {
				return nil, false
			}
		default:
			word.WriteRune(c)
			inWord = true
		}
	}
	if inWord {
		words = append(words, word.String())
	}
	return words, true
}

func shellWords(segment string) []string {
	if words, ok := posixSplit(segment); ok {
		return words
	}
	return strings.FieldsFunc(segment, pyIsSpace)
}

// pyIsSpace is str.isspace(), which also counts the ASCII separators U+001C–U+001F.
func pyIsSpace(r rune) bool {
	return unicode.IsSpace(r) || r >= 0x1c && r <= 0x1f
}

// pathName is pathlib.Path(p).name.
func pathName(p string) string {
	p = strings.TrimRight(p, "/")
	if p == "" {
		return ""
	}
	if i := strings.LastIndex(p, "/"); i >= 0 {
		p = p[i+1:]
	}
	if p == "." {
		return ""
	}
	return p
}

func urlHosts(segment string) []string {
	var hosts []string
	for _, raw := range urlPattern.FindAllString(segment, -1) {
		parsed, err := url.Parse(raw)
		if err != nil {
			continue
		}
		if host := strings.ToLower(parsed.Hostname()); host != "" {
			hosts = appendNew(hosts, host)
		}
	}
	return hosts
}

func targets(words []string) []string {
	var out []string
	for _, w := range words[1:] {
		if !strings.HasPrefix(w, "-") {
			out = append(out, w)
		}
	}
	return out
}

func lastOf(list []string) []string {
	if len(list) == 0 {
		return nil
	}
	return list[len(list)-1:]
}

func assessSegment(segment string) *riskDraft {
	d := newDraft()
	words := shellWords(segment)
	if len(words) == 0 {
		return d
	}
	for len(words) > 0 && envAssignment.MatchString(words[0]) {
		words = words[1:]
	}
	if len(words) > 0 && words[0] == "sudo" {
		d.add("system.admin")
		d.raiseTo(4, "Runs with administrator privileges", true)
		d.systems = append(d.systems, "Operating system")
		words = words[1:]
	}
	for len(words) > 1 && (words[0] == "time" || words[0] == "nice" || words[0] == "nohup" || words[0] == "env") {
		words = words[1:]
	}
	if len(words) == 0 {
		return d
	}
	head := pathName(words[0])
	sub := ""
	if len(words) > 1 {
		sub = words[1]
	}
	lowered := strings.TrimSpace(strings.ToLower(segment))
	d.add("process.execute")

	if secretPath.MatchString(" "+segment+" ") && (readCommands[head] || head == "source" || head == ".") {
		d.add("secret.read")
		d.raiseTo(4, "Reads a credentials or secrets file", true)
		d.summary = "Read secrets"
		return d
	}
	if head == "env" || head == "printenv" {
		d.add("secret.read")
		d.raiseTo(3, "Prints environment variables, which may hold secrets", false)
		d.summary = "Print environment"
		return d
	}
	if head == "git" {
		return assessGit(words, d)
	}
	if head == "gh" {
		return assessGH(words, d)
	}
	if inst, ok := installers[head]; ok && inst.subcommands[sub] {
		d.add("package.install", "network.read")
		d.raiseTo(2, strings.TrimSpace(fmt.Sprintf("Changes installed dependencies (%s %s)", head, sub)), false)
		d.hosts = append(d.hosts, inst.host)
		d.files = append(d.files, inst.files...)
		if len(inst.files) == 0 {
			switch {
			case strings.HasPrefix(head, "pip"):
				d.systems = append(d.systems, "Python environment")
			case head == "brew" || head == "apt" || head == "apt-get":
				d.systems = append(d.systems, "System packages")
			default:
				d.systems = append(d.systems, head+" environment")
			}
		}
		d.summary = "Install dependency"
		return d
	}
	if writesFirmware(head, words, lowered) {
		d.add("hardware.flash")
		d.raiseTo(4, "Writes firmware to a device; a bad image can leave it unusable", true)
		d.systems = append(d.systems, "Device firmware")
		d.summary = "Flash firmware"
		return d
	}
	if head == "ros2" && len(words) >= 3 && rosActuation[[2]string{words[1], words[2]}] {
		d.add("actuator.command")
		d.raiseTo(4, "Sends a command to a running robot (ros2 "+words[1]+" "+words[2]+"); it may move hardware", true)
		d.systems = append(d.systems, "Robot / actuators")
		d.summary = "Command a robot"
		return d
	}
	for _, pattern := range deployPatterns {
		if pattern.MatchString(lowered) {
			d.add("cloud.deploy", "network.write")
			d.raiseTo(4, "Deploys or publishes outside this machine", true)
			d.systems = append(d.systems, "Production / remote environment")
			d.summary = "Deploy or publish"
			return d
		}
	}
	for _, pattern := range migrationPatterns {
		if pattern.MatchString(lowered) {
			d.add("database.write")
			d.raiseTo(4, "Runs a database migration", true)
			d.systems = append(d.systems, "Database schema")
			d.summary = "Database migration"
			return d
		}
	}
	if dbClients[head] {
		if sqlWrite.MatchString(segment) {
			d.add("database.write")
			d.raiseTo(4, "Writes to a database", true)
			d.systems = append(d.systems, "Database")
			d.summary = "Database write"
		} else {
			d.add("database.read")
			d.raiseTo(2, "Connects to a database", false)
			d.systems = append(d.systems, "Database")
			d.summary = "Database query"
		}
		return d
	}
	switch head {
	case "curl", "wget", "http", "https", "httpie", "xh":
		d.hosts = append(d.hosts, urlHosts(segment)...)
		if head != "wget" && httpWrite.MatchString(" "+segment+" ") {
			d.add("network.write")
			d.raiseTo(3, "Sends data to a remote server", true)
			d.summary = "Network write"
		} else {
			d.add("network.read")
			d.raiseTo(2, "Fetches from the network", false)
			d.summary = "Network request"
		}
		return d
	case "rm", "rmdir", "shred", "unlink":
		d.add("filesystem.delete")
		ts := targets(words)
		d.files = append(d.files, ts...)
		d.raiseTo(4, "Deletes files without a checkpoint", true)
		for _, t := range ts {
			if t == "/" || t == "~" || t == "*" || t == "." || t == ".." || strings.HasPrefix(t, "/") || strings.HasPrefix(t, "~") {
				d.raiseTo(4, "Deletes outside the workspace or a whole tree", false)
				break
			}
		}
		d.summary = "Delete files"
		return d
	case "mv", "cp", "touch", "mkdir", "ln", "tee":
		d.add("filesystem.write")
		d.files = append(d.files, lastOf(targets(words))...)
		d.raiseTo(1, "Changes files in the workspace", false)
		d.summary = "Change files"
		return d
	case "chmod", "chown", "mkfs", "dd", "shutdown", "reboot", "systemctl", "launchctl", "passwd", "kill",
		"killall", "pkill", "docker", "kubectl":
		d.add("system.admin")
		level := 4
		if head == "kill" || head == "pkill" || head == "killall" || head == "docker" {
			level = 3
		}
		d.raiseTo(level, "Changes system state outside the workspace", true)
		d.systems = append(d.systems, "Operating system")
		d.summary = "System change"
		return d
	}
	if isVerificationCommand(segment) {
		d.add("filesystem.read")
		d.summary = "Verify"
		return d
	}
	if readCommands[head] {
		if head == "sed" && sedInPlace.MatchString(segment) {
			d.add("filesystem.write")
			d.raiseTo(1, "Edits files in place", false)
			d.files = append(d.files, lastOf(targets(words))...)
			d.summary = "Change files"
		} else {
			d.add("filesystem.read")
			d.summary = "Inspect"
		}
		return d
	}
	d.add("filesystem.write")
	d.raiseTo(1, fmt.Sprintf("Runs %s, which may change files in the workspace", head), false)
	d.summary = "Run " + head
	return d
}

func contains(list []string, item string) bool {
	for _, x := range list {
		if x == item {
			return true
		}
	}
	return false
}

func assessGit(words []string, d *riskDraft) *riskDraft {
	var args []string
	for _, w := range words[1:] {
		if !strings.HasPrefix(w, "-C") {
			args = append(args, w)
		}
	}
	sub := ""
	for _, w := range args {
		if !strings.HasPrefix(w, "-") {
			sub = w
			break
		}
	}
	switch {
	case gitRead[sub]:
		d.add("filesystem.read")
		d.summary = "Inspect repository"
	case sub == "fetch" || sub == "pull" || sub == "clone":
		if sub == "clone" {
			d.add("network.read", "filesystem.write")
		} else {
			d.add("network.read", "git.history")
		}
		d.raiseTo(2, "git "+sub+" reaches a remote", false)
		d.summary = "git " + sub
	case sub == "add" || sub == "rm" || sub == "mv" || sub == "restore" || sub == "stash" || sub == "switch" || sub == "checkout" || sub == "worktree":
		d.add("git.stage")
		d.raiseTo(1, "git "+sub+" changes the working tree or index", false)
		if (sub == "checkout" && contains(words, "--")) || (sub == "restore" && !contains(words, "--staged")) {
			d.raiseTo(4, "Discards uncommitted changes", true)
		}
		d.summary = "git " + sub
	case sub == "commit" || sub == "merge" || sub == "rebase" || sub == "cherry-pick" || sub == "revert" || sub == "tag" || sub == "am":
		d.add("git.commit")
		d.raiseTo(3, "git "+sub+" writes repository history", false)
		d.summary = "git " + sub
	case sub == "push":
		d.add("git.push", "network.write")
		d.hosts = append(d.hosts, "git remote")
		force := false
		for _, w := range words {
			switch w {
			case "--force", "-f", "--force-with-lease", "--mirror", "--delete", "-d":
				force = true
			}
		}
		if len(args) > 1 {
			for _, w := range args[1:] {
				if strings.HasPrefix(w, "+") {
					force = true
				}
			}
		}
		if force {
			d.raiseTo(4, "Rewrites or deletes history on the remote", true)
			d.summary = "Force push"
		} else {
			d.raiseTo(3, "Publishes commits to the remote", true)
			d.summary = "git push"
		}
	case sub == "reset" || sub == "clean":
		d.add("git.history")
		if contains(words, "--hard") || sub == "clean" {
			d.raiseTo(4, "Discards uncommitted work", true)
		} else {
			d.raiseTo(1, "Moves the index", false)
		}
		d.summary = "git " + sub
	default:
		d.add("git.history")
		label := sub
		if label == "" {
			label = "command"
		}
		d.raiseTo(1, "git "+label, false)
		d.summary = strings.TrimSpace("git " + sub)
	}
	return d
}

func assessGH(words []string, d *riskDraft) *riskDraft {
	sub, action := "", ""
	if len(words) > 1 {
		sub = words[1]
	}
	if len(words) > 2 {
		action = words[2]
	}
	d.hosts = append(d.hosts, "github.com")
	switch {
	case sub == "pr" && set("create", "merge", "close", "edit", "comment", "review", "ready")[action]:
		d.add("github.pr", "network.write")
		level := 3
		if action == "merge" {
			level = 4
		}
		d.raiseTo(level, "gh pr "+action+" acts on GitHub", action == "merge")
		d.summary = "gh pr " + action
	case set("release", "repo", "secret", "workflow", "api")[sub] && !set("view", "list", "status", "")[action]:
		d.add("network.write")
		level := 3
		if sub == "secret" || sub == "release" || sub == "repo" {
			level = 4
		}
		d.raiseTo(level, "gh "+sub+" "+action+" changes GitHub", true)
		d.summary = "gh " + sub + " " + action
	default:
		d.add("network.read")
		d.raiseTo(2, "Reads from GitHub", false)
		d.summary = strings.TrimSpace("gh " + sub)
	}
	return d
}

// AssessCommand is risk.assess_command: what running command touches.
func AssessCommand(command string) RiskAssessment {
	normalized := normalizeCommand(command)
	if normalized == "" {
		return newDraft().freeze(0)
	}
	d := newDraft()
	var segments []string
	for _, s := range riskSegments.Split(normalized, -1) {
		if strings.TrimSpace(s) != "" {
			segments = append(segments, strings.TrimSpace(s))
		}
	}
	if len(segments) == 0 {
		segments = []string{normalized}
	}
	for _, segment := range segments {
		d.merge(assessSegment(segment))
	}
	if len(segments) > 1 && d.summary == "" {
		d.summary = "Compound command"
	}
	if pipeToShell.MatchString(normalized) {
		d.add("network.read")
		d.raiseTo(4, "Runs a downloaded script without reading it", true)
		d.summary = "Download and run"
	}
	if writesADevice(normalized) {
		d.add("system.admin")
		d.raiseTo(4, "Writes to a device", true)
	}
	if redirectToFile.MatchString(normalized) && !d.capabilities["filesystem.write"] {
		d.add("filesystem.write")
		d.raiseTo(1, "Redirects output into a file", false)
	}
	legacy := classifyCommandRisk(normalized)
	if legacy == "high" && d.level < 3 {
		d.raiseTo(3, "Classified high-risk by the command policy", false)
	}
	if legacy == "medium" && d.level == 0 && !isVerificationCommand(normalized) {
		d.raiseTo(1, "", false)
	}
	extra := 0
	if hasShellControl(normalized) && len(segments) > 1 {
		extra = 4
	}
	return d.freeze(extra)
}

// ── tools ───────────────────────────────────────────────────────────────────

var (
	riskReadTools = set("read_file", "list_files", "search_code", "glob", "project_context", "git_status", "git_diff",
		"repo_map", "find_symbol", "analyze_file", "notebook_read", "update_todos")
	riskWriteTools       = set("write_file", "edit_file", "multi_edit", "apply_change", "apply_patch", "notebook_edit")
	riskNetworkReadTools = set("web_fetch", "web_search", "get_market_data", "get_market_history")
	patchTarget          = pyRegexp(`(?m)^\+\+\+ (?:b/)?(\S+)`)
)

// toolPaths is risk.tool_paths: the file paths a tool call names.
func toolPaths(params map[string]any) []string {
	var paths []string
	for _, key := range []string{"path", "file_path", "filename", "target", "notebook_path"} {
		if value, ok := params[key].(string); ok && strings.TrimSpace(value) != "" {
			paths = append(paths, strings.TrimSpace(value))
		}
	}
	if patch, ok := params["patch"].(string); ok {
		for _, m := range patchTarget.FindAllStringSubmatch(patch, -1) {
			if m[1] != "/dev/null" {
				paths = append(paths, m[1])
			}
		}
	}
	return paths
}

func expandUser(p string) string {
	if p == "~" || strings.HasPrefix(p, "~/") {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, strings.TrimPrefix(p, "~"))
		}
	}
	return p
}

// resolveLoose is Path.resolve(strict=False): symlinks resolved as far as
// the path exists, the rest appended.
func resolveLoose(p string) string {
	if abs, err := filepath.Abs(p); err == nil {
		p = abs
	}
	rest := ""
	for current := p; ; {
		if resolved, err := filepath.EvalSymlinks(current); err == nil {
			if rest == "" {
				return resolved
			}
			return filepath.Join(resolved, rest)
		}
		parent := filepath.Dir(current)
		if parent == current {
			return p
		}
		rest = filepath.Join(filepath.Base(current), rest)
		current = parent
	}
}

// joinPath is pathlib's root / path. On Windows a path rooted without a drive
// ("/etc/hosts", "\\x") is on root's drive, not inside root, as filepath.Join
// would make it.
func joinPath(root, path string) string {
	if filepath.IsAbs(path) {
		return path
	}
	if path != "" && os.IsPathSeparator(path[0]) {
		return filepath.VolumeName(root) + path
	}
	return filepath.Join(root, path)
}

func insideRoot(path, root string) bool {
	if root == "" {
		return true
	}
	candidate := joinPath(root, expandUser(path))
	rel, err := filepath.Rel(resolveLoose(root), resolveLoose(candidate))
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// AssessTool is risk.assess_tool: what one tool call touches.
func AssessTool(tool string, params map[string]any, root string) RiskAssessment {
	name := strings.TrimSpace(tool)
	if params == nil {
		params = map[string]any{}
	}
	if root != "" {
		root = expandUser(root)
	}
	if name == "run_command" {
		command, _ := params["command"].(string)
		return AssessCommand(command)
	}
	d := newDraft()
	switch {
	case riskReadTools[name]:
		d.add("filesystem.read")
		d.summary = "Inspect"
	case riskWriteTools[name]:
		paths := toolPaths(params)
		d.add("filesystem.write")
		d.files = append(d.files, paths...)
		d.raiseTo(1, "Edits files in the workspace (checkpointed)", false)
		var outside, secrets []string
		for _, p := range paths {
			if !insideRoot(p, root) {
				outside = append(outside, p)
			}
			if secretPath.MatchString(" " + p + " ") {
				secrets = append(secrets, p)
			}
		}
		if len(outside) > 0 {
			d.raiseTo(3, "Writes outside the workspace: "+strings.Join(outside, ", "), false)
		}
		if len(secrets) > 0 {
			d.add("secret.read")
			d.raiseTo(4, "Writes a credentials or secrets file", false)
		}
		d.summary = "Edit files"
	case riskNetworkReadTools[name]:
		d.add("network.read")
		if u, ok := params["url"].(string); ok {
			d.hosts = append(d.hosts, urlHosts(u)...)
		}
		d.raiseTo(2, "Fetches from the network", false)
		d.summary = "Network request"
	case name == "github":
		action, _ := params["action"].(string)
		if action == "" {
			action, _ = params["command"].(string)
		}
		if action != "" {
			return AssessCommand("gh " + action)
		}
		d.add("network.read")
		d.hosts = append(d.hosts, "github.com")
		d.summary = "Read GitHub"
		d.raiseTo(2, "Reads from GitHub", false)
	case name == "broker_order":
		d.add("network.write")
		d.raiseTo(4, "Places an order with a broker", true)
		d.systems = append(d.systems, "Brokerage account")
		d.summary = "Broker order"
	default:
		d.add("process.execute")
		label := name
		if label == "" {
			label = "unknown"
		}
		d.raiseTo(1, fmt.Sprintf("Runs the %s tool", label), false)
		if name != "" {
			d.summary = "Run " + name
		} else {
			d.summary = "Unknown tool"
		}
	}
	return d.freeze(0)
}

// riskCommand answers `aria-runtime risk`: a JSON array of requests on stdin,
// each {"command": "..."} or {"tool": "...", "params": {...}, "root": "..."},
// and the assessments as a JSON array on stdout. A request with "mode" (and
// optionally "auto_level") is answered with its approval requirement and
// risk card as well. It exists so the Python
// oracle can compare both implementations on the same corpus.
func riskCommand(stdin io.Reader, stdout, stderr io.Writer) int {
	var requests []struct {
		Command   *string        `json:"command"`
		Tool      string         `json:"tool"`
		Params    map[string]any `json:"params"`
		Root      string         `json:"root"`
		Mode      *string        `json:"mode"`
		AutoLevel any            `json:"auto_level"`
	}
	if err := json.NewDecoder(io.LimitReader(stdin, 1<<20)).Decode(&requests); err != nil {
		fmt.Fprintln(stderr, "risk: expected a JSON array of requests:", err)
		return 2
	}
	out := make([]any, 0, len(requests))
	for _, r := range requests {
		var a RiskAssessment
		if r.Command != nil {
			a = AssessCommand(*r.Command)
		} else {
			a = AssessTool(r.Tool, r.Params, r.Root)
		}
		if r.Mode == nil {
			out = append(out, a)
			continue
		}
		// With "mode", also how the action is approved and its card.
		out = append(out, map[string]any{"assessment": a, "requirement": approvalRequirement(a, *r.Mode, r.AutoLevel), "card": riskCard(a)})
	}
	if err := json.NewEncoder(stdout).Encode(out); err != nil {
		return 2
	}
	return 0
}

// maxAutoLevel is the highest level approval_mode=risk may run without
// asking. L3 (acting outside the repo) and L4 (destructive) are never automatic.
const maxAutoLevel = 2

// pyInt is Python's int() for a JSON value; ok is false where int() raises.
func pyInt(value any) (int, bool) {
	switch v := value.(type) {
	case float64:
		if v != v || v > 1<<62 || v < -(1<<62) {
			return 0, false
		}
		return int(v), true
	case int:
		return v, true
	case bool:
		if v {
			return 1, true
		}
		return 0, true
	case string:
		n, err := strconv.Atoi(strings.TrimSpace(strings.ReplaceAll(v, "_", "")))
		return n, err == nil
	}
	return 0, false
}

// approvalRequirement is risk.approval_requirement: "always" for L4, which is
// asked about even after a standing "allow" given for something milder;
// "auto" under approval_mode=risk at or below auto_approve_level (capped at
// L2); "ask" otherwise.
func approvalRequirement(a RiskAssessment, mode string, autoLevel any) string {
	if a.Level >= 4 {
		return "always"
	}
	ceiling, ok := pyInt(autoLevel)
	if !ok {
		ceiling = 1
	}
	ceiling = max(0, min(maxAutoLevel, ceiling))
	if mode == "risk" && a.Level <= ceiling {
		return "auto"
	}
	return "ask"
}

// riskCard is ui.render.output.format_risk_card: what an action touches,
// shown above its approval.
func riskCard(a RiskAssessment) []string {
	reversible := "reversible"
	if !a.Reversible {
		reversible = "not reversible"
	}
	lines := []string{fmt.Sprintf("Risk     L%d %s · %d/100 · %s", a.Level, a.Name, a.Score, reversible)}
	row := func(label string, values []string) {
		var shown []string
		for _, v := range values {
			if v != "" {
				shown = append(shown, v)
			}
		}
		if len(shown) == 0 {
			return
		}
		text := strings.Join(shown[:min(4, len(shown))], ", ")
		if len(shown) > 4 {
			text += fmt.Sprintf(" +%d", len(shown)-4)
		}
		lines = append(lines, fmt.Sprintf("%-9s%s", label, text))
	}
	row("Network", a.Hosts)
	row("Files", a.Files)
	row("Affects", a.Systems)
	row("Why", a.Reasons[:min(2, len(a.Reasons))])
	return lines
}
