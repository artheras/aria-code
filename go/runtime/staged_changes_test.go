package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/genai"
)

func TestStageOnlySurvivesProcessesAndNeverWritesBeforeApply(t *testing.T) {
	root, project := t.TempDir(), t.TempDir()
	args := map[string]any{"path": "new.txt", "content": "Reviewed complete file body.\r\n", "stage_only": true}
	staged := bridgeTool(context.Background(), project, root, "write_file", "write_file", args)
	if !staged.Success || staged.Data["staged"] != true {
		t.Fatal(staged)
	}
	id := staged.Data["change_id"].(string)
	if _, err := os.Stat(filepath.Join(project, "new.txt")); !os.IsNotExist(err) {
		t.Fatal("staging wrote the file", err)
	}
	listed := bridgeTool(context.Background(), project, root, "", "list_changes", map[string]any{})
	if !listed.Success || listed.Data["count"] != 1 {
		t.Fatal(listed)
	}
	if result := bridgeTool(context.Background(), project, root, "write_file", "apply_change", map[string]any{"change_id": id}); result.Success {
		t.Fatal("grant for another invocation applied a stage")
	}
	applied := bridgeTool(context.Background(), project, root, "apply_change", "apply_change", map[string]any{"change_id": id[:12]})
	if !applied.Success || applied.Data["checkpoint_id"] == nil {
		t.Fatal(applied)
	}
	body, _ := os.ReadFile(filepath.Join(project, "new.txt"))
	if string(body) != args["content"] {
		t.Fatal(string(body))
	}
	if again := bridgeTool(context.Background(), project, root, "apply_change", "apply_change", map[string]any{"change_id": id}); again.Success {
		t.Fatal("stage applied twice")
	}
	if listed := bridgeTool(context.Background(), project, root, "", "list_changes", map[string]any{}); listed.Data["count"] != 0 {
		t.Fatal(listed)
	}
}
func TestStageRejectConflictAndPersistentDenyProtectTheFile(t *testing.T) {
	for _, mode := range []string{"rejected", "conflict", "deny", "outside"} {
		t.Run(mode, func(t *testing.T) {
			root, project := t.TempDir(), t.TempDir()
			path := filepath.Join(project, "note.txt")
			os.WriteFile(path, []byte("before"), 0644)
			staged := bridgeTool(context.Background(), project, root, "edit_file", "edit_file", map[string]any{"path": "note.txt", "old_string": "before", "new_string": "after", "stage_only": true})
			if !staged.Success {
				t.Fatal(staged)
			}
			id := staged.Data["change_id"].(string)
			workspace := project
			want := "before"
			switch mode {
			case "rejected":
				if result := bridgeTool(context.Background(), project, root, "", "reject_change", map[string]any{"change_id": id}); !result.Success {
					t.Fatal(result)
				}
			case "conflict":
				want = "user modified it"
				os.WriteFile(path, []byte(want), 0644)
			case "deny":
				atomicJSON(filepath.Join(root, "tool_policy.json"), map[string]any{"denied": []string{"edit_file"}})
			case "outside":
				workspace = t.TempDir()
			}
			if result := bridgeTool(context.Background(), workspace, root, "apply_change", "apply_change", map[string]any{"change_id": id}); result.Success {
				t.Fatal(result)
			}
			body, _ := os.ReadFile(path)
			if string(body) != want {
				t.Fatal(string(body))
			}
		})
	}
}
func TestMalformedStageFlagCannotFallThroughToWrite(t *testing.T) {
	root, project := t.TempDir(), t.TempDir()
	for _, flag := range []any{"true", 1, nil} {
		if result := bridgeTool(context.Background(), project, root, "write_file", "write_file", map[string]any{"path": "new.txt", "content": "new body", "stage_only": flag}); result.Success {
			t.Fatal("invalid flag wrote", flag)
		}
	}
	if _, err := os.Stat(filepath.Join(project, "new.txt")); !os.IsNotExist(err) {
		t.Fatal(err)
	}
}
func TestAppStagedApplyAndRejectCommandsUseOriginalWriteApproval(t *testing.T) {
	p := providerFunc(func(ctx context.Context, h []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
		if h[len(h)-1].Parts[0].FunctionResponse != nil {
			emit("Prepared for review.")
			return genai.NewContentFromText("Prepared for review.", genai.RoleModel), nil
		}
		return &genai.Content{Role: "model", Parts: []*genai.Part{call("write_file", map[string]any{"path": "new.txt", "content": "Actual reviewed body.", "stage_only": true})}}, nil
	})
	c, root, project := testApplication(t, p)
	ready := c.until(t, "session.ready")
	session := ready["session_id"].(string)
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "stage", "text": "Stage a complete new file for my review."})
	menu := c.until(t, "approval.requested")
	c.send(t, map[string]any{"type": "dialog.respond", "turn_id": "stage", "request_id": menu["request_id"], "choice": 0})
	operation := c.until(t, "tool.completed")
	if operation["staged"] != true || !strings.Contains(operation["output"].(string), "not been modified") {
		t.Fatal("staging was rendered as a disk write", operation)
	}
	c.until(t, "turn.completed")
	c.until(t, "session.state")
	if _, err := os.Stat(filepath.Join(project, "new.txt")); !os.IsNotExist(err) {
		t.Fatal(err)
	}
	scope, err := newScope(project, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	listed := stagedList(context.Background(), scope, root, session, false)
	if !listed.Success || listed.Data["count"] != 1 {
		t.Fatal(listed)
	}
	id := listed.Data["items"].([]map[string]any)[0]["change_id"].(string)
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "apply", "text": "/apply-change " + id})
	menu = c.until(t, "approval.requested")
	if !strings.Contains(menu["context"].(string), "Actual reviewed body.") || !strings.Contains(menu["context"].(string), "write_file") {
		t.Fatal(menu)
	}
	c.send(t, map[string]any{"type": "dialog.respond", "turn_id": "apply", "request_id": menu["request_id"], "choice": 0})
	if e := c.until(t, "turn.completed"); e["status"] != "ok" {
		t.Fatal(e)
	}
	c.until(t, "session.state")
	body, _ := os.ReadFile(filepath.Join(project, "new.txt"))
	if string(body) != "Actual reviewed body." {
		t.Fatal(string(body))
	}
	c.close(t)
}
