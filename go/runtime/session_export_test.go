package main

import (
	"context"
	"encoding/csv"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func exportConversation() []message {
	return []message{{"role": "user", "content": "请分析这家公司的库存和成本情况，给我具体的建议。"}, {"role": "assistant", "content": "建议先计算库存周转，再核对运输和仓储成本。这些结论需要根据实际数据逐项验证。"}}
}

func TestSessionExportFormats(t *testing.T) {
	now := time.Date(2026, 10, 14, 12, 30, 0, 0, time.UTC)
	for _, format := range []string{"json", "csv", "md", "sft", "ipynb"} {
		t.Run(format, func(t *testing.T) {
			body, ext, _, err := sessionExport(format, exportConversation(), now)
			if err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(body, "库存") {
				t.Fatal("Chinese content lost")
			}
			if format == "json" || format == "sft" || format == "ipynb" {
				if !json.Valid([]byte(body)) {
					t.Fatal("invalid JSON")
				}
			}
			if format == "sft" && ext != "json" {
				t.Fatal(ext)
			}
			if format == "csv" {
				records, err := csv.NewReader(strings.NewReader(body)).ReadAll()
				if err != nil || len(records) != 3 {
					t.Fatal(records, err)
				}
			}
		})
	}
}

func TestSessionExportRetainsAttachmentsAndUnknownFields(t *testing.T) {
	conv := []message{{"role": "user", "content": []any{map[string]any{"type": "text", "text": "look at this"}, map[string]any{"type": "image_url", "image_url": map[string]any{"url": "data:image/png;base64,PRIVATEIMAGE"}}}, "attachments": []any{map[string]any{"id": "persisted-file-id"}}, "future_field": "retained"}}
	body, _, _, err := sessionExport("json", conv, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	var restored []message
	if err := json.Unmarshal([]byte(body), &restored); err != nil {
		t.Fatal(err)
	}
	if restored[0]["future_field"] != "retained" || restored[0]["attachments"] == nil || !strings.Contains(body, "PRIVATEIMAGE") {
		t.Fatal("lossy JSON")
	}
	for _, format := range []string{"md", "csv", "ipynb"} {
		body, _, _, err := sessionExport(format, conv, time.Now())
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(body, "PRIVATEIMAGE") || !strings.Contains(body, "preserved in JSON") {
			t.Fatal("attachment incorrectly rendered")
		}
	}
	conv = append(conv, message{"role": "assistant", "content": strings.Repeat("long answer ", 5)})
	if _, _, _, err := sessionExport("sft", conv, time.Now()); err == nil {
		t.Fatal("attachment-dependent SFT pair exported")
	}
}

func TestSessionExportSFTExcludesShortAndToolPairs(t *testing.T) {
	for _, text := range []string{"短", "Tool results: a long hidden tool result"} {
		conv := []message{{"role": "user", "content": text}, {"role": "assistant", "content": strings.Repeat("answer ", 8)}}
		if _, _, _, err := sessionExport("sft", conv, time.Now()); err == nil {
			t.Fatal("invalid training pair")
		}
	}
	if _, _, _, err := sessionExport("bundle", exportConversation(), time.Now()); err == nil {
		t.Fatal("unsupported bundle presented as supported")
	}
}

func TestExportCommandActuallyWritesAndResumes(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{allowedTools: []string{"write_file"}})
	s.snap.Messages = exportConversation()
	before, _ := json.Marshal(s.snap.Messages)
	answer, err := s.command(context.Background(), "export-turn", "/export json session export.json")
	if err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(s.scope.workspace, "session export.json")
	if !strings.Contains(answer, file) {
		t.Fatal(answer)
	}
	data, err := os.ReadFile(file)
	if err != nil {
		t.Fatal(err)
	}
	var actual []message
	if err := json.Unmarshal(data, &actual); err != nil {
		t.Fatal(err)
	}
	after, _ := json.Marshal(actual)
	if string(before) != string(after) {
		t.Fatal("export changed conversation")
	}
	if runtime.GOOS != "windows" {
		info, _ := os.Stat(file)
		if info.Mode().Perm() != 0o600 {
			t.Fatal("export not private")
		}
	}
	if err := s.persist(); err != nil {
		t.Fatal(err)
	}
	restored, err := loadSession(s.opts.root, s.snap.ID)
	if err != nil {
		t.Fatal(err)
	}
	after, _ = json.Marshal(restored.Messages)
	if string(before) != string(after) {
		t.Fatal("export command modified saved history")
	}
}

func TestExportCommandRefusesWrites(t *testing.T) {
	for _, tc := range []struct {
		name  string
		cfg   config
		grant bool
	}{{"unapproved", config{}, false}, {"readonly", config{"permission_mode": "read-only"}, true}} {
		t.Run(tc.name, func(t *testing.T) {
			opts := appOptions{}
			if tc.grant {
				opts.allowedTools = []string{"write_file"}
			}
			s := approvalServer(t, tc.cfg, opts)
			s.snap.Messages = exportConversation()
			if _, err := s.command(context.Background(), "turn", "/export json forbidden.json"); err == nil {
				t.Fatal("unapproved write")
			}
			if _, err := os.Stat(filepath.Join(s.scope.workspace, "forbidden.json")); !os.IsNotExist(err) {
				t.Fatal("file created")
			}
		})
	}
	s := approvalServer(t, config{}, appOptions{allowedTools: []string{"write_file"}})
	s.snap.Messages = exportConversation()
	outside := filepath.Join(t.TempDir(), "outside.json")
	// Local sessions also grant the OS temporary directory. Restrict this
	// fixture to its workspace so the target is genuinely outside its roots.
	s.scope.writeRoots = []string{s.scope.workspace}
	if _, err := s.exportCommand(context.Background(), "turn", []string{"json", outside}); err == nil {
		t.Fatal("outside-root export")
	}
	if _, err := os.Stat(outside); !os.IsNotExist(err) {
		t.Fatal("out-of-scope file created")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := s.exportCommand(ctx, "turn", []string{"json", "cancelled.json"}); err == nil {
		t.Fatal("cancelled export succeeded")
	}
}

func TestExportCommandHonorsPersistentAndProjectDenials(t *testing.T) {
	for _, policy := range []string{"persistent", "project"} {
		t.Run(policy, func(t *testing.T) {
			s := approvalServer(t, config{}, appOptions{allowedTools: []string{"write_file"}})
			s.snap.Messages = exportConversation()
			if policy == "persistent" {
				if err := atomicJSON(filepath.Join(s.opts.root, "tool_policy.json"), map[string]any{"denied": []string{"write_file"}}); err != nil {
					t.Fatal(err)
				}
			} else {
				writePolicy(t, s.scope.workspace, "contract:\n  allow: [src/**]\n")
			}
			if _, err := s.exportCommand(context.Background(), "turn", []string{"json", "denied.json"}); err == nil {
				t.Fatal("policy bypassed by standing grant")
			}
			if _, err := os.Stat(filepath.Join(s.scope.workspace, "denied.json")); !os.IsNotExist(err) {
				t.Fatal("denied file created")
			}
		})
	}
}
