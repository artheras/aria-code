package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

type externalSessionInfo struct {
	Source    string    `json:"source"`
	Path      string    `json:"path"`
	Size      int64     `json:"size"`
	UpdatedAt time.Time `json:"updated_at"`
}

func extractExternalContentText(raw any) string {
	switch v := raw.(type) {
	case string:
		return strings.TrimSpace(v)
	case []any:
		var parts []string
		for _, item := range v {
			switch elem := item.(type) {
			case string:
				if t := strings.TrimSpace(elem); t != "" {
					parts = append(parts, t)
				}
			case map[string]any:
				if t, _ := elem["text"].(string); strings.TrimSpace(t) != "" {
					parts = append(parts, strings.TrimSpace(t))
				} else if t, _ := elem["input_text"].(string); strings.TrimSpace(t) != "" {
					parts = append(parts, strings.TrimSpace(t))
				} else if t, _ := elem["output_text"].(string); strings.TrimSpace(t) != "" {
					parts = append(parts, strings.TrimSpace(t))
				}
			}
		}
		return strings.Join(parts, "\n")
	}
	return ""
}

func decodeChatGPTConversationObject(conv map[string]any) (snapshot, bool) {
	mapping, ok := conv["mapping"].(map[string]any)
	if !ok || len(mapping) == 0 {
		return snapshot{}, false
	}

	var orderedIDs []string
	visited := map[string]bool{}
	cur, _ := conv["current_node"].(string)
	for cur != "" && !visited[cur] {
		visited[cur] = true
		orderedIDs = append(orderedIDs, cur)
		node, ok := mapping[cur].(map[string]any)
		if !ok {
			break
		}
		cur, _ = node["parent"].(string)
	}
	// Reverse to chronological order
	for i, j := 0, len(orderedIDs)-1; i < j; i, j = i+1, j-1 {
		orderedIDs[i], orderedIDs[j] = orderedIDs[j], orderedIDs[i]
	}
	if len(orderedIDs) == 0 {
		for k := range mapping {
			orderedIDs = append(orderedIDs, k)
		}
		sort.Strings(orderedIDs)
	}

	var msgs []message
	for _, id := range orderedIDs {
		node, ok := mapping[id].(map[string]any)
		if !ok {
			continue
		}
		msgObj, ok := node["message"].(map[string]any)
		if !ok || msgObj == nil {
			continue
		}
		author, _ := msgObj["author"].(map[string]any)
		role, _ := author["role"].(string)
		switch role {
		case "user", "assistant", "system":
		default:
			continue
		}
		contentObj, _ := msgObj["content"].(map[string]any)
		text := extractExternalContentText(contentObj["parts"])
		if text == "" {
			text = extractExternalContentText(contentObj["text"])
		}
		if text == "" {
			continue
		}
		msgs = append(msgs, message{"role": role, "content": text})
	}
	if len(msgs) == 0 {
		return snapshot{}, false
	}
	meta := map[string]any{"source": "chatgpt"}
	if title, _ := conv["title"].(string); strings.TrimSpace(title) != "" {
		meta["title"] = capText(strings.TrimSpace(title), 200)
	}
	return snapshot{Messages: msgs, Metadata: meta}, true
}

func tryDecodeChatGPTExport(body []byte) (snapshot, bool) {
	if body[0] == '{' {
		var single map[string]any
		if err := json.Unmarshal(body, &single); err == nil {
			if snap, ok := decodeChatGPTConversationObject(single); ok {
				return snap, true
			}
		}
		return snapshot{}, false
	}
	if body[0] == '[' {
		var list []map[string]any
		if err := json.Unmarshal(body, &list); err == nil {
			for _, conv := range list {
				if snap, ok := decodeChatGPTConversationObject(conv); ok {
					return snap, true
				}
			}
		}
	}
	return snapshot{}, false
}

func tryDecodeExternalJSONL(body []byte) (snapshot, bool) {
	scanner := bufio.NewScanner(bytes.NewReader(body))
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)

	var msgs []message
	detectedSource := ""
	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 || line[0] != '{' {
			continue
		}
		var row map[string]any
		if err := json.Unmarshal(line, &row); err != nil {
			continue
		}
		rowType, _ := row["type"].(string)
		// Claude Code JSONL format: {"type":"user"|"assistant", "message":{"role":...,"content":...}}
		if msgObj, ok := row["message"].(map[string]any); ok && (rowType == "user" || rowType == "assistant") {
			role, _ := msgObj["role"].(string)
			if role == "" {
				role = rowType
			}
			text := extractExternalContentText(msgObj["content"])
			if text != "" && (role == "user" || role == "assistant" || role == "system") {
				msgs = append(msgs, message{"role": role, "content": text})
				if detectedSource == "" {
					detectedSource = "claude_code"
				}
			}
			continue
		}
		// Codex JSONL format: {"type":"response_item", "payload":{"role":...,"content":...}}
		if rowType == "response_item" {
			if payload, ok := row["payload"].(map[string]any); ok {
				role, _ := payload["role"].(string)
				text := extractExternalContentText(payload["content"])
				if text != "" && (role == "user" || role == "assistant" || role == "system") {
					msgs = append(msgs, message{"role": role, "content": text})
					if detectedSource == "" {
						detectedSource = "codex"
					}
				}
			}
			continue
		}
	}
	if len(msgs) == 0 {
		return snapshot{}, false
	}
	return snapshot{
		Messages: msgs,
		Metadata: map[string]any{"source": detectedSource},
	}, true
}

func discoverExternalSessions(homeDir string, limit int) []externalSessionInfo {
	if strings.TrimSpace(homeDir) == "" {
		return nil
	}
	if limit <= 0 {
		limit = 20
	}
	roots := []struct {
		source string
		dir    string
	}{
		{"claude_code", filepath.Join(homeDir, ".claude", "projects")},
		{"codex", filepath.Join(homeDir, ".codex", "sessions")},
	}
	var found []externalSessionInfo
	for _, r := range roots {
		if _, err := os.Stat(r.dir); err != nil {
			continue
		}
		_ = filepath.WalkDir(r.dir, func(path string, d fs.DirEntry, err error) error {
			if err != nil || d == nil || d.IsDir() {
				return nil
			}
			if !strings.HasSuffix(strings.ToLower(d.Name()), ".jsonl") {
				return nil
			}
			info, err := d.Info()
			if err != nil || info.Size() == 0 {
				return nil
			}
			found = append(found, externalSessionInfo{
				Source:    r.source,
				Path:      path,
				Size:      info.Size(),
				UpdatedAt: info.ModTime(),
			})
			return nil
		})
	}
	sort.Slice(found, func(i, j int) bool {
		return found[i].UpdatedAt.After(found[j].UpdatedAt)
	})
	if len(found) > limit {
		found = found[:limit]
	}
	return found
}

// Imported IDs are never trusted as destinations. Validate the entire history
// before saving a new independent session; never overwrite the active session.
func decodeSessionImport(body []byte) (snapshot, error) {
	var imported snapshot
	body = bytes.TrimSpace(body)
	if len(body) == 0 || len(body) > maxState {
		return imported, errors.New("session import must contain 1 byte to 16 MiB of JSON")
	}
	externalSource := ""
	if ext, ok := tryDecodeChatGPTExport(body); ok {
		imported = ext
		externalSource, _ = ext.Metadata["source"].(string)
	} else if ext, ok := tryDecodeExternalJSONL(body); ok {
		imported = ext
		externalSource, _ = ext.Metadata["source"].(string)
	} else if body[0] == '[' {
		if err := json.Unmarshal(body, &imported.Messages); err != nil {
			return imported, err
		}
	} else if err := json.Unmarshal(body, &imported); err != nil {
		return imported, err
	}
	if imported.Messages == nil || len(imported.Messages) > 10000 {
		return imported, errors.New("expected a messages array with at most 10000 messages")
	}
	for _, m := range imported.Messages {
		switch m["role"] {
		case "user", "assistant", "system", "tool", "function":
		default:
			return imported, errors.New("invalid imported message role")
		}
	}
	// Validate both visible history and native history, not just the preferred
	// one. Unknown fields remain in messages for forward compatibility.
	if _, err := legacyContents(imported.Messages); err != nil {
		return imported, err
	}
	if bytes.Equal(bytes.TrimSpace(imported.ProviderContents), []byte("null")) {
		imported.ProviderContents = nil
	}
	contents, err := sessionContents(imported)
	if err != nil {
		return imported, err
	}
	imported.ProviderContents, err = json.Marshal(contents)
	if err != nil {
		return imported, err
	}
	imported.ID, err = newID()
	if err != nil {
		return imported, err
	}
	if imported.Metadata == nil {
		imported.Metadata = map[string]any{}
	}
	// Checkpoints belong to the source session and must not become local
	// write authority just because a JSON file contains their identifiers.
	metadata := map[string]any{"imported": true}
	if title, ok := imported.Metadata["title"].(string); ok {
		metadata["title"] = capText(title, 200)
	}
	if externalSource != "" {
		metadata["source"] = externalSource
	}
	imported.Metadata = metadata
	return imported, nil
}

func (s *server) importCommand(ctx context.Context, turn, path string) (string, error) {
	path = strings.TrimSpace(path)
	if len(path) >= 2 && ((path[0] == '"' && path[len(path)-1] == '"') || (path[0] == '\'' && path[len(path)-1] == '\'')) {
		path = path[1 : len(path)-1]
	}
	if path == "" {
		return "", errors.New("usage: /import <JSON/JSONL export or --discover>")
	}
	if path == "--discover" || path == "discover" {
		home, _ := os.UserHomeDir()
		sessions := discoverExternalSessions(home, 15)
		if len(sessions) == 0 {
			return "No local Claude Code (~/.claude/projects) or Codex (~/.codex/sessions) sessions found.", nil
		}
		var b strings.Builder
		b.WriteString("Discovered local sessions (run `/import <path>` to import):\n")
		for _, item := range sessions {
			fmt.Fprintf(&b, "- [%s] %s (%s, %d bytes)\n", item.Source, item.Path, item.UpdatedAt.Format("2006-01-02 15:04"), item.Size)
		}
		return strings.TrimRight(b.String(), "\n"), nil
	}
	params := map[string]any{"path": path}
	if contract := projectContract(s.scope.workspace, "Import session"); contract != nil {
		v := contract.Check("read_file", params)
		if !v.Allowed {
			return "", errors.New(v.toolResult("read_file").Error)
		}
	}
	ok, reason, err := s.approve(ctx, "read_file", params, fileChange{})
	if err != nil {
		return "", err
	}
	if !ok {
		return "", fmt.Errorf("session import refused: %s", reason)
	}
	p, err := s.scope.open(path, false)
	if err != nil {
		return "", err
	}
	defer p.Close()
	body, err := readRegular(p, maxState)
	if err != nil {
		return "", err
	}
	imported, err := decodeSessionImport(body)
	if err != nil {
		return "", fmt.Errorf("session import rejected: %w", err)
	}
	if err := ctx.Err(); err != nil {
		return "", err
	}
	if err := saveSession(s.opts.root, imported); err != nil {
		return "", err
	}
	return fmt.Sprintf("Imported %d messages as a new session. Current conversation unchanged.\n/resume %s", len(imported.Messages), imported.ID), nil
}
