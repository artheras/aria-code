package main

// Content fingerprints for the acceptance gate. A passing command is not
// evidence that it ran against the bytes still on disk. The snapshot is a
// SHA-256 of each regular file, or a recorded absence. It is not a Git
// revision, a permission digest, or an atomic filesystem picture.

import (
	"crypto/sha256"
	"encoding/hex"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const (
	revisionMaxPaths = 64
	revisionMaxBytes = 1 << 20
)

type revisionEntry struct {
	Path   string
	State  string
	SHA256 string
}

func revisionInputList(value any) ([]string, bool) {
	if value == nil {
		return nil, true
	}
	items, ok := value.([]any)
	if !ok || len(items) > revisionMaxPaths {
		return nil, false
	}
	out := make([]string, 0, len(items))
	for _, item := range items {
		text, ok := item.(string)
		if !ok || text == "" || len(text) > 512 {
			return nil, false
		}
		out = append(out, text)
	}
	return out, true
}

func boundRevisionPaths(paths, inputs []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, path := range append(append([]string{}, paths...), inputs...) {
		if seen[path] {
			continue
		}
		seen[path] = true
		out = append(out, path)
	}
	return out
}

func sameRevision(a, b []revisionEntry) bool {
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

func (g *acceptanceGate) fingerprint(paths []string) ([]revisionEntry, bool) {
	if g.revisionBroken || len(paths) == 0 || len(paths) > revisionMaxPaths {
		return nil, false
	}
	root, err := filepath.Abs(g.root)
	if err != nil {
		return nil, false
	}
	if resolved, err := filepath.EvalSymlinks(root); err == nil {
		root = resolved
	} else {
		return nil, false
	}
	out := make([]revisionEntry, 0, len(paths))
	for _, value := range paths {
		name, ok := workspaceRelative(root, value)
		if !ok {
			return nil, false
		}
		entry, ok := fingerprintFile(root, name)
		if !ok {
			return nil, false
		}
		out = append(out, entry)
	}
	return out, true
}

func workspaceRelative(root, value string) (string, bool) {
	if value == "" || len(value) > 512 || strings.Contains(value, "\\") {
		return "", false
	}
	if filepath.IsAbs(value) {
		abs, err := filepath.Abs(value)
		if err != nil {
			return "", false
		}
		rel, err := filepath.Rel(root, abs)
		if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			return "", false
		}
		value = filepath.ToSlash(rel)
	}
	if strings.Contains(value, ":") {
		return "", false
	}
	parts := strings.Split(value, "/")
	if len(parts) == 0 || len(parts) > 32 {
		return "", false
	}
	for _, part := range parts {
		if part == "" || part == "." || part == ".." {
			return "", false
		}
	}
	return strings.Join(parts, "/"), true
}

func fingerprintFile(root, name string) (revisionEntry, bool) {
	current := root
	parts := strings.Split(name, "/")
	var info os.FileInfo
	for i, part := range parts {
		next := filepath.Join(current, part)
		stat, err := os.Lstat(next)
		if err != nil {
			if os.IsNotExist(err) {
				return revisionEntry{Path: name, State: "absent"}, true
			}
			return revisionEntry{}, false
		}
		if stat.Mode()&os.ModeSymlink != 0 || !stat.Mode().IsRegular() && i == len(parts)-1 || (i < len(parts)-1 && !stat.IsDir()) {
			return revisionEntry{}, false
		}
		info = stat
		current = next
	}
	if info.Size() > revisionMaxBytes {
		return revisionEntry{}, false
	}
	file, err := os.OpenFile(current, os.O_RDONLY|noFollowFlag(), 0)
	if err != nil {
		return revisionEntry{}, false
	}
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !opened.Mode().IsRegular() || opened.Size() != info.Size() || opened.Size() > revisionMaxBytes {
		return revisionEntry{}, false
	}
	data, err := io.ReadAll(io.LimitReader(file, revisionMaxBytes+1))
	if err != nil || len(data) > revisionMaxBytes || int64(len(data)) != info.Size() {
		return revisionEntry{}, false
	}
	sum := sha256.Sum256(data)
	return revisionEntry{Path: name, State: "file", SHA256: hex.EncodeToString(sum[:])}, true
}
