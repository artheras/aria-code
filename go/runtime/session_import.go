package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

// Imported IDs are never trusted as destinations. Validate the entire history
// before saving a new independent session; never overwrite the active session.
func decodeSessionImport(body []byte) (snapshot, error) {
	var imported snapshot
	body = bytes.TrimSpace(body)
	if len(body) == 0 || len(body) > maxState {
		return imported, errors.New("session import must contain 1 byte to 16 MiB of JSON")
	}
	if body[0] == '[' {
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
	imported.Metadata = metadata
	return imported, nil
}

func (s *server) importCommand(ctx context.Context, turn, path string) (string, error) {
	path = strings.TrimSpace(path)
	if len(path) >= 2 && ((path[0] == '"' && path[len(path)-1] == '"') || (path[0] == '\'' && path[len(path)-1] == '\'')) {
		path = path[1 : len(path)-1]
	}
	if path == "" {
		return "", errors.New("usage: /import <JSON export or session file>")
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
