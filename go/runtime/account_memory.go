package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// accountID returns verified uid from config if auth_token sub matches user_id.
func accountID(cfg config) string {
	token := cfg.text("auth_token", "")
	uid := cfg.text("user_id", "")
	if token == "" || uid == "" {
		return ""
	}
	claims := tokenClaims(token)
	if claims == nil || claims["sub"] != uid {
		return ""
	}
	return uid
}

// accountRoot returns <stateRoot>/accounts/<sha256(uid)>/ (or anonymous if unauthenticated).
func accountRoot(root string, cfg config) string {
	uid := accountID(cfg)
	bucket := "anonymous"
	if uid != "" {
		hash := sha256.Sum256([]byte(uid))
		bucket = hex.EncodeToString(hash[:])
	}
	return filepath.Join(root, "accounts", bucket)
}

type accountMemoryItem struct {
	ID        string         `json:"id"`
	Content   string         `json:"content"`
	Kind      string         `json:"kind"`
	Scope     string         `json:"scope"`
	CreatedAt string         `json:"created_at"`
	Metadata  map[string]any `json:"metadata,omitempty"`
}

type accountMemoryState struct {
	Items     []accountMemoryItem `json:"items"`
	Outbox    []accountMemoryItem `json:"outbox"`
	UpdatedAt float64             `json:"updated_at"`
	SyncedAt  float64             `json:"synced_at"`
}

var (
	memorySyncMu     sync.Mutex
	memorySyncClient *http.Client
)

func getMemorySyncClient() *http.Client {
	if memorySyncClient != nil {
		return memorySyncClient
	}
	return &http.Client{
		Timeout: 10 * time.Second,
		Transport: &http.Transport{
			Proxy: nil,
		},
	}
}

func loadAccountMemory(root string, cfg config) (accountMemoryState, string) {
	accRoot := accountRoot(root, cfg)
	filePath := filepath.Join(accRoot, "memory.json")
	var state accountMemoryState
	data, err := os.ReadFile(filePath)
	if err == nil {
		_ = json.Unmarshal(data, &state)
	}
	return state, filePath
}

func saveAccountMemory(filePath string, state accountMemoryState) error {
	_ = os.MkdirAll(filepath.Dir(filePath), 0o755)
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	return atomicWrite(filePath, data)
}

// syncAccountMemory pushes outbox items to Cloud Memory API and pulls latest remote items.
func syncAccountMemory(ctx context.Context, root string, cfg config) (int, int, error) {
	memorySyncMu.Lock()
	defer memorySyncMu.Unlock()

	uid := accountID(cfg)
	token := cfg.text("auth_token", "")
	if uid == "" || token == "" {
		return 0, 0, nil // Anonymous / unauthenticated: local only
	}

	apiURL := currentArtheraURL(cfg.text("api_url", artheraAPIURL), false)
	state, filePath := loadAccountMemory(root, cfg)

	client := getMemorySyncClient()
	pushed := 0

	// 1. Flush outbox
	remainingOutbox := make([]accountMemoryItem, 0, len(state.Outbox))
	for _, item := range state.Outbox {
		payload := map[string]any{
			"content":  item.Content,
			"kind":     item.Kind,
			"scope":    item.Scope,
			"metadata": item.Metadata,
		}
		body, _ := json.Marshal(payload)
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, apiURL+"/api/v2/memory/items", bytes.NewReader(body))
		if err != nil {
			remainingOutbox = append(remainingOutbox, item)
			continue
		}
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("Content-Type", "application/json")

		resp, err := client.Do(req)
		if err == nil && (resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusCreated) {
			resp.Body.Close()
			pushed++
		} else {
			if resp != nil {
				resp.Body.Close()
			}
			remainingOutbox = append(remainingOutbox, item)
		}
	}
	state.Outbox = remainingOutbox

	// 2. Fetch remote items
	pulled := 0
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, apiURL+"/api/v2/memory/items?limit=50", nil)
	if err == nil {
		req.Header.Set("Authorization", "Bearer "+token)
		resp, err := client.Do(req)
		if err == nil && resp.StatusCode == http.StatusOK {
			defer resp.Body.Close()
			var result struct {
				Items []accountMemoryItem `json:"items"`
			}
			if json.NewDecoder(resp.Body).Decode(&result) == nil {
				// Merge remote items
				known := map[string]bool{}
				for _, it := range state.Items {
					if it.ID != "" {
						known[it.ID] = true
					}
					known[it.Content] = true
				}
				for _, rem := range result.Items {
					if !known[rem.ID] && !known[rem.Content] {
						state.Items = append(state.Items, rem)
						known[rem.ID] = true
						known[rem.Content] = true
						pulled++
					}
				}
			}
		} else if resp != nil {
			resp.Body.Close()
		}
	}

	state.SyncedAt = float64(time.Now().UnixNano()) / 1e9
	state.UpdatedAt = float64(time.Now().UnixNano()) / 1e9
	_ = saveAccountMemory(filePath, state)

	return pushed, pulled, nil
}

// addAccountMemory queues a fact into the local account memory and outbox.
func addAccountMemory(root string, cfg config, content string) error {
	content = strings.TrimSpace(content)
	if content == "" {
		return nil
	}
	state, filePath := loadAccountMemory(root, cfg)
	now := time.Now().UTC().Format(time.RFC3339)
	item := accountMemoryItem{
		ID:        fmt.Sprintf("mem_%d", time.Now().UnixNano()),
		Content:   content,
		Kind:      "durable",
		Scope:     "user",
		CreatedAt: now,
		Metadata:  map[string]any{"source": "aria-runtime"},
	}
	state.Items = append(state.Items, item)
	state.Outbox = append(state.Outbox, item)
	state.UpdatedAt = float64(time.Now().UnixNano()) / 1e9
	return saveAccountMemory(filePath, state)
}
