package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAccountPartitioningAndMemorySync(t *testing.T) {
	root := t.TempDir()
	workspace := t.TempDir()

	// 1. Unauthenticated / anonymous root
	cfgAnon := config{}
	if id := accountID(cfgAnon); id != "" {
		t.Fatalf("expected empty accountID for unauthenticated config, got %s", id)
	}
	anonRoot := accountRoot(root, cfgAnon)
	if !strings.HasSuffix(anonRoot, filepath.Join("accounts", "anonymous")) {
		t.Fatalf("expected anonymous account root, got %s", anonRoot)
	}

	// 2. Add local memory for anonymous
	if err := addAccountMemory(root, cfgAnon, "User prefers Go over Python"); err != nil {
		t.Fatal(err)
	}
	memState, _ := loadAccountMemory(root, cfgAnon)
	if len(memState.Items) != 1 || len(memState.Outbox) != 1 {
		t.Fatalf("expected 1 item and 1 outbox, got %+v", memState)
	}
	if memState.Items[0].Content != "User prefers Go over Python" {
		t.Fatalf("unexpected content: %s", memState.Items[0].Content)
	}

	// Check that projectInstructions includes account_memory
	instr := projectInstructions(root, workspace)
	if !strings.Contains(instr, "User prefers Go over Python") {
		t.Fatalf("expected account_memory to be injected into instructions: %s", instr)
	}

	// 3. Authenticated account test with fake Firebase JWT
	claims := tokenClaims("eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJhcnRoZXJhLTY2ZTViIiwiaXNzIjoiaHR0cHM6Ly9zZWN1cmV0b2tlbi5nb29nbGUuY29tL2FydGhlcmEtNjZlNWIiLCJzdWIiOiJ1c2VyXzEyMyJ9.sig")
	if claims == nil || claims["sub"] != "user_123" {
		t.Fatalf("tokenClaims verification failed: %+v", claims)
	}

	validToken := "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJhcnRoZXJhLTY2ZTViIiwiaXNzIjoiaHR0cHM6Ly9zZWN1cmV0b2tlbi5nb29nbGUuY29tL2FydGhlcmEtNjZlNWIiLCJzdWIiOiJ1c2VyXzEyMyJ9.sig"
	cfgAuth := config{"auth_token": validToken, "user_id": "user_123"}
	if id := accountID(cfgAuth); id != "user_123" {
		t.Fatalf("expected accountID user_123, got %s", id)
	}
	userRoot := accountRoot(root, cfgAuth)
	if strings.HasSuffix(userRoot, "anonymous") {
		t.Fatalf("expected hashed user root, got %s", userRoot)
	}

	// 4. Test Cloud Memory sync with mock HTTP server
	var pushedItems []map[string]any
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+validToken {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		if r.Method == http.MethodPost && r.URL.Path == "/api/v2/memory/items" {
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			pushedItems = append(pushedItems, body)
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"id": "cloud_mem_1"})
			return
		}
		if r.Method == http.MethodGet && r.URL.Path == "/api/v2/memory/items" {
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"items": []map[string]any{
					{"id": "cloud_mem_remote", "content": "Always use strict typing", "kind": "durable", "scope": "user"},
				},
			})
			return
		}
		w.WriteHeader(http.StatusNotFound)
	})
	memorySyncClient = &http.Client{
		Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)
			return rec.Result(), nil
		}),
	}
	defer func() { memorySyncClient = nil }()

	cfgAuth["api_url"] = "https://api.arthera.xyz"
	if err := addAccountMemory(root, cfgAuth, "User prefers dark mode"); err != nil {
		t.Fatal(err)
	}

	pushed, pulled, err := syncAccountMemory(context.Background(), root, cfgAuth)
	if err != nil {
		t.Fatal(err)
	}
	if pushed != 1 || pulled != 1 {
		t.Fatalf("expected pushed=1, pulled=1, got pushed=%d, pulled=%d", pushed, pulled)
	}
	if len(pushedItems) != 1 || pushedItems[0]["content"] != "User prefers dark mode" {
		t.Fatalf("unexpected pushed items: %+v", pushedItems)
	}

	// Verify state after sync: outbox should be drained, and remote item added
	syncedState, _ := loadAccountMemory(root, cfgAuth)
	if len(syncedState.Outbox) != 0 {
		t.Fatalf("expected outbox to be empty after sync, got %d", len(syncedState.Outbox))
	}
	if len(syncedState.Items) != 2 {
		t.Fatalf("expected 2 items after sync, got %d", len(syncedState.Items))
	}

	// 5. Verify /memory profile account command
	s, err := newServer(appOptions{root: root, workspace: workspace}, os.Stdout)
	if err != nil {
		t.Fatal(err)
	}
	s.cfg = cfgAuth
	out, err := s.command(context.Background(), "t", "/memory profile account")
	if err != nil || !strings.Contains(out, "Always use strict typing") {
		t.Fatalf("unexpected /memory output: %s, %v", out, err)
	}
}
