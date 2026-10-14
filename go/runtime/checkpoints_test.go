package main

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"

	"google.golang.org/genai"
)

func checkpointFixture(t *testing.T) (*scope, string, *checkpointStore) {
	t.Helper()
	root, project := t.TempDir(), t.TempDir()
	scope, err := newScope(project, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	store, err := openCheckpoints(root)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(store.Close)
	return scope, root, store
}
func recordedWrite(t *testing.T, scope *scope, root, session, path, body string) string {
	t.Helper()
	c, err := propose(scope, "write_file", map[string]any{"path": path, "content": body})
	if err != nil {
		t.Fatal(err)
	}
	result := applyRecordedChange(context.Background(), scope, root, session, "write_file", c)
	if !result.Success {
		t.Fatal(result)
	}
	id, ok := result.Data["checkpoint_id"].(string)
	if !ok {
		t.Fatal("actual write had no checkpoint", result)
	}
	return id
}
func TestCheckpointSurvivesRestartRestoresExactBytesAndCreation(t *testing.T) {
	scope, root, store := checkpointFixture(t)
	path := filepath.Join(scope.workspace, "source.txt")
	before := []byte("你好\r\nsecond line\r\n")
	if err := os.WriteFile(path, before, 0751); err != nil {
		t.Fatal(err)
	}
	id := recordedWrite(t, scope, root, "session", "source.txt", "new body\r\n")
	store.Close()
	reopened, err := openCheckpoints(root)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	records, err := reopened.selectRecords(context.Background(), "", id[:12])
	if err != nil || len(records) != 1 {
		t.Fatal(records, err)
	}
	paths, err := reopened.restore(context.Background(), scope, records)
	if err != nil || len(paths) != 1 {
		t.Fatal(paths, err)
	}
	body, _ := os.ReadFile(path)
	if string(body) != string(before) {
		t.Fatal("physical preimage changed", string(body))
	}
	info, _ := os.Stat(path)
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0751 {
		t.Fatal("mode changed", info.Mode())
	}
	if _, err := reopened.selectRecords(context.Background(), "", id); err == nil {
		t.Fatal("restored checkpoint reopened")
	}
	created := recordedWrite(t, scope, root, "session", "new.txt", "")
	records, err = reopened.selectRecords(context.Background(), "", created)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reopened.restore(context.Background(), scope, records); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(scope.workspace, "new.txt")); !os.IsNotExist(err) {
		t.Fatal("new file was not removed", err)
	}
}
func TestCheckpointConflictsPreflightAllFilesAndPreserveUserChanges(t *testing.T) {
	scope, root, store := checkpointFixture(t)
	first := recordedWrite(t, scope, root, "session", "a.txt", "first image")
	second := recordedWrite(t, scope, root, "session", "b.txt", "second image")
	r1, _ := store.selectRecords(context.Background(), "", first)
	r2, _ := store.selectRecords(context.Background(), "", second)
	if err := os.WriteFile(filepath.Join(scope.workspace, "a.txt"), []byte("user edited it"), 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := store.restore(context.Background(), scope, append(r2, r1...)); err == nil {
		t.Fatal("conflict was ignored")
	}
	for path, want := range map[string]string{"a.txt": "user edited it", "b.txt": "second image"} {
		body, _ := os.ReadFile(filepath.Join(scope.workspace, path))
		if string(body) != want {
			t.Fatal("preflight touched a file", path, string(body))
		}
	}
	active, err := store.selectRecords(context.Background(), "", second)
	if err != nil || active[0].Status != "active" {
		t.Fatal(active, err)
	}
}
func TestCheckpointRestoreOrdersRepeatedWritesNewestFirst(t *testing.T) {
	scope, root, store := checkpointFixture(t)
	path := filepath.Join(scope.workspace, "note.txt")
	os.WriteFile(path, []byte("original"), 0644)
	one := recordedWrite(t, scope, root, "session", "note.txt", "one")
	two := recordedWrite(t, scope, root, "session", "note.txt", "two")
	r1, _ := store.selectRecords(context.Background(), "", one)
	r2, _ := store.selectRecords(context.Background(), "", two)
	if _, err := store.restore(context.Background(), scope, append(r1, r2...)); err == nil {
		t.Fatal("out-of-order restore accepted")
	}
	paths, err := store.restore(context.Background(), scope, append(r2, r1...))
	if err != nil || len(paths) != 1 {
		t.Fatal(paths, err)
	}
	body, _ := os.ReadFile(path)
	if string(body) != "original" {
		t.Fatal(string(body))
	}
}
func TestCheckpointScopeAndRedirectedSymlinksCannotWrite(t *testing.T) {
	scope, root, store := checkpointFixture(t)
	id := recordedWrite(t, scope, root, "session", "note.txt", "after")
	records, _ := store.selectRecords(context.Background(), "", id)
	other, err := newScope(t.TempDir(), nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.restore(context.Background(), other, records); err == nil {
		t.Fatal("escaped restore scope")
	}
	path := filepath.Join(scope.workspace, "note.txt")
	target := filepath.Join(scope.workspace, "other.txt")
	os.WriteFile(target, []byte("after"), 0644)
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, path); err != nil {
		t.Skip("symlinks unavailable", err)
	}
	if _, err := store.restore(context.Background(), scope, records); err == nil {
		t.Fatal("redirected checkpoint wrote a different file")
	}
	body, _ := os.ReadFile(target)
	if string(body) != "after" {
		t.Fatal(string(body))
	}
}
func TestCheckpointStorageFailureAndCancellationPreventWrites(t *testing.T) {
	scope, _, _ := checkpointFixture(t)
	root := t.TempDir()
	os.WriteFile(filepath.Join(root, "runtime"), []byte("not a directory"), 0644)
	c, err := propose(scope, "write_file", map[string]any{"path": "note.txt", "content": "complete new file"})
	if err != nil {
		t.Fatal(err)
	}
	if result := applyRecordedChange(context.Background(), scope, root, "session", "write_file", c); result.Success {
		t.Fatal(result)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if result := applyRecordedChange(ctx, scope, t.TempDir(), "session", "write_file", c); result.Success {
		t.Fatal(result)
	}
	if _, err := os.Stat(c.path); !os.IsNotExist(err) {
		t.Fatal("failed checkpoint still wrote", err)
	}
}

func TestRestoreDeletionRejectsRedirectAfterPreflight(t *testing.T) {
	scope, root, store := checkpointFixture(t)
	id := recordedWrite(t, scope, root, "session", "new.txt", "postimage")
	records, err := store.selectRecords(context.Background(), "", id)
	if err != nil {
		t.Fatal(err)
	}
	steps, err := planRestore(context.Background(), scope, records)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(scope.workspace, "new.txt")
	other := filepath.Join(scope.workspace, "user-file.txt")
	if err := os.WriteFile(other, []byte("postimage"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(other, path); err != nil {
		t.Skip("symlinks unavailable", err)
	}
	if result := applyRestoreStep(scope, steps[0]); result.Success {
		t.Fatal("redirected deletion removed another file", result)
	}
	body, err := os.ReadFile(other)
	if err != nil || string(body) != "postimage" {
		t.Fatal(string(body), err)
	}
}

func TestSelectingLatestCheckpointDoesNotLoadOlderImages(t *testing.T) {
	scope, root, store := checkpointFixture(t)
	older := recordedWrite(t, scope, root, "session", "older.txt", "old postimage")
	_, err := store.db.Exec("UPDATE checkpoints SET metadata_json=? WHERE checkpoint_id=?", strings.Repeat("x", maxState+1), older)
	if err != nil {
		t.Fatal(err)
	}
	latest := recordedWrite(t, scope, root, "session", "latest.txt", "latest postimage")
	records, err := store.selectRecords(context.Background(), "session", "")
	if err != nil || len(records) != 1 || records[0].ID != latest {
		t.Fatal("latest selection unnecessarily read old file contents", records, err)
	}
	if _, err := store.selectRecords(context.Background(), "", older); err == nil {
		t.Fatal("oversized checkpoint accepted")
	}
}
func TestCheckpointPreparedRecordRecoversOnlyAnActualPostimage(t *testing.T) {
	scope, _, store := checkpointFixture(t)
	c, err := propose(scope, "write_file", map[string]any{"path": "note.txt", "content": "new file"})
	if err != nil {
		t.Fatal(err)
	}
	id, err := store.prepare(context.Background(), c, scope.workspace, "session", "write_file")
	if err != nil {
		t.Fatal(err)
	}
	records, _ := store.selectRecords(context.Background(), "", id)
	if _, err := store.restore(context.Background(), scope, records); err == nil {
		t.Fatal("unexecuted write was restored")
	}
	if result := applyChange(scope, c); !result.Success {
		t.Fatal(result)
	}
	if _, err := store.restore(context.Background(), scope, records); err != nil {
		t.Fatal("durable preimage could not recover interrupted activation", err)
	}
}
func TestCheckpointConcurrentRestoreHasOneWinner(t *testing.T) {
	scope, root, store := checkpointFixture(t)
	id := recordedWrite(t, scope, root, "session", "new.txt", "actual new file")
	records, _ := store.selectRecords(context.Background(), "", id)
	var wg sync.WaitGroup
	results := make(chan error, 4)
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			own, err := openCheckpoints(root)
			if err == nil {
				defer own.Close()
				copyOfRecords := append([]checkpoint{}, records...)
				_, err = own.restore(context.Background(), scope, copyOfRecords)
			}
			results <- err
		}()
	}
	wg.Wait()
	close(results)
	winners := 0
	for err := range results {
		if err == nil {
			winners++
		}
	}
	if winners != 1 {
		t.Fatal("restore was not serialized", winners)
	}
}
func TestNativeAppRewindApprovalAndUndoPersistAcrossSessionSwitch(t *testing.T) {
	c, root, project := testApplication(t, simpleProvider())
	ready := c.until(t, "session.ready")
	id := ready["session_id"].(string)
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "write", "text": "write new.txt"})
	menu := c.until(t, "approval.requested")
	c.send(t, map[string]any{"type": "dialog.respond", "turn_id": "write", "request_id": menu["request_id"], "choice": 0})
	c.until(t, "turn.completed")
	c.until(t, "session.state")
	c.send(t, map[string]any{"type": "session.new"})
	c.until(t, "session.ready")
	c.send(t, map[string]any{"type": "session.resume", "session_id": id})
	c.until(t, "session.ready")
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "rewind", "text": "/rewind code"})
	menu = c.until(t, "approval.requested")
	if !strings.Contains(menu["context"].(string), "new.txt") || !strings.Contains(menu["context"].(string), "Remove file") {
		t.Fatal(menu)
	}
	c.send(t, map[string]any{"type": "dialog.respond", "turn_id": "rewind", "request_id": menu["request_id"], "choice": 0})
	if event := c.until(t, "turn.completed"); event["status"] != "ok" {
		t.Fatal(event)
	}
	c.until(t, "session.state")
	if _, err := os.Stat(filepath.Join(project, "new.txt")); !os.IsNotExist(err) {
		t.Fatal(err)
	}
	c.send(t, map[string]any{"type": "turn.submit", "turn_id": "undo", "text": "/undo"})
	if event := c.until(t, "turn.completed"); event["status"] != "ok" {
		t.Fatal(event)
	}
	state := c.until(t, "session.state")
	if state["history_reset"] != true || !strings.Contains(state["history_notice"].(string), "Undone") {
		t.Fatal("frontend was not told to refresh rewound history", state)
	}
	if len(state["messages"].([]any)) != 0 {
		t.Fatal("undo left stale display history", state)
	}
	c.close(t)
	saved, err := loadSession(root, id)
	if err != nil || len(saved.Messages) != 0 {
		t.Fatal(saved, err)
	}
	contents, err := sessionContents(saved)
	if err != nil || len(contents) != 0 {
		t.Fatal("undo left provider tool history", contents, err)
	}
}
func TestNativeRewindDeclineReadOnlyAndHeadlessPolicies(t *testing.T) {
	for _, policy := range []string{"deny", "ask"} {
		t.Run(policy, func(t *testing.T) {
			scope, root, _ := checkpointFixture(t)
			id := recordedWrite(t, scope, root, "session", "new.txt", "postimage")
			key := "denied"
			if policy == "ask" {
				key = "ask_always"
			}
			atomicJSON(filepath.Join(root, "tool_policy.json"), map[string]any{key: []string{"restore_checkpoint"}})
			s := &server{scope: scope, opts: appOptions{root: root, noninteractive: true, skipPermissions: true}, snap: snapshot{ID: "session"}, grants: map[string]bool{}}
			if message, err := s.rewindCommand(context.Background(), "turn", []string{"code", id, "--yes"}); err == nil && !strings.Contains(message, "cancelled") {
				t.Fatal("persistent policy bypassed", policy)
			}
			body, _ := os.ReadFile(filepath.Join(scope.workspace, "new.txt"))
			if string(body) != "postimage" {
				t.Fatal(string(body))
			}
		})
	}
	scope, root, store := checkpointFixture(t)
	id := recordedWrite(t, scope, root, "session", "new.txt", "postimage")
	records, _ := store.selectRecords(context.Background(), "", id)
	scope.readOnly = true
	if _, err := store.restore(context.Background(), scope, records); err == nil {
		t.Fatal("read-only session restored files")
	}
}
func TestConversationUndoRetainsEarlierSignedToolHistory(t *testing.T) {
	older := &genai.Content{Role: "model", Parts: []*genai.Part{call("read_file", map[string]any{"path": "first.txt"})}}
	s := &server{snap: snapshot{Messages: []message{{"role": "user", "content": "first"}, {"role": "assistant", "content": "first answer"}, {"role": "user", "content": "second"}}}, contents: []*genai.Content{genai.NewContentFromText("first", genai.RoleUser), older, {Role: "user", Parts: []*genai.Part{{FunctionResponse: &genai.FunctionResponse{Name: "read_file", Response: map[string]any{"success": true}}}}}, genai.NewContentFromText("first answer", genai.RoleModel), genai.NewContentFromText("second", genai.RoleUser)}}
	if _, err := s.undoConversation(); err != nil {
		t.Fatal(err)
	}
	if len(s.snap.Messages) != 2 || len(s.contents) != 4 || s.contents[1] != older || s.contents[1].Parts[0].ThoughtSignature == nil {
		t.Fatal("earlier tool signature lost")
	}
	if _, err := s.undoConversation(); err != nil {
		t.Fatal(err)
	}
	if len(s.contents) != 0 {
		t.Fatal("provider history remained")
	}
}
