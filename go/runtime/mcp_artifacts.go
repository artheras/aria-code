package main

// aria.artifacts.list for aria-runtime mcp: the Go port of
// artifacts.recent_artifacts_all. It lists, newest first, what the Go tools
// wrote under <state>/artifacts/<kind>/, and what the Python tools recorded
// with *.metadata.json under the user's artifact roots (ARIA_ARTIFACT_ROOT
// or ~/Documents/Aria Code, and ARIA_USER_OUTPUT_ROOT/generated).

import (
	"context"
	"encoding/json"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const maxArtifactScan = 20000

type artifactItem struct {
	Kind         string  `json:"kind"`
	Status       string  `json:"status,omitempty"`
	Topic        string  `json:"topic,omitempty"`
	Path         string  `json:"path"`
	MetadataPath string  `json:"metadata_path,omitempty"`
	CreatedAt    string  `json:"created_at,omitempty"`
	Mtime        float64 `json:"mtime"`
	SizeBytes    int64   `json:"size_bytes"`
	Root         string  `json:"root"`
}

// pythonArtifactRoots are artifacts.artifact_roots without the project .ariarc.
func pythonArtifactRoots() []string {
	home, _ := os.UserHomeDir()
	documents := filepath.Join(home, "Documents", "Aria Code")
	main := documents
	if configured := envKey("ARIA_ARTIFACT_ROOT"); configured != "" {
		main = expandUser(configured)
	}
	output := documents
	if configured := envKey("ARIA_USER_OUTPUT_ROOT"); configured != "" {
		output = expandUser(configured)
	}
	return appendNew(nil, main, filepath.Join(output, "generated"))
}

func statTime(info fs.FileInfo) float64 {
	return float64(info.ModTime().UnixNano()) / float64(time.Second)
}

// scanArtifacts walks root with a shared budget, calling visit for each regular file.
func scanArtifacts(ctx context.Context, root string, budget *int, visit func(path string, info fs.FileInfo)) {
	filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil || ctx.Err() != nil || *budget <= 0 {
			if d != nil && d.IsDir() && err != nil {
				return fs.SkipDir
			}
			if *budget <= 0 || ctx.Err() != nil {
				return fs.SkipAll
			}
			return nil
		}
		if d.IsDir() {
			if path != root && strings.HasPrefix(d.Name(), ".") {
				return fs.SkipDir
			}
			return nil
		}
		if d.Type()&fs.ModeSymlink != 0 || !d.Type().IsRegular() {
			return nil
		}
		*budget--
		if info, err := d.Info(); err == nil {
			visit(path, info)
		}
		return nil
	})
}

// pythonArtifact reads one *.metadata.json record, as _artifact_entry_from_metadata does.
func pythonArtifact(path, root string) (artifactItem, bool) {
	data, err := os.ReadFile(path)
	if err != nil || len(data) > 1<<20 {
		return artifactItem{}, false
	}
	var meta struct {
		Kind      string `json:"kind"`
		Status    string `json:"status"`
		Symbol    string `json:"symbol"`
		CreatedAt string `json:"created_at"`
		Artifact  struct {
			Path         string `json:"path"`
			MetadataPath string `json:"metadata_path"`
			RawDataPath  string `json:"raw_data_path"`
			Category     string `json:"category"`
			Topic        string `json:"topic"`
		} `json:"artifact"`
	}
	if json.Unmarshal(data, &meta) != nil {
		return artifactItem{}, false
	}
	item := artifactItem{Kind: meta.Kind, Status: meta.Status, Topic: meta.Artifact.Topic, Path: meta.Artifact.Path,
		MetadataPath: path, CreatedAt: meta.CreatedAt, Root: root}
	if item.Kind == "" {
		item.Kind = meta.Artifact.Category
	}
	if item.Kind == "" {
		item.Kind = "artifact"
	}
	if item.Status == "" {
		item.Status = "unknown"
	}
	if item.Topic == "" {
		item.Topic = meta.Symbol
	}
	raw := meta.Artifact.RawDataPath
	if raw == "" {
		raw = strings.TrimSuffix(path, ".json") + ".raw_data.json"
	}
	for _, p := range []string{item.Path, path, raw} {
		if p == "" {
			continue
		}
		if info, err := os.Stat(p); err == nil && info.Mode().IsRegular() {
			item.SizeBytes += info.Size()
			if t := statTime(info); t > item.Mtime {
				item.Mtime = t
			}
		}
	}
	return item, true
}

func listArtifacts(ctx context.Context, limit int) []artifactItem {
	var items []artifactItem
	seen := map[string]bool{}
	budget := maxArtifactScan
	add := func(item artifactItem) {
		key := item.MetadataPath
		if key == "" {
			key = item.Path
		}
		if !seen[key] {
			seen[key] = true
			items = append(items, item)
		}
	}
	if state, err := stateRoot(); err == nil {
		root := filepath.Join(state, "artifacts")
		scanArtifacts(ctx, root, &budget, func(path string, info fs.FileInfo) {
			rel, _ := filepath.Rel(root, path)
			kind := strings.SplitN(filepath.ToSlash(rel), "/", 2)[0]
			if kind == filepath.ToSlash(rel) {
				kind = "artifact"
			}
			add(artifactItem{Kind: kind, Status: "complete", Path: path, Mtime: statTime(info), SizeBytes: info.Size(), Root: root})
		})
	}
	for _, root := range pythonArtifactRoots() {
		scanArtifacts(ctx, root, &budget, func(path string, info fs.FileInfo) {
			if strings.HasSuffix(path, ".metadata.json") {
				if item, ok := pythonArtifact(path, root); ok {
					add(item)
				}
			}
		})
	}
	sort.SliceStable(items, func(i, j int) bool { return items[i].Mtime > items[j].Mtime })
	if len(items) > limit {
		items = items[:limit]
	}
	return items
}

func mcpArtifactsList(ctx context.Context, args map[string]any) map[string]any {
	limit, err := intArg(args, "limit", 20, 1, 200)
	if err != nil {
		return mcpFail(err.Error())
	}
	items := listArtifacts(ctx, limit)
	out := make([]any, 0, len(items))
	for _, item := range items {
		out = append(out, item)
	}
	return map[string]any{"success": true, "artifacts": out}
}

func init() {
	mcpServerTools = append(mcpServerTools,
		readOnlyTool("aria.artifacts.list", "Recent files Aria produced (reports, charts, images, videos, generated code), newest first, with kind, path, size and time.",
			map[string]any{"limit": map[string]any{"type": "integer", "description": "How many to return, 1–200 (default 20)."}}))
	mcpServerHandlers["aria.artifacts.list"] = mcpArtifactsList
}
