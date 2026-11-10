package main

// The evidence layer for one turn. It keeps three things the model does not
// get to invent: a bounded list of the files that grounded the turn, a refusal
// to send the same bytes twice, and a note when the closing text claims a
// check passed or a file was written without a record of it.

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

const (
	contextBudget   = 32 << 10
	contextPathCap  = 64
	claimNoteHeader = "未核实：以下说法没有对应的记录。"
)

type contextEntry struct {
	Path   string `json:"path"`
	State  string `json:"state"`
	SHA256 string `json:"sha256,omitempty"`
	Bytes  int    `json:"bytes,omitempty"`
	Reason string `json:"reason,omitempty"`
}

type contextPacket struct {
	BudgetBytes int            `json:"budget_bytes"`
	Included    []contextEntry `json:"included"`
	Omitted     []contextEntry `json:"omitted"`
}

func (p contextPacket) export() map[string]any {
	included, omitted := p.Included, p.Omitted
	if included == nil {
		included = []contextEntry{}
	}
	if omitted == nil {
		omitted = []contextEntry{}
	}
	return map[string]any{"budget_bytes": p.BudgetBytes, "included": included, "omitted": omitted}
}

// prepareContext lists the paths a caller may attach, in order, until the
// byte budget or the path cap. A missing file stays in the packet as absent.
// A symlink, an oversized file or a path outside the workspace is omitted
// rather than read.
func prepareContext(root string, paths []string, budgetBytes int) contextPacket {
	if budgetBytes <= 0 {
		budgetBytes = contextBudget
	}
	packet := contextPacket{BudgetBytes: budgetBytes, Included: []contextEntry{}, Omitted: []contextEntry{}}
	resolved, err := canonical(root)
	if err != nil {
		for _, path := range paths {
			packet.Omitted = append(packet.Omitted, contextEntry{Path: path, State: "omitted", Reason: "unavailable"})
		}
		return packet
	}
	seen := map[string]bool{}
	used := 0
	for _, path := range paths {
		if len(packet.Included)+len(packet.Omitted) >= contextPathCap {
			packet.Omitted = append(packet.Omitted, contextEntry{Path: path, State: "omitted", Reason: "over the path cap"})
			continue
		}
		name, ok := workspaceRelative(resolved, path)
		if !ok {
			packet.Omitted = append(packet.Omitted, contextEntry{Path: path, State: "omitted", Reason: "outside the workspace"})
			continue
		}
		if seen[name] {
			continue
		}
		seen[name] = true
		entry, ok := fingerprintFile(resolved, name)
		if !ok {
			packet.Omitted = append(packet.Omitted, contextEntry{Path: name, State: "omitted", Reason: "unavailable"})
			continue
		}
		item := contextEntry{Path: entry.Path, State: entry.State, SHA256: entry.SHA256}
		if entry.State == "file" {
			size := regularSize(resolved, name)
			if size < 0 {
				packet.Omitted = append(packet.Omitted, contextEntry{Path: name, State: "omitted", Reason: "unavailable"})
				continue
			}
			if used+size > budgetBytes {
				packet.Omitted = append(packet.Omitted, contextEntry{Path: name, State: "omitted", SHA256: entry.SHA256, Bytes: size, Reason: "over the byte budget"})
				continue
			}
			used += size
			item.Bytes = size
		}
		packet.Included = append(packet.Included, item)
	}
	return packet
}

func regularSize(root, name string) int {
	current := root
	var info os.FileInfo
	for _, part := range strings.Split(name, "/") {
		next := filepath.Join(current, part)
		stat, err := os.Lstat(next)
		if err != nil || stat.Mode()&os.ModeSymlink != 0 {
			return -1
		}
		info = stat
		current = next
	}
	if info == nil || !info.Mode().IsRegular() || info.Size() > revisionMaxBytes {
		return -1
	}
	return int(info.Size())
}

func (e *turnEvidence) contextPacket() contextPacket {
	if e == nil || e.ledger == nil {
		return prepareContext("", nil, contextBudget)
	}
	paths := append([]string{}, e.readPaths...)
	paths = append(paths, e.ledger.order...)
	return prepareContext(e.ledger.root, paths, contextBudget)
}

// collapseUnchangedRead reports that this read's bytes were already sent.
// The first time through records the digest and returns false.
func (e *turnEvidence) collapseUnchangedRead(params map[string]any, result toolResult) (string, bool) {
	if e == nil || result.Data == nil {
		return "", false
	}
	content, _ := result.Data["content"].(string)
	path, _ := result.Data["path"].(string)
	if path == "" {
		return "", false
	}
	sum := sha256.Sum256([]byte(content))
	digest := hex.EncodeToString(sum[:])
	key := path + "\x00" + strconv.Itoa(integer(params, "offset", 0)) + "\x00" + strconv.Itoa(integer(params, "limit", 0))
	if e.readDigest == nil {
		e.readDigest = map[string]string{}
	}
	if previous, ok := e.readDigest[key]; ok && previous == digest {
		return fmt.Sprintf("unchanged since the earlier read in this turn; sha256=%s; bytes=%d", digest, len(content)), true
	}
	e.readDigest[key] = digest
	return "", false
}

var (
	passClaim = regexp.MustCompile(`(?im)(?:^|[.!?。]\s*)(?:all |the )?(?:tests|checks|test suite) (?:have |has )?passed\b|测试已经通过|测试已通过|验收已经通过|验收已通过`)
	// A dot inside a filename is not the end of the sentence.
	editClaim = regexp.MustCompile(`(?i)(?:I(?:'ve| have)? (?:updated|modified|edited|wrote|created)|我(?:已经|已)?(?:修改了|更新了|写入了|写了|创建了))(?:[^.\n。]|\.[A-Za-z0-9]){0,80}`)
	fileToken = regexp.MustCompile(`(?:[\w.-]+/)*[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,7}`)
)

// claimFindings lists hard claims in answer that the delivery report does not
// support. Ordinary prose without those claims produces none.
func claimFindings(answer string, report DeliveryReport, figures map[string]bool) []string {
	var out []string
	if passClaim.MatchString(answer) && !checksSupportPass(report) {
		out = append(out, "回答说检查已通过，这次没有完整通过的验收。")
	}
	seen := map[string]bool{}
	for _, sentence := range editClaim.FindAllString(answer, -1) {
		for _, token := range fileToken.FindAllString(sentence, -1) {
			if seen[token] || wrotePath(report, token) {
				seen[token] = true
				continue
			}
			seen[token] = true
			out = append(out, "回答说写过 `"+token+"`，记录里没有这个文件的写入。")
			if len(out) >= 8 {
				return out
			}
		}
	}
	for _, figure := range financeClaimNumbers(answer, len(figures) > 0) {
		if len(out) >= 8 {
			break
		}
		if financeFigureKnown(figures, figure) {
			continue
		}
		out = append(out, "回答里的 "+figure+" 不是这次金融工具返回的数字。")
	}
	return out
}

func checksSupportPass(report DeliveryReport) bool {
	if report.Verified == nil || !*report.Verified || len(report.NotRun) > 0 || len(report.Checks) == 0 {
		return false
	}
	for _, check := range report.Checks {
		passed, ok := check["passed"].(bool)
		if !ok || !passed {
			return false
		}
	}
	return true
}

func wrotePath(report DeliveryReport, token string) bool {
	token = filepath.ToSlash(token)
	for _, changed := range report.Changed {
		if changed == nil {
			continue
		}
		name := filepath.ToSlash(changed.Path)
		if name == token || strings.HasSuffix(name, "/"+token) {
			return true
		}
		if !strings.Contains(token, "/") && filepath.Base(name) == token {
			return true
		}
	}
	return false
}

// annotateClaims leaves the model's words in place and appends a note when
// a hard claim has no record. A turn with no evidence is unchanged.
func annotateClaims(answer string, evidence *turnEvidence) string {
	if evidence == nil || strings.Contains(answer, claimNoteHeader) {
		return answer
	}
	findings := claimFindings(answer, evidence.report("completed"), evidence.financeFigures)
	if len(findings) == 0 {
		return answer
	}
	var b strings.Builder
	b.WriteString(strings.TrimRight(answer, "\n"))
	b.WriteString("\n\n")
	b.WriteString(claimNoteHeader)
	for _, finding := range findings {
		b.WriteString("\n- ")
		b.WriteString(finding)
	}
	return b.String()
}
