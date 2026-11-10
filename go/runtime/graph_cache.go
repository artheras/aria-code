package main

import (
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// cachedImpactLine is delivery._impact: the one-line hint from a graph that
// is already on disk. It never scans the tree. No cache, or a cache for a
// different root, is an empty line, the same as Python's cached_graph.
func cachedImpactLine(sources []string) string {
	var graph *projectGraph
	var targets []string
	for _, raw := range sources {
		if strings.TrimSpace(raw) == "" {
			continue
		}
		origin, relative, ok := repositoryOf(raw)
		if !ok {
			continue
		}
		loaded := loadCachedProjectGraph(origin)
		if loaded == nil || (graph != nil && loaded.root != graph.root) {
			continue
		}
		graph = loaded
		targets = append(targets, relative)
	}
	if graph == nil || len(targets) == 0 {
		return ""
	}
	return graph.impact(targets).reportLine()
}

func (r graphImpact) reportLine() string {
	if len(r.targets) == 0 || sameStrings(r.unresolved, r.targets) {
		return ""
	}
	reach := len(r.direct) + len(r.indirect)
	fileWord, testWord := "files", "tests"
	if reach == 1 {
		fileWord = "file"
	}
	if len(r.tests) == 1 {
		testWord = "test"
	}
	parts := []string{
		r.breadth(),
		fmt.Sprintf("%d %s depend on it", reach, fileWord),
		fmt.Sprintf("%d %s", len(r.tests), testWord),
	}
	if len(r.services) > 0 {
		parts = append(parts, strings.Join(r.services, ", "))
	}
	return strings.Join(parts, " · ")
}

func sameStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func repositoryOf(raw string) (origin, relative string, ok bool) {
	path := raw
	if !filepath.IsAbs(path) {
		abs, err := filepath.Abs(path)
		if err != nil {
			return "", "", false
		}
		path = abs
	}
	folder := path
	if info, err := os.Stat(path); err != nil || !info.IsDir() {
		folder = filepath.Dir(path)
	}
	for {
		if info, err := os.Stat(folder); err == nil && info.IsDir() {
			break
		}
		parent := filepath.Dir(folder)
		if parent == folder {
			return "", "", false
		}
		folder = parent
	}
	if top, common, found := gitRepository(folder); found {
		origin = top
		if filepath.Base(common) == ".git" {
			origin = filepath.Dir(common)
		}
		rel, err := filepath.Rel(top, resolveExisting(path))
		if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			return "", "", false
		}
		return origin, filepath.ToSlash(rel), true
	}
	for current := folder; ; current = filepath.Dir(current) {
		resolved := resolveExisting(current)
		if cache, err := graphCachePath(resolved); err == nil {
			if info, err := os.Stat(cache); err == nil && !info.IsDir() {
				rel, err := filepath.Rel(resolved, resolveExisting(path))
				if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
					return "", "", false
				}
				return resolved, filepath.ToSlash(rel), true
			}
		}
		parent := filepath.Dir(current)
		if parent == current {
			return "", "", false
		}
	}
}

func gitRepository(folder string) (top, common string, ok bool) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "git", "rev-parse", "--show-toplevel", "--git-common-dir")
	cmd.Dir = folder
	out, err := cmd.Output()
	if err != nil {
		return "", "", false
	}
	lines := strings.Split(strings.TrimRight(string(out), "\n"), "\n")
	if len(lines) < 2 || lines[0] == "" || lines[1] == "" {
		return "", "", false
	}
	top = resolveExisting(lines[0])
	common = lines[1]
	if !filepath.IsAbs(common) {
		common = filepath.Join(folder, common)
	}
	return top, resolveExisting(common), true
}

func resolveExisting(path string) string {
	if resolved, err := filepath.EvalSymlinks(path); err == nil {
		path = resolved
	}
	if abs, err := filepath.Abs(path); err == nil {
		return abs
	}
	return path
}

func graphCachePath(root string) (string, error) {
	home, err := stateRoot()
	if err != nil {
		return "", err
	}
	sum := sha1.Sum([]byte(root))
	digest := hex.EncodeToString(sum[:])[:12]
	return filepath.Join(home, "graphs", filepath.Base(root)+"-"+digest+".json"), nil
}

func loadCachedProjectGraph(root string) *projectGraph {
	resolved := resolveExisting(root)
	cache, err := graphCachePath(resolved)
	if err != nil {
		return nil
	}
	file, err := os.Open(cache)
	if err != nil {
		return nil
	}
	defer file.Close()
	body, err := io.ReadAll(io.LimitReader(file, 32<<20+1))
	if err != nil || len(body) > 32<<20 {
		return nil
	}
	var payload struct {
		Version  int                        `json:"version"`
		Root     string                     `json:"root"`
		Files    map[string]cachedGraphFile `json:"files"`
		Services map[string]string          `json:"services"`
		Refs     map[string][]string        `json:"refs"`
		Defs     map[string][]string        `json:"defs"`
	}
	if json.Unmarshal(body, &payload) != nil {
		return nil
	}
	if payload.Version != 1 || payload.Root != resolved {
		return nil
	}
	graph := &projectGraph{
		root: resolved, files: map[string]*graphFile{},
		defs: payload.Defs, refs: payload.Refs, services: payload.Services,
	}
	for name, item := range payload.Files {
		entry := &graphFile{imports: item.Imports}
		if entry.imports == nil {
			entry.imports = []string{}
		}
		for _, row := range item.Symbols {
			if len(row) < 4 {
				continue
			}
			symbolName, _ := row[0].(string)
			kind, _ := row[1].(string)
			parent, _ := row[3].(string)
			entry.symbols = append(entry.symbols, graphSymbol{name: symbolName, kind: kind, parent: parent})
		}
		graph.files[name] = entry
	}
	return graph
}

type cachedGraphFile struct {
	Imports []string `json:"imports"`
	Symbols [][]any  `json:"symbols"`
}
