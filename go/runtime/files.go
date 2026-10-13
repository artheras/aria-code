package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"unicode/utf8"
)

func remoteScope() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv("ARIA_RUNTIME_SCOPE")), "remote")
}

// Scope is constructed only from host arguments. Provider parameters cannot
// add roots, disable path checks or approve a write. os.Root also protects the
// actual open/rename from a symlink replacement after the initial resolution.
type scope struct {
	workspace  string
	readRoots  []string
	writeRoots []string
	readOnly   bool
}
type scopedPath struct {
	root     *os.Root
	relative string
	absolute string
}

func (p *scopedPath) Close() { p.root.Close() }

func canonical(path string) (string, error) {
	path = lexicalPath(path)
	absolute, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	var missing []string
	cursor := absolute
	for {
		resolved, err := filepath.EvalSymlinks(cursor)
		if err == nil {
			for i := len(missing) - 1; i >= 0; i-- {
				resolved = filepath.Join(resolved, missing[i])
			}
			return lexicalPath(resolved), nil
		}
		if !os.IsNotExist(err) {
			return "", err
		}
		parent := filepath.Dir(cursor)
		if parent == cursor {
			return "", err
		}
		missing = append(missing, filepath.Base(cursor))
		cursor = parent
	}
}

// Rust canonicalize returns verbatim Windows paths; os.Getwd and legacy
// Python checkpoints usually use ordinary drive/UNC paths. Normalize only
// equivalent absolute spellings, without weakening symlink or root checks.
func normalWindowsPath(path string) string {
	if len(path) >= 8 && strings.EqualFold(path[:8], `\\?\UNC\`) {
		return `\\` + path[8:]
	}
	if len(path) >= 7 && path[:4] == `\\?\` && path[5:7] == `:\` && ((path[4] >= 'A' && path[4] <= 'Z') || (path[4] >= 'a' && path[4] <= 'z')) {
		return path[4:]
	}
	return path
}
func lexicalPath(path string) string {
	if runtime.GOOS == "windows" {
		path = normalWindowsPath(path)
	}
	return filepath.Clean(path)
}
func newScope(workspace string, reads, writes []string, local bool) (*scope, error) {
	root, err := canonical(workspace)
	if err != nil {
		return nil, err
	}
	s := &scope{workspace: root, readRoots: []string{root}, writeRoots: []string{root}}
	if local {
		if home, err := os.UserHomeDir(); err == nil {
			s.readRoots = append(s.readRoots, home)
		}
		s.readRoots = append(s.readRoots, os.TempDir())
		s.writeRoots = append(s.writeRoots, os.TempDir())
	}
	s.readRoots = append(s.readRoots, reads...)
	s.readRoots = append(s.readRoots, writes...)
	s.writeRoots = append(s.writeRoots, writes...)
	for _, roots := range [][]string{s.readRoots, s.writeRoots} {
		for i, root := range roots {
			value, err := canonical(root)
			if err != nil {
				return nil, err
			}
			roots[i] = value
		}
		sort.SliceStable(roots, func(i, j int) bool { return len(roots[i]) > len(roots[j]) })
	}
	return s, nil
}
func within(path, root string) (string, bool) {
	rel, err := filepath.Rel(root, path)
	return rel, err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) && !filepath.IsAbs(rel)
}
func (s *scope) open(path string, write bool) (*scopedPath, error) {
	if write && s.readOnly {
		return nil, errors.New("read-only session cannot modify files")
	}
	if path == "" {
		return nil, errors.New("missing path")
	}
	if strings.IndexByte(path, 0) >= 0 {
		return nil, errors.New("invalid path")
	}
	if path == "~" || strings.HasPrefix(path, "~/") {
		home, err := os.UserHomeDir()
		if err != nil {
			return nil, err
		}
		if path == "~" {
			path = home
		} else {
			path = filepath.Join(home, strings.TrimPrefix(path, "~/"))
		}
	}
	if !filepath.IsAbs(path) {
		path = filepath.Join(s.workspace, path)
	}
	resolved, err := canonical(path)
	if err != nil {
		return nil, err
	}
	for _, blocked := range []string{"/etc", "/sys", "/proc", "/dev", "/boot", "/root"} {
		if filepath.IsAbs(blocked) {
			base, err := canonical(blocked)
			if err == nil {
				if _, yes := within(resolved, base); yes {
					return nil, errors.New("access to system directories is denied")
				}
			}
		}
	}
	roots := s.readRoots
	if write {
		roots = s.writeRoots
	}
	for _, root := range roots {
		if relative, yes := within(resolved, root); yes {
			fd, err := os.OpenRoot(root)
			if err != nil {
				return nil, err
			}
			return &scopedPath{fd, relative, resolved}, nil
		}
	}
	return nil, fmt.Errorf("access denied: %s is outside the selected roots", resolved)
}

type toolResult struct {
	Success bool           `json:"success"`
	Data    map[string]any `json:"data,omitempty"`
	Error   string         `json:"error,omitempty"`
	Reused  bool           `json:"reused,omitempty"`
}

func failed(err error) toolResult { return toolResult{Error: err.Error()} }
func str(params map[string]any, key, fallback string) string {
	if value, ok := params[key].(string); ok {
		return value
	}
	return fallback
}
func integer(params map[string]any, key string, fallback int) int {
	if n, ok := params[key].(float64); ok {
		return int(n)
	}
	if n, ok := params[key].(int); ok {
		return n
	}
	return fallback
}
func noHostFlags(params map[string]any) error {
	for key := range params {
		if strings.HasPrefix(key, "_") {
			return fmt.Errorf("provider cannot set host field %s", key)
		}
	}
	return nil
}
func readRegular(p *scopedPath, limit int64) ([]byte, error) {
	info, err := p.root.Stat(p.relative)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, errors.New("path is not a regular file")
	}
	if info.Size() > limit {
		return nil, fmt.Errorf("file exceeds %d bytes; read a smaller file", limit)
	}
	f, err := p.root.Open(p.relative)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err = f.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, errors.New("path changed to a non-regular file")
	}
	body, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > limit {
		return nil, errors.New("file grew beyond read limit")
	}
	return body, nil
}
func capText(text string, limit int) string {
	chars := []rune(text)
	if len(chars) > limit {
		return string(chars[:limit]) + "\n[display truncated]"
	}
	return text
}
func fileRead(ctx context.Context, s *scope, params map[string]any) toolResult {
	if err := noHostFlags(params); err != nil {
		return failed(err)
	}
	p, err := s.open(str(params, "path", ""), false)
	if err != nil {
		return failed(err)
	}
	defer p.Close()
	body, err := readRegular(p, 2_000_000)
	if err != nil {
		return failed(err)
	}
	text := strings.ToValidUTF8(string(body), "�")
	text = strings.ReplaceAll(strings.ReplaceAll(text, "\r\n", "\n"), "\r", "\n")
	lines := strings.Split(strings.TrimSuffix(text, "\n"), "\n")
	if text == "" {
		lines = nil
	}
	offset, limit := integer(params, "offset", 0), integer(params, "limit", 0)
	if offset < 0 || limit < 0 {
		return failed(errors.New("offset and limit must be nonnegative"))
	}
	if offset == 0 && limit == 0 {
		limit = 160
	}
	if offset > len(lines) {
		offset = len(lines)
	}
	end := len(lines)
	if limit > 0 && offset+limit < end {
		end = offset + limit
	}
	var out strings.Builder
	for i := offset; i < end; i++ {
		if i > offset {
			out.WriteByte('\n')
		}
		fmt.Fprintf(&out, "%4d│ %s", i+1, lines[i])
	}
	if end < len(lines) {
		fmt.Fprintf(&out, "\n... [%d of %d lines shown — use offset/limit to read more]", end-offset, len(lines))
	}
	return toolResult{Success: true, Data: map[string]any{"path": p.absolute, "lines": end - offset, "content": capText(out.String(), 30_000)}}
}
func globRegex(pattern string) (*regexp.Regexp, error) {
	if len(pattern) > 1024 {
		return nil, errors.New("glob is too long")
	}
	pattern = filepath.ToSlash(pattern)
	var b strings.Builder
	b.WriteByte('^')
	for i := 0; i < len(pattern); i++ {
		switch pattern[i] {
		case '*':
			if i+1 < len(pattern) && pattern[i+1] == '*' {
				i++
				if i+1 < len(pattern) && pattern[i+1] == '/' {
					i++
					b.WriteString("(?:.*/)?")
				} else {
					b.WriteString(".*")
				}
			} else {
				b.WriteString("[^/]*")
			}
		case '?':
			b.WriteString("[^/]")
		case '[':
			end := strings.IndexByte(pattern[i+1:], ']')
			if end < 0 {
				return nil, errors.New("invalid glob character class")
			}
			end += i + 1
			part := pattern[i : end+1]
			if strings.HasPrefix(part, "[!") {
				part = "[^" + part[2:]
			}
			b.WriteString(part)
			i = end
		default:
			b.WriteString(regexp.QuoteMeta(pattern[i : i+1]))
		}
	}
	b.WriteByte('$')
	return regexp.Compile(b.String())
}
func walk(ctx context.Context, p *scopedPath, pattern string, limit int, visit func(string, fs.DirEntry) error) error {
	re, err := globRegex(pattern)
	if err != nil {
		return err
	}
	count := 0
	return fs.WalkDir(p.root.FS(), filepath.ToSlash(p.relative), func(path string, entry fs.DirEntry, err error) error {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(p.relative, filepath.FromSlash(path))
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if rel == "." {
			return nil
		}
		prune := entry.IsDir() && !strings.Contains(pattern, "**") && strings.Count(rel, "/") >= strings.Count(pattern, "/")
		if !re.MatchString(rel) {
			if prune {
				return fs.SkipDir
			}
			return nil
		}
		if count >= limit {
			return fs.SkipAll
		}
		count++
		if err := visit(rel, entry); err != nil {
			return err
		}
		if prune {
			return fs.SkipDir
		}
		return nil
	})
}
func fileList(ctx context.Context, s *scope, params map[string]any) toolResult {
	if err := noHostFlags(params); err != nil {
		return failed(err)
	}
	p, err := s.open(str(params, "path", "."), false)
	if err != nil {
		return failed(err)
	}
	defer p.Close()
	pattern := str(params, "pattern", "*")
	items := []map[string]any{}
	info, err := p.root.Stat(p.relative)
	if err != nil {
		return failed(err)
	}
	if info.Mode().IsRegular() {
		items = append(items, map[string]any{"name": filepath.Base(p.absolute), "type": "file", "size": info.Size()})
	} else if info.IsDir() {
		err = walk(ctx, p, pattern, 100, func(rel string, entry fs.DirEntry) error {
			info, err := safeEntry(s, p, rel)
			if err != nil {
				return nil
			}
			kind, size := "file", info.Size()
			if info.IsDir() {
				kind, size = "dir", 0
			}
			if !info.IsDir() && !info.Mode().IsRegular() {
				return nil
			}
			items = append(items, map[string]any{"name": filepath.FromSlash(rel), "type": kind, "size": size})
			return nil
		})
		if err != nil {
			return failed(err)
		}
	} else {
		return failed(errors.New("path is not a regular file or directory"))
	}
	return toolResult{Success: true, Data: map[string]any{"path": p.absolute, "pattern": pattern, "count": len(items), "items": items}}
}

type fileChange struct {
	path    string
	before  []byte
	existed bool
	after   []byte
	mode    fs.FileMode
	sha     string
}

func propose(s *scope, tool string, params map[string]any) (fileChange, error) {
	if err := noHostFlags(params); err != nil {
		return fileChange{}, err
	}
	if _, err := stageOnly(params); err != nil {
		return fileChange{}, err
	}
	p, err := s.open(str(params, "path", ""), true)
	if err != nil {
		return fileChange{}, err
	}
	defer p.Close()
	c := fileChange{path: p.absolute, mode: 0644}
	info, err := p.root.Stat(p.relative)
	if err == nil {
		c.before, err = readRegular(p, 4<<20)
		if err != nil {
			return c, err
		}
		c.existed = true
		c.mode = info.Mode().Perm()
	} else if !os.IsNotExist(err) {
		return c, err
	}
	hash := sha256.Sum256(c.before)
	c.sha = hex.EncodeToString(hash[:])
	switch tool {
	case "write_file":
		value, ok := params["content"].(string)
		if !ok {
			return c, errors.New("content must be a string")
		}
		c.after = []byte(value)
	case "edit_file":
		if !c.existed {
			return c, errors.New("file not found")
		}
		old, new := str(params, "old_string", str(params, "old_str", "")), str(params, "new_string", str(params, "new_str", ""))
		if old == "" {
			return c, errors.New("old_string is required; read the file first")
		}
		if !utf8.Valid(c.before) {
			return c, errors.New("edit_file requires UTF-8 source")
		}
		// Reads expose logical lines; retain a CRLF file's physical line endings.
		if strings.Contains(string(c.before), "\r\n") {
			old = strings.ReplaceAll(strings.ReplaceAll(old, "\r\n", "\n"), "\n", "\r\n")
			new = strings.ReplaceAll(strings.ReplaceAll(new, "\r\n", "\n"), "\n", "\r\n")
		}
		count := strings.Count(string(c.before), old)
		all, _ := params["replace_all"].(bool)
		if count == 0 {
			return c, errors.New("old_string was not found; read_file first to copy exact text")
		}
		if count != 1 && !all {
			return c, fmt.Errorf("old_string matches %d places; include unique surrounding text or explicitly set replace_all", count)
		}
		n := 1
		if all {
			n = -1
		}
		c.after = []byte(strings.Replace(string(c.before), old, new, n))
	case "multi_edit":
		if !c.existed {
			return c, errors.New("file not found")
		}
		edits, ok := params["edits"].([]any)
		if !ok || len(edits) == 0 || len(edits) > 100 {
			return c, errors.New("edits must contain between 1 and 100 edit objects")
		}
		working := string(c.before)
		if !utf8.Valid(c.before) {
			return c, errors.New("multi_edit requires UTF-8 source")
		}
		for i, raw := range edits {
			edit, ok := raw.(map[string]any)
			if !ok {
				return c, fmt.Errorf("edit %d is not an object; no changes applied", i+1)
			}
			if err := noHostFlags(edit); err != nil {
				return c, err
			}
			old, ok := edit["old_string"].(string)
			if !ok || old == "" {
				return c, fmt.Errorf("edit %d requires old_string", i+1)
			}
			new, ok := edit["new_string"].(string)
			if !ok {
				return c, fmt.Errorf("edit %d requires new_string", i+1)
			}
			if strings.Contains(string(c.before), "\r\n") {
				old = strings.ReplaceAll(strings.ReplaceAll(old, "\r\n", "\n"), "\n", "\r\n")
				new = strings.ReplaceAll(strings.ReplaceAll(new, "\r\n", "\n"), "\n", "\r\n")
			}
			count := strings.Count(working, old)
			all, _ := edit["replace_all"].(bool)
			if count == 0 || (count > 1 && !all) {
				return c, fmt.Errorf("edit %d has %d matches; no changes applied", i+1, count)
			}
			n := 1
			if all {
				n = -1
			}
			working = strings.Replace(working, old, new, n)
			if len(working) > 4<<20 {
				return c, errors.New("file content exceeds 4 MiB")
			}
		}
		if working == string(c.before) {
			return c, errors.New("edits produced no changes")
		}
		c.after = []byte(working)
	default:
		return c, errors.New("unknown file mutation")
	}
	if len(c.after) > 4<<20 {
		return c, errors.New("file content exceeds 4 MiB")
	}
	return c, nil
}
func applyChange(s *scope, c fileChange) toolResult {
	p, err := s.open(c.path, true)
	if err != nil {
		return failed(err)
	}
	defer p.Close()
	if p.absolute != c.path {
		return failed(errors.New("file path resolves to a different target since review"))
	}
	current, err := readRegular(p, 4<<20)
	if os.IsNotExist(err) && !c.existed {
		current = nil
	} else if err != nil {
		return failed(err)
	}
	if (err == nil) != c.existed {
		return failed(errors.New("file existence changed during approval; review the new file first"))
	}
	hash := sha256.Sum256(current)
	if hex.EncodeToString(hash[:]) != c.sha {
		return failed(errors.New("file changed during approval; read it again before editing"))
	}
	parent := filepath.Dir(p.relative)
	if err := p.root.MkdirAll(parent, 0755); err != nil {
		return failed(err)
	}
	id, err := newID()
	if err != nil {
		return failed(err)
	}
	temporary := filepath.Join(parent, ".aria-edit-"+id)
	f, err := p.root.OpenFile(temporary, os.O_CREATE|os.O_EXCL|os.O_WRONLY, c.mode)
	if err != nil {
		return failed(err)
	}
	defer p.root.Remove(temporary)
	if err := f.Chmod(c.mode.Perm()); err != nil {
		f.Close()
		return failed(err)
	}
	if _, err := f.Write(c.after); err != nil {
		f.Close()
		return failed(err)
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return failed(err)
	}
	if err := f.Close(); err != nil {
		return failed(err)
	}
	if err := p.root.Rename(temporary, p.relative); err != nil {
		return failed(err)
	}
	return toolResult{Success: true, Data: map[string]any{"path": c.path, "bytes": len(c.after), "changed": string(c.before) != string(c.after)}}
}
