package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"google.golang.org/genai"
)

func TestOllamaMissingSelectionDoesNotPersistAndRemovalBlocksInference(t *testing.T) {
	writeProviders(t, map[string]any{})
	names := []string{"tiny:latest"}
	calls := 0
	host := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/models" {
			rows := []map[string]string{}
			for _, name := range names {
				rows = append(rows, map[string]string{"id": name})
			}
			json.NewEncoder(w).Encode(map[string]any{"data": rows})
			return
		}
		calls++
		t.Errorf("unavailable model reached inference: %s", r.URL.Path)
	}))
	defer host.Close()
	s := approvalServer(t, config{"ollama_url": host.URL}, appOptions{})
	old := s.provider
	if _, err := s.command(context.Background(), "t", "/model ollama tiny:7b"); err == nil || !strings.Contains(err.Error(), "not installed") {
		t.Fatal(err)
	}
	if s.provider != old || s.cfg["model"] == "tiny:7b" {
		t.Fatal("failed selection mutated session")
	}
	disk, err := loadConfig(s.opts.root)
	if err != nil || disk["model"] == "tiny:7b" {
		t.Fatal(disk, err)
	}
	if _, err := s.command(context.Background(), "t", "/model ollama tiny"); err != nil {
		t.Fatal(err)
	}
	if s.cfg["model"] != "tiny:latest" {
		t.Fatal(s.cfg)
	}
	names = nil
	_, err = s.provider.Generate(context.Background(), []*genai.Content{genai.NewContentFromText("hello", genai.RoleUser)}, "s", nil, func(string) {})
	if err == nil || !strings.Contains(err.Error(), "not installed") || calls != 0 {
		t.Fatal(err, calls)
	}
}

func TestOllamaDiscoveryFailureNeverAllowsAnInference(t *testing.T) {
	for _, body := range []string{`{}`, `{"data":null}`, `{"data":[{"id":"other:latest"}]}`, `not-json`} {
		t.Run(body, func(t *testing.T) {
			host := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/v1/models" {
					t.Fatal("inference called without an installed model")
				}
				io.WriteString(w, body)
			}))
			defer host.Close()
			p, err := modelProviderFor(config{"local_provider": "ollama", "model": "chosen:1b", "ollama_base_url": host.URL})
			if err != nil {
				t.Fatal(err)
			}
			_, err = p.Generate(context.Background(), []*genai.Content{genai.NewContentFromText("hello", genai.RoleUser)}, "", nil, func(string) {})
			if err == nil {
				t.Fatal("invalid/absent model list accepted")
			}
		})
	}
}
