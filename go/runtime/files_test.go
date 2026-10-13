package main

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestWindowsVerbatimSpellingsAreEquivalentWithoutDeviceRewrites(t *testing.T) {
	for input, want := range map[string]string{
		`\\?\C:\project\new.txt`:       `C:\project\new.txt`,
		`\\?\UNC\server\share\new.txt`: `\\server\share\new.txt`,
		`\\?\unc\server\share\new.txt`: `\\server\share\new.txt`,
		`\\?\Volume{123}\file`:         `\\?\Volume{123}\file`,
		`\\.\pipe\name`:                `\\.\pipe\name`,
		`C:relative`:                   `C:relative`,
	} {
		if got := normalWindowsPath(input); got != want {
			t.Fatalf("%q became %q, wanted %q", input, got, want)
		}
	}
	if runtime.GOOS != "windows" {
		return
	}
	root := t.TempDir()
	project, err := newScope(`\\?\`+root, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	change, err := propose(project, "write_file", map[string]any{"path": "new.txt", "content": "complete native body"})
	if err != nil {
		t.Fatal(err)
	}
	if result := applyRecordedChange(context.Background(), project, t.TempDir(), "session", "write_file", change); !result.Success {
		t.Fatal(result)
	}
	p, err := project.open(filepath.Join(root, "new.txt"), true)
	if err != nil {
		t.Fatal(err)
	}
	p.Close()
}

func TestFilesReadAndEditActualSourceWithoutPython(t *testing.T) {
	root := t.TempDir()
	scope, err := newScope(root, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "note.txt")
	if err := os.WriteFile(path, []byte("first\r\nsecond\r\n"), 0644); err != nil {
		t.Fatal(err)
	}
	read := fileRead(context.Background(), scope, map[string]any{"path": "note.txt", "offset": 1, "limit": 1})
	if !read.Success || read.Data["lines"] != 1 || !strings.Contains(read.Data["content"].(string), "   2│ second") {
		t.Fatal(read)
	}
	change, err := propose(scope, "edit_file", map[string]any{"path": "note.txt", "old_string": "first\nsecond", "new_string": "first\nchanged"})
	if err != nil {
		t.Fatal(err)
	}
	if result := applyChange(scope, change); !result.Success {
		t.Fatal(result)
	}
	body, err := os.ReadFile(path)
	if err != nil || string(body) != "first\r\nchanged\r\n" {
		t.Fatal(string(body), err)
	}
	listed := fileList(context.Background(), scope, map[string]any{"path": "."})
	if !listed.Success || listed.Data["count"] != 1 {
		t.Fatal(listed)
	}
}
func TestFilesRejectSpoofedRootsAndUnapprovedExternalTargets(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	scope, err := newScope(root, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range []map[string]any{{"path": filepath.Join(outside, "data")}, {"path": "../escape"}, {"path": "note.txt", "_permission_mode": "full-access"}, {"path": "note.txt", "_workspace": outside}} {
		if result := fileRead(context.Background(), scope, p); result.Success {
			t.Fatal("read escape", p)
		}
		p["content"] = "must not be written"
		if _, err := propose(scope, "write_file", p); err == nil {
			t.Fatal("write escape", p)
		}
	}
	file := filepath.Join(outside, "secret.txt")
	if err := os.WriteFile(file, []byte("external"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(file, filepath.Join(root, "link")); err == nil {
		if result := fileRead(context.Background(), scope, map[string]any{"path": "link"}); result.Success {
			t.Fatal("symlink escape")
		}
	}
}
func TestExplicitReadRootsDoNotGrantWriteAccess(t *testing.T) {
	root, outside := t.TempDir(), t.TempDir()
	file := filepath.Join(outside, "note.txt")
	if err := os.WriteFile(file, []byte("read-only"), 0644); err != nil {
		t.Fatal(err)
	}
	scope, err := newScope(root, []string{outside}, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if result := fileRead(context.Background(), scope, map[string]any{"path": file}); !result.Success {
		t.Fatal(result)
	}
	if _, err := propose(scope, "write_file", map[string]any{"path": file, "content": "overwrite"}); err == nil {
		t.Fatal("read root became writable")
	}
}
func TestAnApprovalCannotOverwriteAnInterveningExternalEdit(t *testing.T) {
	root := t.TempDir()
	scope, err := newScope(root, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "note.txt")
	if err := os.WriteFile(path, []byte("original"), 0644); err != nil {
		t.Fatal(err)
	}
	change, err := propose(scope, "edit_file", map[string]any{"path": "note.txt", "old_string": "original", "new_string": "approved"})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("changed by user"), 0644); err != nil {
		t.Fatal(err)
	}
	if result := applyChange(scope, change); result.Success || !strings.Contains(result.Error, "changed during approval") {
		t.Fatal(result)
	}
	body, err := os.ReadFile(path)
	if err != nil || string(body) != "changed by user" {
		t.Fatal(string(body), err)
	}
}
func TestNewFilesAreAtomicAndAmbiguousEditsRemainUnchanged(t *testing.T) {
	root := t.TempDir()
	scope, err := newScope(root, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	change, err := propose(scope, "write_file", map[string]any{"path": "src/note.txt", "content": "two two"})
	if err != nil {
		t.Fatal(err)
	}
	if result := applyChange(scope, change); !result.Success {
		t.Fatal(result)
	}
	if _, err := propose(scope, "edit_file", map[string]any{"path": "src/note.txt", "old_string": "two", "new_string": "one"}); err == nil {
		t.Fatal("ambiguous edit was accepted")
	}
	change, err = propose(scope, "edit_file", map[string]any{"path": "src/note.txt", "old_string": "two", "new_string": "one", "replace_all": true})
	if err != nil {
		t.Fatal(err)
	}
	if result := applyChange(scope, change); !result.Success {
		t.Fatal(result)
	}
	body, err := os.ReadFile(filepath.Join(root, "src", "note.txt"))
	if err != nil || string(body) != "one one" {
		t.Fatal(string(body), err)
	}
	files, err := filepath.Glob(filepath.Join(root, "src", ".aria-edit-*"))
	if err != nil || len(files) != 0 {
		t.Fatal(files, err)
	}
}
