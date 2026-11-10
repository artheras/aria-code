package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestCachedImpactLineUsesTheOnDiskGraph(t *testing.T) {
	root := resolveExisting(t.TempDir())
	t.Setenv("ARIA_HOME", t.TempDir())
	cache, err := graphCachePath(root)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(cache), 0o755); err != nil {
		t.Fatal(err)
	}
	payload := map[string]any{
		"version": 1,
		"root":    root,
		"files": map[string]any{
			"app/api.py": map[string]any{
				"imports": []string{},
				"symbols": [][]any{{"handler", "def", 1, ""}},
			},
			"app/cli.py": map[string]any{
				"imports": []string{"app/api.py"},
				"symbols": [][]any{{"main", "def", 1, ""}},
			},
			"tests/test_api.py": map[string]any{
				"imports": []string{"app/api.py"},
				"symbols": [][]any{{"test_handler", "def", 1, ""}},
			},
		},
		"services": map[string]string{"api": "app"},
		"defs":     map[string][]string{"handler": {"app/api.py"}, "main": {"app/cli.py"}},
		"refs":     map[string][]string{"handler": {"app/cli.py", "tests/test_api.py"}},
	}
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cache, body, 0o644); err != nil {
		t.Fatal(err)
	}
	line := cachedImpactLine([]string{filepath.Join(root, "app", "api.py")})
	const want = "narrow · 2 files depend on it · 1 test · api"
	if line != want {
		t.Fatalf("impact = %q, want %q", line, want)
	}
	if cachedImpactLine([]string{filepath.Join(t.TempDir(), "missing.py")}) != "" {
		t.Fatal("a root without a graph cache should leave the impact line empty")
	}
}
