package main

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"errors"
	"fmt"
	"io"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

const (
	maxExtractedBytes = 80 << 20
	maxArchiveFiles   = 2000
	maxReviewFiles    = 300
	maxSourceBytes    = 512 << 10
)

var ignoredParts = map[string]bool{
	".git": true, ".hg": true, ".svn": true, "node_modules": true, "vendor": true,
	"dist": true, "build": true, ".next": true, ".venv": true, "venv": true,
	"__pycache__": true, "coverage": true, "Pods": true,
}

var sensitiveNames = map[string]bool{
	".env": true, ".npmrc": true, ".pypirc": true, "credentials.json": true,
	"service-account.json": true, "id_rsa": true, "id_dsa": true,
}

var textExtensions = setOf(
	".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".swift", ".kt", ".kts", ".java",
	".go", ".rs", ".rb", ".php", ".cs", ".c", ".h", ".cpp", ".hpp", ".m", ".mm",
	".sh", ".bash", ".zsh", ".sql", ".graphql", ".json", ".yaml", ".yml", ".toml",
	".xml", ".html", ".css", ".scss", ".md", ".txt",
)

func setOf(values ...string) map[string]bool {
	out := map[string]bool{}
	for _, v := range values {
		out[v] = true
	}
	return out
}

func truncateRunes(value string, limit int) string {
	if limit <= 0 {
		return ""
	}
	runes := []rune(value)
	if len(runes) > limit {
		return string(runes[:limit])
	}
	return value
}

type archiveFile struct {
	Path string
	Data []byte
}
type reviewFile struct{ Path, Content, Language string }

func safeArchivePath(raw string) (string, error) {
	name := strings.ReplaceAll(raw, "\\", "/")
	clean := path.Clean(name)
	if clean == "." || strings.HasPrefix(clean, "/") || clean == ".." || strings.HasPrefix(clean, "../") || strings.Contains(clean, ":") {
		return "", errors.New("Archive contains an unsafe path.")
	}
	return clean, nil
}

func ignoredArchivePath(name string) bool {
	for _, part := range strings.Split(name, "/") {
		if ignoredParts[part] {
			return true
		}
	}
	return false
}

func sensitiveArchivePath(name string) bool {
	base := strings.ToLower(path.Base(name))
	ext := strings.ToLower(path.Ext(name))
	return sensitiveNames[base] || strings.HasPrefix(base, ".env.") || setOf(".pem", ".key", ".p12", ".pfx")[ext]
}

func archiveEntries(payload []byte, filename string) ([]archiveFile, error) {
	lowered := strings.ToLower(filename)
	var out []archiveFile
	if strings.HasSuffix(lowered, ".zip") {
		reader, err := zip.NewReader(bytes.NewReader(payload), int64(len(payload)))
		if err != nil {
			return nil, errors.New("Invalid or encrypted ZIP archive.")
		}
		files, size := 0, int64(0)
		for _, entry := range reader.File {
			if entry.FileInfo().IsDir() {
				continue
			}
			files++
			declaredSize := entry.UncompressedSize64
			if files > maxArchiveFiles || declaredSize > uint64(maxExtractedBytes) || size > maxExtractedBytes-int64(declaredSize) {
				return nil, errors.New("Expanded project exceeds the file-count or size limit.")
			}
			size += int64(declaredSize)
		}
		for _, entry := range reader.File {
			if entry.FileInfo().IsDir() || entry.Mode()&0o170000 == 0o120000 || entry.UncompressedSize64 > uint64(maxSourceBytes) {
				continue
			}
			file, err := entry.Open()
			if err != nil {
				return nil, errors.New("Invalid or encrypted ZIP archive.")
			}
			data, err := io.ReadAll(io.LimitReader(file, maxSourceBytes+1))
			_ = file.Close()
			if err != nil {
				return nil, errors.New("Invalid or encrypted ZIP archive.")
			}
			out = append(out, archiveFile{entry.Name, data})
		}
		return out, nil
	}
	if strings.HasSuffix(lowered, ".tar") || strings.HasSuffix(lowered, ".tar.gz") || strings.HasSuffix(lowered, ".tgz") {
		var source io.Reader = bytes.NewReader(payload)
		if strings.HasSuffix(lowered, ".tar.gz") || strings.HasSuffix(lowered, ".tgz") {
			gz, err := gzip.NewReader(source)
			if err != nil {
				return nil, errors.New("Invalid TAR archive.")
			}
			defer gz.Close()
			source = gz
		}
		tr := tar.NewReader(source)
		files, size := 0, int64(0)
		for {
			header, err := tr.Next()
			if errors.Is(err, io.EOF) {
				break
			}
			if err != nil {
				return nil, errors.New("Invalid TAR archive.")
			}
			if header.Typeflag != tar.TypeReg && header.Typeflag != tar.TypeRegA {
				continue
			}
			files++
			if header.Size < 0 || files > maxArchiveFiles || header.Size > maxExtractedBytes || size > maxExtractedBytes-header.Size {
				return nil, errors.New("Expanded project exceeds the file-count or size limit.")
			}
			size += header.Size
			if header.Size > maxSourceBytes {
				continue
			}
			data, err := io.ReadAll(io.LimitReader(tr, maxSourceBytes+1))
			if err != nil {
				return nil, errors.New("Invalid TAR archive.")
			}
			out = append(out, archiveFile{header.Name, data})
		}
		return out, nil
	}
	return nil, errors.New("Only .zip, .tar, .tar.gz, and .tgz projects are supported.")
}

func languageFor(name string) string {
	switch strings.ToLower(filepath.Ext(name)) {
	case ".py", ".pyi":
		return "Python"
	case ".js", ".jsx":
		return "JavaScript"
	case ".ts", ".tsx":
		return "TypeScript"
	case ".swift":
		return "Swift"
	case ".kt", ".kts":
		return "Kotlin"
	case ".java":
		return "Java"
	case ".go":
		return "Go"
	case ".rs":
		return "Rust"
	case ".rb":
		return "Ruby"
	case ".php":
		return "PHP"
	case ".cs":
		return "C#"
	case ".c":
		return "C"
	case ".h":
		return "C/C++"
	case ".cpp", ".hpp":
		return "C++"
	case ".sql":
		return "SQL"
	case ".sh", ".bash", ".zsh":
		return "Shell"
	default:
		return "Text"
	}
}

func decodeSource(data []byte) (string, bool) {
	if bytes.IndexByte(data[:min(len(data), 8192)], 0) >= 0 {
		return "", false
	}
	return strings.ToValidUTF8(string(data), "�"), true
}

func collectReviewFiles(payload []byte, filename string) ([]reviewFile, map[string]any, error) {
	if len(payload) == 0 || len(payload) > maxArchiveBytes {
		return nil, nil, fmt.Errorf("Project archive must be between 1 byte and %d bytes.", maxArchiveBytes)
	}
	entries, err := archiveEntries(payload, filename)
	if err != nil {
		return nil, nil, err
	}
	files := make([]reviewFile, 0, min(len(entries), maxReviewFiles))
	languages := map[string]int{}
	var totalFiles, totalBytes, skippedSensitive, skippedBinary int
	for _, entry := range entries {
		name, err := safeArchivePath(entry.Path)
		if err != nil {
			return nil, nil, err
		}
		if ignoredArchivePath(name) {
			continue
		}
		totalFiles++
		totalBytes += len(entry.Data)
		if totalFiles > maxArchiveFiles || totalBytes > maxExtractedBytes {
			return nil, nil, errors.New("Expanded project exceeds the file-count or size limit.")
		}
		if sensitiveArchivePath(name) {
			skippedSensitive++
			continue
		}
		ext := strings.ToLower(filepath.Ext(name))
		if len(entry.Data) > maxSourceBytes || !textExtensions[ext] {
			skippedBinary++
			continue
		}
		text, ok := decodeSource(entry.Data)
		if !ok {
			skippedBinary++
			continue
		}
		language := languageFor(name)
		lines := strings.Count(text, "\n") + 1
		if lines < 1 {
			lines = 1
		}
		languages[language] += lines
		if len(files) < maxReviewFiles {
			files = append(files, reviewFile{name, text, language})
		}
	}
	if len(files) == 0 {
		return nil, nil, errors.New("No reviewable source or text files were found.")
	}
	type languageCount struct {
		Name  string `json:"name"`
		Lines int    `json:"lines"`
	}
	languageRows := make([]languageCount, 0, len(languages))
	for name, lines := range languages {
		languageRows = append(languageRows, languageCount{name, lines})
	}
	sort.Slice(languageRows, func(i, j int) bool {
		if languageRows[i].Lines == languageRows[j].Lines {
			return languageRows[i].Name < languageRows[j].Name
		}
		return languageRows[i].Lines > languageRows[j].Lines
	})
	if len(languageRows) > 8 {
		languageRows = languageRows[:8]
	}
	archiveName := path.Base(strings.ReplaceAll(filename, "\\", "/"))
	archiveName = truncateRunes(archiveName, 180)
	summary := map[string]any{
		"archive_name": archiveName, "total_files": totalFiles, "reviewed_files": len(files),
		"total_uncompressed_bytes": totalBytes, "skipped_sensitive_files": skippedSensitive,
		"skipped_binary_or_large_files": skippedBinary, "languages": languageRows,
		"truncated": len(files) >= maxReviewFiles || totalFiles > len(files)+skippedSensitive+skippedBinary,
	}
	return files, summary, nil
}

var (
	secretAssignment = regexp.MustCompile(`(?i)\b(?:api[_-]?key|secret|token|password)\b\s*=\s*['"][^'"]{8,}['"]`)
	tokenLiteral     = regexp.MustCompile(`(?i)\b(?:(?:ghp|github_pat)_[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{12,})\b`)
	shellTrue        = regexp.MustCompile(`(?i)\b(?:subprocess\.[A-Za-z_]+|os\.system)\b.*\bshell\s*=\s*True`)
	evalExec         = regexp.MustCompile(`\b(?:eval|exec)\s*\(`)
	pickleLoad       = regexp.MustCompile(`\bpickle\.loads?\s*\(`)
	unsafeYAML       = regexp.MustCompile(`\byaml\.load\s*\(`)
	tlsDisabled      = regexp.MustCompile(`(?i)\brequests\.[A-Za-z_]+\s*\(.*\bverify\s*=\s*False`)
	bareExcept       = regexp.MustCompile(`^\s*except\s*:`)
)

type finding struct {
	Severity string `json:"severity"`
	Rule     string `json:"rule"`
	Message  string `json:"message"`
	Line     int    `json:"line"`
	Evidence string `json:"evidence"`
	File     string `json:"file"`
}

func scanReviewFile(file reviewFile) []finding {
	if strings.TrimSpace(file.Content) == "" {
		return []finding{{Severity: "medium", Rule: "empty-input", Message: "没有可审查的代码内容。", File: file.Path}}
	}
	lines := splitTextLines(file.Content)
	var findings []finding
	for i, line := range lines {
		add := func(severity, rule, message, evidence string) {
			if rule == "hardcoded-secret" {
				evidence = "<credential value redacted>"
			}
			evidence = truncateRunes(evidence, 240)
			findings = append(findings, finding{severity, rule, message, i + 1, strings.TrimSpace(evidence), file.Path})
		}
		if secretAssignment.MatchString(line) || tokenLiteral.MatchString(line) {
			add("high", "hardcoded-secret", "疑似硬编码凭据；改用环境变量或受管密钥，并立即轮换已暴露的值。", line)
		}
		if shellTrue.MatchString(line) {
			add("high", "shell-true", "shell=True 会把不可信输入交给 shell 解释；改用参数列表和严格的输入校验。", line)
		}
		if evalExec.MatchString(line) {
			add("high", "dynamic-execution", "eval/exec 可执行不可信文本；使用受限解析器或显式分发表。", line)
		}
		if pickleLoad.MatchString(line) {
			add("high", "unsafe-deserialization", "pickle 反序列化不可信数据可执行任意代码；改用 JSON 或签名的可信工件。", line)
		}
		if unsafeYAML.MatchString(line) && !strings.Contains(line, "Loader=") {
			add("high", "unsafe-yaml-load", "yaml.load 未指定安全 Loader；使用 yaml.safe_load。", line)
		}
		if tlsDisabled.MatchString(line) {
			add("high", "tls-verification-disabled", "TLS 证书校验被关闭；除受控测试外不得使用 verify=False。", line)
		}
		if bareExcept.MatchString(line) {
			add("medium", "bare-except", "裸 except 会吞掉系统退出和中断；至少捕获 Exception，并记录或处理错误。", "except:")
		}
	}
	return findings
}

func reviewArchive(payload []byte, filename string) (map[string]any, error) {
	files, summary, err := collectReviewFiles(payload, filename)
	if err != nil {
		return nil, err
	}
	findings := make([]finding, 0)
	for _, file := range files {
		findings = append(findings, scanReviewFile(file)...)
	}
	seenFindings := make(map[string]bool, len(findings))
	uniqueFindings := findings[:0]
	for _, row := range findings {
		key := fmt.Sprintf("%s\x00%d\x00%s\x00%s", row.Rule, row.Line, row.Evidence, row.File)
		if seenFindings[key] {
			continue
		}
		seenFindings[key] = true
		uniqueFindings = append(uniqueFindings, row)
	}
	findings = uniqueFindings
	severityRank := map[string]int{"critical": 0, "high": 1, "medium": 2, "low": 3}
	sort.Slice(findings, func(i, j int) bool {
		a, b := findings[i], findings[j]
		if severityRank[a.Severity] != severityRank[b.Severity] {
			return severityRank[a.Severity] < severityRank[b.Severity]
		}
		if a.File != b.File {
			return a.File < b.File
		}
		if a.Line != b.Line {
			return a.Line < b.Line
		}
		return a.Rule < b.Rule
	})
	counts := map[string]int{"critical": 0, "high": 0, "medium": 0, "low": 0}
	for _, row := range findings {
		counts[row.Severity]++
	}
	if len(findings) > 500 {
		findings = findings[:500]
	}
	score := max(0, 100-counts["critical"]*25-counts["high"]*12-counts["medium"]*4-counts["low"])
	summary["score"] = score
	summary["finding_counts"] = counts
	return map[string]any{
		"schema_version": "aria.project-review.v1", "status": "completed", "summary": summary,
		"product_assessment": productAssessment(files), "findings": findings,
		"recommendations": projectRecommendations(files, summary),
		"limitations":     []string{"只进行了只读静态审核；没有执行代码、安装依赖、访问网络或运行测试。", "自动审核不能替代人工安全审计、运行时验证或渗透测试。"},
	}, nil
}

func projectRecommendations(files []reviewFile, summary map[string]any) []map[string]string {
	paths := make([]string, 0, len(files))
	names := map[string]bool{}
	for _, f := range files {
		p := strings.ToLower(f.Path)
		paths = append(paths, p)
		names[strings.ToLower(path.Base(p))] = true
	}
	hasMarker := func(markers ...string) bool {
		for _, p := range paths {
			for _, m := range markers {
				if strings.Contains(p, m) {
					return true
				}
			}
		}
		return false
	}
	hasTests := hasMarker("test", "tests", "spec", "__tests__")
	hasCI := hasMarker(".github/workflows/", ".gitlab-ci")
	hasReadme := false
	for n := range names {
		if strings.HasPrefix(n, "readme") {
			hasReadme = true
		}
	}
	hasLock := false
	for _, n := range []string{"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock", "cargo.lock", "go.sum"} {
		if names[n] {
			hasLock = true
		}
	}
	out := []map[string]string{}
	if !hasTests {
		out = append(out, map[string]string{"priority": "high", "category": "testing", "message": "未发现测试目录或测试文件；先为核心业务路径补充可重复执行的单元与集成测试。"})
	}
	if !hasCI {
		out = append(out, map[string]string{"priority": "medium", "category": "delivery", "message": "未发现 CI 工作流；建议在合并前自动运行格式、静态检查、测试和依赖审计。"})
	}
	if !hasReadme {
		out = append(out, map[string]string{"priority": "medium", "category": "documentation", "message": "未发现 README；补充本地启动、配置、测试、部署与安全边界说明。"})
	}
	if !hasLock && (names["package.json"] || names["pyproject.toml"] || names["cargo.toml"] || names["go.mod"]) {
		out = append(out, map[string]string{"priority": "medium", "category": "dependencies", "message": "检测到依赖清单但未发现锁文件；应用项目应锁定可复现依赖版本。"})
	}
	if value, _ := summary["skipped_sensitive_files"].(int); value > 0 {
		out = append(out, map[string]string{"priority": "high", "category": "secrets", "message": "上传包包含敏感配置文件，审核时已跳过；确认这些文件未进入版本库并轮换可能暴露的凭据。"})
	}
	if summary["truncated"] == true {
		out = append(out, map[string]string{"priority": "medium", "category": "coverage", "message": "项目超过单次审核覆盖上限；建议按服务、包或提交拆分后继续审核。"})
	}
	return out
}

func productAssessment(files []reviewFile) map[string]any {
	paths := []string{}
	names := map[string]bool{}
	var joined strings.Builder
	for i, f := range files {
		p := strings.ToLower(f.Path)
		paths = append(paths, p)
		names[path.Base(p)] = true
		if i < 80 {
			content := f.Content
			content = truncateRunes(content, 8000)
			joined.WriteString(strings.ToLower(content))
			joined.WriteByte('\n')
		}
	}
	text := joined.String()
	hasPath := func(markers ...string) bool {
		for _, p := range paths {
			for _, m := range markers {
				if strings.Contains(p, m) {
					return true
				}
			}
		}
		return false
	}
	contains := func(markers ...string) bool {
		for _, m := range markers {
			if strings.Contains(text, m) {
				return true
			}
		}
		return false
	}
	types := []string{}
	for _, item := range []struct {
		name    string
		markers []string
	}{
		{"web", []string{"package.json", "vite.config", "next.config", "src/app", "src/pages"}},
		{"mobile", []string{"project.pbxproj", "package.swift", "androidmanifest.xml", "pubspec.yaml"}},
		{"backend_api", []string{"fastapi", "django", "flask", "express(", "@restcontroller", "gin.default"}},
		{"cli", []string{"[project.scripts]", "console_scripts", "commander(", "argparse", "click.command"}},
		{"library", []string{"setup.py", "pyproject.toml", "package.json", "cargo.toml", "go.mod"}},
	} {
		if contains(item.markers...) || hasPath(item.markers...) {
			types = append(types, item.name)
		}
	}
	if len(types) == 0 {
		types = []string{"software_project"}
	}
	frameworks := []string{}
	frameworkMarkers := []struct {
		name    string
		markers []string
	}{
		{"React", []string{"react", "react-dom"}},
		{"Next.js", []string{"next", "next.config"}},
		{"Vue", []string{"vue", "nuxt"}},
		{"SwiftUI", []string{"swiftui"}},
		{"FastAPI", []string{"fastapi"}},
		{"Django", []string{"django"}},
		{"Flask", []string{"flask"}},
		{"Express", []string{"express"}},
		{"Spring", []string{"springframework"}},
		{"Flutter", []string{"flutter", "pubspec.yaml"}},
	}
	for _, item := range frameworkMarkers {
		if contains(item.markers...) || hasPath(item.markers...) {
			frameworks = append(frameworks, item.name)
		}
	}
	dimensions := map[string]bool{
		"user_experience":     hasPath(".tsx", ".jsx", ".swift", ".html", ".vue"),
		"api_and_data":        contains("fastapi", "express", "router", "@app.", "@router.", "graphql"),
		"authentication":      contains("oauth", "jwt", "firebaseauth", "authentication", "authorization"),
		"testing":             hasPath("test", "tests", "spec", "__tests__"),
		"continuous_delivery": hasPath(".github/workflows/", ".gitlab-ci"),
		"documentation":       hasPath("readme"),
		"containerization":    hasPath("dockerfile", "docker-compose.yml", "compose.yaml"),
	}
	return map[string]any{"product_types": types, "frameworks": frameworks, "review_dimensions": dimensions, "evidence_basis": "manifest names, source imports, and repository paths"}
}
