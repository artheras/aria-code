package main

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode/utf8"
)

var workspaceIDPattern = regexp.MustCompile(`^ws_[a-f0-9]{24}$`)
var changeIDPattern = regexp.MustCompile(`^chg_[a-f0-9]{24}$`)

type workspaceManifest struct {
	SchemaVersion         string `json:"schema_version"`
	WorkspaceID           string `json:"workspace_id"`
	OwnerHash             string `json:"owner_hash"`
	RunHash               string `json:"run_hash"`
	ArchiveName           string `json:"archive_name"`
	FileCount             int    `json:"file_count"`
	ExtractedBytes        int    `json:"extracted_bytes"`
	SkippedSensitiveFiles int    `json:"skipped_sensitive_files"`
	CreatedAt             string `json:"created_at"`
	ExpiresAt             string `json:"expires_at"`
	ExecutionEnabled      bool   `json:"execution_enabled"`
	NetworkEnabled        bool   `json:"network_enabled"`
}

func (m workspaceManifest) public() map[string]any {
	return map[string]any{"schema_version": m.SchemaVersion, "workspace_id": m.WorkspaceID, "archive_name": m.ArchiveName, "file_count": m.FileCount, "extracted_bytes": m.ExtractedBytes, "skipped_sensitive_files": m.SkippedSensitiveFiles, "created_at": m.CreatedAt, "expires_at": m.ExpiresAt, "execution_enabled": m.ExecutionEnabled, "network_enabled": m.NetworkEnabled}
}

func subjectHash(value string) string {
	sum := sha256.Sum256([]byte(strings.TrimSpace(value)))
	return hex.EncodeToString(sum[:])
}
func nowString() string { return time.Now().UTC().Format(time.RFC3339Nano) }

func (s *service) workspaceDir(id string) (string, error) {
	if !workspaceIDPattern.MatchString(id) {
		return "", errors.New("Invalid workspace id.")
	}
	root, err := filepath.Abs(s.root)
	if err != nil {
		return "", errors.New("Invalid workspace path.")
	}
	dir := filepath.Join(root, id)
	if filepath.Dir(dir) != root {
		return "", errors.New("Invalid workspace path.")
	}
	return dir, nil
}

func (s *service) workspace(id, owner string) (workspaceManifest, error) {
	dir, err := s.workspaceDir(id)
	if err != nil {
		return workspaceManifest{}, err
	}
	data, err := os.ReadFile(filepath.Join(dir, ".aria-workspace.json"))
	if err != nil {
		return workspaceManifest{}, errors.New("Workspace not found.")
	}
	var manifest workspaceManifest
	if json.Unmarshal(data, &manifest) != nil || manifest.WorkspaceID != id || len(manifest.OwnerHash) != 64 {
		return workspaceManifest{}, errors.New("Workspace not found.")
	}
	expected := subjectHash(owner)
	if subtle.ConstantTimeCompare([]byte(expected), []byte(manifest.OwnerHash)) != 1 {
		return workspaceManifest{}, errors.New("Workspace not found.")
	}
	if expires, err := time.Parse(time.RFC3339Nano, manifest.ExpiresAt); err == nil && time.Now().After(expires) {
		return workspaceManifest{}, errors.New("Workspace not found.")
	}
	return manifest, nil
}

func (s *service) cleanupExpired(now time.Time) int {
	entries, err := os.ReadDir(s.root)
	if err != nil {
		return 0
	}
	removed := 0
	for _, entry := range entries {
		if !entry.IsDir() || !workspaceIDPattern.MatchString(entry.Name()) {
			continue
		}
		dir, err := s.workspaceDir(entry.Name())
		if err != nil {
			continue
		}
		data, err := os.ReadFile(filepath.Join(dir, ".aria-workspace.json"))
		if err != nil {
			continue
		}
		var manifest workspaceManifest
		if json.Unmarshal(data, &manifest) != nil {
			continue
		}
		expires, err := time.Parse(time.RFC3339Nano, manifest.ExpiresAt)
		if err != nil || expires.After(now) {
			continue
		}
		if os.RemoveAll(dir) == nil {
			removed++
		}
	}
	return removed
}

func (s *service) createWorkspaceFromArchive(payload []byte, filename, owner, run string) (workspaceManifest, error) {
	if strings.TrimSpace(owner) == "" || strings.TrimSpace(run) == "" {
		return workspaceManifest{}, errors.New("owner_id and run_id are required.")
	}
	if len(payload) == 0 || len(payload) > maxArchiveBytes {
		return workspaceManifest{}, errors.New("Project archive is empty or exceeds the upload limit.")
	}
	entries, err := archiveEntries(payload, filename)
	if err != nil {
		return workspaceManifest{}, err
	}
	var id string
	for i := 0; i < 3; i++ {
		id = "ws_" + randomHex(12)
		dir, _ := s.workspaceDir(id)
		if _, e := os.Stat(dir); errors.Is(e, os.ErrNotExist) {
			break
		}
	}
	finalDir, err := s.workspaceDir(id)
	if err != nil {
		return workspaceManifest{}, err
	}
	staging, err := os.MkdirTemp(s.root, "."+id+"-")
	if err != nil {
		return workspaceManifest{}, errors.New("Could not create isolated workspace.")
	}
	committed := false
	defer func() {
		if !committed {
			_ = os.RemoveAll(staging)
		}
	}()
	sourceRoot := filepath.Join(staging, "source")
	if err := os.Mkdir(sourceRoot, 0700); err != nil {
		return workspaceManifest{}, errors.New("Could not create isolated workspace.")
	}
	count, total, skipped := 0, 0, 0
	for _, entry := range entries {
		rel, err := safeArchivePath(entry.Path)
		if err != nil {
			return workspaceManifest{}, err
		}
		if ignoredArchivePath(rel) {
			continue
		}
		if sensitiveArchivePath(rel) {
			skipped++
			continue
		}
		count++
		total += len(entry.Data)
		if count > maxArchiveFiles || total > maxExtractedBytes {
			return workspaceManifest{}, errors.New("Expanded project exceeds workspace limits.")
		}
		target := filepath.Join(sourceRoot, filepath.FromSlash(rel))
		if !within(sourceRoot, target) {
			return workspaceManifest{}, errors.New("Archive path escapes the workspace.")
		}
		if err := os.MkdirAll(filepath.Dir(target), 0700); err != nil {
			return workspaceManifest{}, errors.New("Could not create isolated workspace.")
		}
		if err := os.WriteFile(target, entry.Data, 0600); err != nil {
			return workspaceManifest{}, errors.New("Could not create isolated workspace.")
		}
	}
	if count == 0 {
		return workspaceManifest{}, errors.New("No safe project files were found.")
	}
	created := time.Now().UTC()
	manifest := workspaceManifest{SchemaVersion: "aria.workspace.v1", WorkspaceID: id, OwnerHash: subjectHash(owner), RunHash: subjectHash(run), ArchiveName: archiveBase(filename), FileCount: count, ExtractedBytes: total, SkippedSensitiveFiles: skipped, CreatedAt: created.Format(time.RFC3339Nano), ExpiresAt: created.Add(s.ttl).Format(time.RFC3339Nano), ExecutionEnabled: false, NetworkEnabled: false}
	metadata, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return workspaceManifest{}, errors.New("Could not create isolated workspace.")
	}
	if err = os.WriteFile(filepath.Join(staging, ".aria-workspace.json"), metadata, 0600); err != nil {
		return workspaceManifest{}, errors.New("Could not create isolated workspace.")
	}
	if err = os.Rename(staging, finalDir); err != nil {
		return workspaceManifest{}, errors.New("Could not create isolated workspace.")
	}
	committed = true
	return manifest, nil
}

func archiveBase(name string) string {
	base := path.Base(strings.ReplaceAll(name, "\\", "/"))
	if len(base) > 180 {
		base = base[:180]
	}
	return base
}

func randomHex(size int) string {
	buf := make([]byte, size)
	if _, err := rand.Read(buf); err != nil {
		panic("secure random unavailable")
	}
	return hex.EncodeToString(buf)
}

func within(root, target string) bool {
	r, err := filepath.Abs(root)
	if err != nil {
		return false
	}
	t, err := filepath.Abs(target)
	if err != nil {
		return false
	}
	rel, err := filepath.Rel(r, t)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

func (s *service) sourcePath(id, owner, relative string) (string, error) {
	if _, err := s.workspace(id, owner); err != nil {
		return "", errors.New("Workspace not found.")
	}
	relative = strings.ReplaceAll(relative, "\\", "/")
	clean := path.Clean(relative)
	if path.IsAbs(relative) || clean == ".." || strings.HasPrefix(clean, "../") || strings.Contains(clean, ":") {
		return "", errors.New("Path must stay inside the workspace.")
	}
	dir, err := s.workspaceDir(id)
	if err != nil {
		return "", err
	}
	root := filepath.Join(dir, "source")
	target := filepath.Join(root, filepath.FromSlash(clean))
	if !within(root, target) {
		return "", errors.New("Path must stay inside the workspace.")
	}
	// Reject symbolic links at every existing component so later reads/writes cannot
	// escape the extracted tree through a link created outside the service.
	current := root
	rel, err := filepath.Rel(root, target)
	if err != nil {
		return "", errors.New("Path must stay inside the workspace.")
	}
	parts := strings.Split(filepath.ToSlash(rel), "/")
	for _, part := range parts {
		if part == "." || part == "" {
			continue
		}
		current = filepath.Join(current, part)
		info, e := os.Lstat(current)
		if errors.Is(e, os.ErrNotExist) {
			continue
		}
		if e != nil {
			return "", e
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return "", errors.New("Path must stay inside the workspace.")
		}
	}
	return target, nil
}

func (s *service) workspaceRead(id, owner, relative string, offset, limit int) (map[string]any, error) {
	target, err := s.sourcePath(id, owner, relative)
	if err != nil {
		return nil, err
	}
	info, err := os.Stat(target)
	if err != nil {
		return nil, errors.New("File not found.")
	}
	if !info.Mode().IsRegular() {
		return nil, errors.New("Not a file.")
	}
	if info.Size() > 2<<20 {
		return nil, errors.New("File too large (max 2 MB). Use offset/limit parameters to read sections.")
	}
	data, err := os.ReadFile(target)
	if err != nil {
		return nil, err
	}
	text := strings.ToValidUTF8(string(data), "�")
	lines := splitTextLines(text)
	total := len(lines)
	if offset == 0 && limit == 0 && info.Size() > 500000 {
		limit = 500
	}
	start := min(offset, total)
	end := total
	if limit > 0 {
		end = min(start+limit, total)
	}
	var b strings.Builder
	for i := start; i < end; i++ {
		if i > start {
			b.WriteByte('\n')
		}
		fmt.Fprintf(&b, "%4d│ %s", i+1, lines[i])
	}
	if limit > 0 && end < total {
		fmt.Fprintf(&b, "\n... [%d of %d lines shown — use offset/limit to read more]", end-start, total)
	}
	content := b.String()
	content = truncateRunes(content, 30000)
	return map[string]any{"success": true, "path": relative, "lines": end - start, "content": content}, nil
}

func (s *service) workspaceList(id, owner, relative, pattern string) (map[string]any, error) {
	target, err := s.sourcePath(id, owner, relative)
	if err != nil {
		return nil, err
	}
	info, err := os.Stat(target)
	if err != nil {
		return nil, errors.New("Path not found.")
	}
	items := make([]map[string]any, 0)
	if info.Mode().IsRegular() {
		result, err := s.workspaceRead(id, owner, relative, 0, 0)
		if err != nil {
			return nil, err
		}
		items = append(items, map[string]any{"name": filepath.Base(target), "type": "file", "size": info.Size()})
		return map[string]any{"success": true, "path": relative, "pattern": pattern, "count": 1, "items": items, "content": result["content"]}, nil
	}
	paths := []string{}
	if strings.Contains(pattern, "**") {
		err = filepath.WalkDir(target, func(current string, entry fs.DirEntry, walkErr error) error {
			if walkErr != nil {
				return nil
			}
			if current == target {
				return nil
			}
			rel, relErr := filepath.Rel(target, current)
			if relErr != nil {
				return nil
			}
			rel = filepath.ToSlash(rel)
			if entry.IsDir() && entry.Type()&os.ModeSymlink == 0 && ignoredArchivePath(rel) {
				return filepath.SkipDir
			}
			if globMatch(pattern, rel) {
				paths = append(paths, current)
			}
			return nil
		})
	} else {
		children, readErr := os.ReadDir(target)
		if readErr != nil {
			return nil, readErr
		}
		for _, entry := range children {
			match, matchErr := path.Match(pattern, entry.Name())
			if matchErr != nil {
				return nil, errors.New("Invalid file pattern.")
			}
			if match {
				paths = append(paths, filepath.Join(target, entry.Name()))
			}
		}
	}
	if err != nil {
		return nil, err
	}
	sort.Strings(paths)
	for _, current := range paths {
		if len(items) >= 100 {
			break
		}
		rel, relErr := filepath.Rel(target, current)
		if relErr != nil {
			continue
		}
		rel = filepath.ToSlash(rel)
		if _, err := s.sourcePath(id, owner, filepath.ToSlash(filepath.Join(relative, rel))); err != nil {
			continue
		}
		info, infoErr := os.Stat(current)
		if infoErr != nil {
			continue
		}
		kind, size := "file", int64(0)
		if info.IsDir() {
			kind = "dir"
		} else {
			size = info.Size()
		}
		items = append(items, map[string]any{"name": rel, "type": kind, "size": size})
	}
	return map[string]any{"success": true, "path": relative, "pattern": pattern, "count": len(items), "items": items}, nil
}

func (s *service) workspaceSearch(id, owner, relative, pattern, fileGlob string) (map[string]any, error) {
	target, err := s.sourcePath(id, owner, relative)
	if err != nil {
		return nil, err
	}
	if _, err = os.Stat(target); err != nil {
		return nil, errors.New("Path not found.")
	}
	query, err := regexp.Compile("(?i)" + pattern)
	if err != nil {
		return nil, errors.New("Invalid search pattern.")
	}
	files := []string{}
	_ = filepath.WalkDir(target, func(current string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return nil
		}
		if entry.IsDir() {
			if current != target && ignoredArchivePath(strings.TrimPrefix(filepath.ToSlash(current), filepath.ToSlash(target)+"/")) {
				return filepath.SkipDir
			}
			return nil
		}
		rel, _ := filepath.Rel(target, current)
		rel = filepath.ToSlash(rel)
		if globMatch(fileGlob, rel) {
			files = append(files, current)
		}
		if len(files) >= 200 {
			return filepath.SkipAll
		}
		return nil
	})
	sort.Strings(files)
	matches := make([]map[string]any, 0)
	budget := int64(0)
	for _, file := range files {
		if len(matches) >= 50 {
			break
		}
		if _, err := s.sourcePath(id, owner, filepath.ToSlash(filepath.Join(relative, strings.TrimPrefix(file, target+string(filepath.Separator))))); err != nil {
			continue
		}
		info, err := os.Stat(file)
		if err != nil || !info.Mode().IsRegular() || info.Size() > 5<<20 {
			continue
		}
		budget += info.Size()
		if budget > 5<<20 {
			break
		}
		data, err := os.ReadFile(file)
		if err != nil {
			continue
		}
		for number, line := range splitTextLines(strings.ToValidUTF8(string(data), "�")) {
			if query.MatchString(line) {
				content := strings.TrimSpace(line)
				content = truncateRunes(content, 200)
				matches = append(matches, map[string]any{"file": filepath.ToSlash(strings.TrimPrefix(file, target+string(filepath.Separator))), "line": number + 1, "content": content})
				if len(matches) >= 50 {
					break
				}
			}
		}
	}
	return map[string]any{"success": true, "pattern": pattern, "path": relative, "count": len(matches), "matches": matches}, nil
}

func splitTextLines(text string) []string {
	text = strings.ReplaceAll(text, "\r\n", "\n")
	text = strings.ReplaceAll(text, "\r", "\n")
	if text == "" {
		return nil
	}
	lines := strings.Split(text, "\n")
	if strings.HasSuffix(text, "\n") {
		lines = lines[:len(lines)-1]
	}
	return lines
}

func globMatch(pattern, name string) bool {
	pattern = path.Clean(strings.ReplaceAll(pattern, "\\", "/"))
	name = path.Clean(name)
	if pattern == "." {
		return name == "."
	}
	patternParts := strings.Split(pattern, "/")
	nameParts := strings.Split(name, "/")
	if len(patternParts) > 256 || len(nameParts) > 256 {
		return false
	}
	seen := make(map[[2]int]bool)
	results := make(map[[2]int]bool)
	var match func(int, int) bool
	match = func(patternIndex, nameIndex int) bool {
		key := [2]int{patternIndex, nameIndex}
		if seen[key] {
			return results[key]
		}
		seen[key] = true
		if patternIndex == len(patternParts) {
			results[key] = nameIndex == len(nameParts)
			return results[key]
		}
		if patternParts[patternIndex] == "**" {
			if match(patternIndex+1, nameIndex) {
				results[key] = true
				return true
			}
			results[key] = nameIndex < len(nameParts) && match(patternIndex, nameIndex+1)
			return results[key]
		}
		if nameIndex >= len(nameParts) {
			results[key] = false
			return false
		}
		matched, err := path.Match(patternParts[patternIndex], nameParts[nameIndex])
		results[key] = err == nil && matched && match(patternIndex+1, nameIndex+1)
		return results[key]
	}
	return match(0, 0)
}

type changeRecord struct {
	SchemaVersion  string `json:"schema_version"`
	ChangeID       string `json:"change_id"`
	WorkspaceID    string `json:"workspace_id"`
	Path           string `json:"path"`
	Operation      string `json:"operation"`
	Status         string `json:"status"`
	BaseSHA256     string `json:"base_sha256"`
	ProposedSHA256 string `json:"proposed_sha256"`
	CreatedAt      string `json:"created_at"`
	UpdatedAt      string `json:"updated_at"`
	Diff           string `json:"diff"`
}

func (s *service) changeDir(workspaceID, changeID string) (string, error) {
	if !changeIDPattern.MatchString(changeID) {
		return "", errors.New("Invalid change id.")
	}
	dir, err := s.workspaceDir(workspaceID)
	if err != nil {
		return "", err
	}
	root := filepath.Join(dir, ".aria-changes")
	if err = os.MkdirAll(root, 0700); err != nil {
		return "", err
	}
	return filepath.Join(root, changeID), nil
}
func readChange(dir string) (changeRecord, error) {
	var change changeRecord
	data, err := os.ReadFile(filepath.Join(dir, "change.json"))
	if err != nil {
		return change, errors.New("Change proposal not found.")
	}
	if json.Unmarshal(data, &change) != nil {
		return change, errors.New("Change proposal not found.")
	}
	return change, nil
}
func writeChange(dir string, change changeRecord) error {
	data, err := json.MarshalIndent(change, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, "change.json"), data, 0600)
}
func digest(data []byte) string { sum := sha256.Sum256(data); return hex.EncodeToString(sum[:]) }

func (s *service) propose(workspaceID, owner, relative, content string, expected *string) (changeRecord, error) {
	rel := path.Clean(strings.ReplaceAll(relative, "\\", "/"))
	if rel == "." || path.IsAbs(relative) || rel == ".." || strings.HasPrefix(rel, "../") || strings.Contains(rel, ":") {
		return changeRecord{}, errors.New("Change path must be relative to the workspace.")
	}
	if sensitiveArchivePath(rel) {
		return changeRecord{}, errors.New("Sensitive configuration and key files cannot be written.")
	}
	proposed := []byte(content)
	if len(proposed) > 1<<20 {
		return changeRecord{}, errors.New("Proposed file exceeds the 1 MB text-change limit.")
	}
	target, err := s.sourcePath(workspaceID, owner, rel)
	if err != nil {
		return changeRecord{}, err
	}
	info, statErr := os.Stat(target)
	exists := statErr == nil
	if statErr != nil && !errors.Is(statErr, os.ErrNotExist) {
		return changeRecord{}, statErr
	}
	if exists && !info.Mode().IsRegular() {
		return changeRecord{}, errors.New("Change target is not a regular file.")
	}
	before := []byte{}
	if exists {
		before, err = os.ReadFile(target)
		if err != nil || !utf8Valid(before) {
			return changeRecord{}, errors.New("Only UTF-8 text files can be changed.")
		}
	}
	baseHash := digest(before)
	if expected != nil && *expected != "" && !constantEqual(*expected, baseHash) {
		return changeRecord{}, errors.New("File changed since it was reviewed; refresh before proposing a patch.")
	}
	if bytesEqual(before, proposed) {
		return changeRecord{}, errors.New("Proposed content is identical to the current file.")
	}
	changeID := "chg_" + randomHex(12)
	dir, err := s.changeDir(workspaceID, changeID)
	if err != nil {
		return changeRecord{}, err
	}
	if err = os.Mkdir(dir, 0700); err != nil {
		return changeRecord{}, err
	}
	created := nowString()
	change := changeRecord{SchemaVersion: "aria.workspace-change.v1", ChangeID: changeID, WorkspaceID: workspaceID, Path: rel, Operation: "create", Status: "proposed", BaseSHA256: baseHash, ProposedSHA256: digest(proposed), CreatedAt: created, UpdatedAt: created, Diff: unifiedTextDiff(rel, string(before), content)}
	if exists {
		change.Operation = "update"
	}
	if err = os.WriteFile(filepath.Join(dir, "before.bin"), before, 0600); err != nil {
		_ = os.RemoveAll(dir)
		return changeRecord{}, err
	}
	if err = os.WriteFile(filepath.Join(dir, "proposed.txt"), proposed, 0600); err != nil {
		_ = os.RemoveAll(dir)
		return changeRecord{}, err
	}
	if err = writeChange(dir, change); err != nil {
		_ = os.RemoveAll(dir)
		return changeRecord{}, err
	}
	return change, nil
}

func utf8Valid(data []byte) bool  { return utf8.Valid(data) }
func bytesEqual(a, b []byte) bool { return bytes.Equal(a, b) }
func constantEqual(a, b string) bool {
	return len(a) == len(b) && subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1
}

func unifiedTextDiff(name, before, after string) string {
	oldLines := strings.Split(strings.TrimSuffix(before, "\n"), "\n")
	newLines := strings.Split(strings.TrimSuffix(after, "\n"), "\n")
	if before == "" {
		oldLines = nil
	}
	if after == "" {
		newLines = nil
	}
	prefix := 0
	for prefix < len(oldLines) && prefix < len(newLines) && oldLines[prefix] == newLines[prefix] {
		prefix++
	}
	suffix := 0
	for suffix < len(oldLines)-prefix && suffix < len(newLines)-prefix && oldLines[len(oldLines)-1-suffix] == newLines[len(newLines)-1-suffix] {
		suffix++
	}
	if prefix == len(oldLines) && prefix == len(newLines) {
		return ""
	}
	oldStart := max(1, prefix+1)
	newStart := max(1, prefix+1)
	oldCount := len(oldLines) - prefix - suffix
	newCount := len(newLines) - prefix - suffix
	var b strings.Builder
	fmt.Fprintf(&b, "--- a/%s\n+++ b/%s\n@@ -%d,%d +%d,%d @@\n", name, name, oldStart, oldCount, newStart, newCount)
	for _, line := range oldLines[prefix : len(oldLines)-suffix] {
		b.WriteByte('-')
		b.WriteString(line)
		b.WriteByte('\n')
	}
	for _, line := range newLines[prefix : len(newLines)-suffix] {
		b.WriteByte('+')
		b.WriteString(line)
		b.WriteByte('\n')
	}
	result := b.String()
	if len(result) > 120000 {
		result = result[:120000] + "\n... diff truncated ...\n"
	}
	return result
}

func (s *service) change(workspaceID, changeID, owner string) (changeRecord, error) {
	if _, err := s.workspace(workspaceID, owner); err != nil {
		return changeRecord{}, err
	}
	dir, err := s.changeDir(workspaceID, changeID)
	if err != nil {
		return changeRecord{}, err
	}
	return readChange(dir)
}

func (s *service) applyOrRollback(workspaceID, changeID, owner, action string) (int, changeRecord, error) {
	change, err := s.change(workspaceID, changeID, owner)
	if err != nil {
		return 404, change, errors.New("Change proposal not found.")
	}
	target, err := s.sourcePath(workspaceID, owner, change.Path)
	if err != nil {
		return 409, change, err
	}
	dir, err := s.changeDir(workspaceID, changeID)
	if err != nil {
		return 409, change, err
	}
	current, err := os.ReadFile(target)
	if errors.Is(err, os.ErrNotExist) {
		current = []byte{}
	} else if err != nil {
		return 409, change, err
	}
	if action == "apply" {
		if change.Status != "proposed" {
			return 409, change, errors.New("Only a proposed change can be applied.")
		}
		if !constantEqual(digest(current), change.BaseSHA256) {
			return 409, change, errors.New("File changed after the proposal; approval is stale.")
		}
		proposed, err := os.ReadFile(filepath.Join(dir, "proposed.txt"))
		if err != nil || !constantEqual(digest(proposed), change.ProposedSHA256) {
			return 409, change, errors.New("Stored proposal failed its integrity check.")
		}
		if err = os.MkdirAll(filepath.Dir(target), 0700); err != nil {
			return 409, change, err
		}
		tmp, err := os.CreateTemp(filepath.Dir(target), "."+filepath.Base(target)+".")
		if err != nil {
			return 409, change, err
		}
		tmpName := tmp.Name()
		defer os.Remove(tmpName)
		if _, err = tmp.Write(proposed); err != nil {
			tmp.Close()
			return 409, change, err
		}
		if err = tmp.Sync(); err != nil {
			tmp.Close()
			return 409, change, err
		}
		if err = tmp.Close(); err != nil {
			return 409, change, err
		}
		_ = os.Chmod(tmpName, 0600)
		if err = os.Rename(tmpName, target); err != nil {
			return 409, change, err
		}
		change.Status = "applied"
	} else {
		if change.Status != "applied" {
			return 409, change, errors.New("Only an applied change can be rolled back.")
		}
		if !constantEqual(digest(current), change.ProposedSHA256) {
			return 409, change, errors.New("File changed after apply; automatic rollback would overwrite newer work.")
		}
		before, err := os.ReadFile(filepath.Join(dir, "before.bin"))
		if err != nil {
			return 409, change, err
		}
		if change.Operation == "create" {
			if err = os.Remove(target); err != nil && !errors.Is(err, os.ErrNotExist) {
				return 409, change, err
			}
		} else {
			if err = os.WriteFile(target, before, 0600); err != nil {
				return 409, change, err
			}
		}
		change.Status = "rolled_back"
	}
	change.UpdatedAt = nowString()
	if err = writeChange(dir, change); err != nil {
		return 500, change, errors.New("Could not save change status.")
	}
	return 200, change, nil
}
