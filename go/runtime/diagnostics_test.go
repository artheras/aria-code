package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestDiagnosticBundleDoesNotSerializeUserValues(t *testing.T) {
	secret := "PRIVATE_CANARY_TOKEN_PATH"
	body, err := nativeDiagnosticBundle([]message{{"role": secret, "content": secret, "attachments": []any{secret}, "api_key": secret}}, time.Now())
	if err != nil || !json.Valid([]byte(body)) || strings.Contains(body, secret) {
		t.Fatalf("invalid or leaking diagnostic: %v", err)
	}
	var bundle map[string]any
	_ = json.Unmarshal([]byte(body), &bundle)
	if bundle["schema"] != "aria.native_diagnostic_bundle.v1" {
		t.Fatal("missing schema")
	}
	if bundle["conversation_summary"].(map[string]any)["roles"].(map[string]any)["other"] != float64(1) {
		t.Fatal("unknown roles must be counted without echoing their values")
	}
}

func TestDiagnosticExportEmptySessionAndDenial(t *testing.T) {
	for _, allowed := range []bool{true, false} {
		opts := appOptions{}
		if allowed {
			opts.allowedTools = []string{"write_file"}
		}
		s := approvalServer(t, config{}, opts)
		_, err := s.command(context.Background(), "diagnostics", "/export BUNDLE diagnostic report.json")
		body, readErr := os.ReadFile(filepath.Join(s.scope.workspace, "diagnostic report.json"))
		if allowed {
			if err != nil || readErr != nil || !json.Valid(body) {
				t.Fatalf("export failed: %v %v", err, readErr)
			}
		} else if err == nil || !os.IsNotExist(readErr) {
			t.Fatal("unapproved bundle written")
		}
		if len(s.snap.Messages) != 0 {
			t.Fatal("diagnostics changed conversation")
		}
	}
}
