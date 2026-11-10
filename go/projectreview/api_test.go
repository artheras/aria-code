package main

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func makeZip(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	for name, content := range files {
		w, err := zw.Create(name)
		if err != nil {
			t.Fatalf("failed to create zip entry %s: %v", name, err)
		}
		if _, err := io.WriteString(w, content); err != nil {
			t.Fatalf("failed to write zip entry %s: %v", name, err)
		}
	}
	if err := zw.Close(); err != nil {
		t.Fatalf("failed to close zip writer: %v", err)
	}
	return buf.Bytes()
}

func setupTestHandler(t *testing.T, token string) (http.Handler, *service) {
	t.Helper()
	tmpDir := t.TempDir()
	s := &service{
		root: tmpDir,
		ttl:  24 * time.Hour,
	}
	t.Setenv("ARIA_CODE_REVIEW_TOKEN", token)
	return s.routes(), s
}

func TestHealthEndpoint(t *testing.T) {
	handler, _ := setupTestHandler(t, "test-secret-token")
	req := httptest.NewRequest("GET", "/health", nil)
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected 200 OK, got %d", rec.Code)
	}

	var body map[string]string
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("failed to decode health response: %v", err)
	}
	if body["status"] != "ok" || body["service"] != "aria-code-project-review" {
		t.Errorf("unexpected health response payload: %+v", body)
	}
}

func TestAuthValidation(t *testing.T) {
	token := "correct-token-123456"
	handler, _ := setupTestHandler(t, token)

	// Test 1: No auth header -> 401
	req := httptest.NewRequest("POST", "/v1/project-reviews", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("expected 401 Unauthorized, got %d", rec.Code)
	}

	// Test 2: Wrong token -> 401
	req = httptest.NewRequest("POST", "/v1/project-reviews", nil)
	req.Header.Set("Authorization", "Bearer wrong-token")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("expected 401 Unauthorized for wrong token, got %d", rec.Code)
	}

	// Test 3: Unset server token -> 503
	t.Setenv("ARIA_CODE_REVIEW_TOKEN", "")
	req = httptest.NewRequest("POST", "/v1/project-reviews", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusServiceUnavailable {
		t.Errorf("expected 503 Service Unavailable when server unconfigured, got %d", rec.Code)
	}
}

func TestProjectReviewReporting(t *testing.T) {
	token := "review-token"
	handler, _ := setupTestHandler(t, token)

	zipBytes := makeZip(t, map[string]string{
		"sample/app.py": "from fastapi import FastAPI\nAPI_KEY = 'super-secret-value'\ntry:\n    run()\nexcept:\n    pass\n",
		"sample/pyproject.toml": "[project]\nname='sample'\n",
		"sample/.env": "SECRET=leak",
	})

	var body bytes.Buffer
	mw := multipart.NewWriter(&body)
	fw, err := mw.CreateFormFile("project", "sample.zip")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fw.Write(zipBytes); err != nil {
		t.Fatal(err)
	}
	mw.Close()

	req := httptest.NewRequest("POST", "/v1/project-reviews", &body)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200 OK, got %d: %s", rec.Code, rec.Body.String())
	}

	var report map[string]any
	if err := json.NewDecoder(rec.Body).Decode(&report); err != nil {
		t.Fatal(err)
	}

	if report["schema_version"] != "aria.project-review.v1" {
		t.Errorf("unexpected schema version: %v", report["schema_version"])
	}

	summary, ok := report["summary"].(map[string]any)
	if !ok {
		t.Fatalf("missing summary: %+v", report)
	}
	if skipped, ok := summary["skipped_sensitive_files"].(float64); !ok || int(skipped) < 1 {
		t.Errorf("expected at least 1 skipped sensitive file, got %v", summary["skipped_sensitive_files"])
	}

	findings, ok := report["findings"].([]any)
	if !ok || len(findings) == 0 {
		t.Fatalf("expected findings, got: %v", report["findings"])
	}

	foundSecret := false
	for _, f := range findings {
		fm, ok := f.(map[string]any)
		if ok && fm["rule"] == "hardcoded-secret" {
			foundSecret = true
			break
		}
	}
	if !foundSecret {
		t.Errorf("expected hardcoded-secret finding in %+v", findings)
	}
}

func TestWorkspaceLifecycleEndToEnd(t *testing.T) {
	token := "workspace-token"
	handler, _ := setupTestHandler(t, token)

	zipBytes := makeZip(t, map[string]string{
		"src/main.py": "def hello():\n    return 'initial'\n",
		"config.json": `{"version": 1}`,
	})

	// 1. Create Workspace
	var body bytes.Buffer
	mw := multipart.NewWriter(&body)
	fw, _ := mw.CreateFormFile("project", "repo.zip")
	_, _ = fw.Write(zipBytes)
	_ = mw.WriteField("owner_id", "user_test_123")
	_ = mw.WriteField("run_id", "run_abc_456")
	mw.Close()

	req := httptest.NewRequest("POST", "/v1/workspaces", &body)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK && rec.Code != http.StatusCreated {
		t.Fatalf("create workspace returned %d: %s", rec.Code, rec.Body.String())
	}

	var wsCreated map[string]any
	_ = json.NewDecoder(rec.Body).Decode(&wsCreated)
	wsID, _ := wsCreated["workspace_id"].(string)
	if !strings.HasPrefix(wsID, "ws_") {
		t.Fatalf("invalid workspace id returned: %s", wsID)
	}

	// 2. Read File
	readReqBody, _ := json.Marshal(map[string]any{
		"owner_id": "user_test_123",
		"path":     "src/main.py",
	})
	req = httptest.NewRequest("POST", "/v1/workspaces/"+wsID+"/files/read", bytes.NewReader(readReqBody))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("readFile returned %d: %s", rec.Code, rec.Body.String())
	}
	var fileData map[string]any
	_ = json.NewDecoder(rec.Body).Decode(&fileData)
	content, _ := fileData["content"].(string)
	if !strings.Contains(content, "initial") {
		t.Errorf("unexpected file content: %s", content)
	}

	// 3. List Files
	listReqBody, _ := json.Marshal(map[string]any{
		"owner_id": "user_test_123",
		"pattern":  "**/*.py",
	})
	req = httptest.NewRequest("POST", "/v1/workspaces/"+wsID+"/files/list", bytes.NewReader(listReqBody))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("listFiles returned %d: %s", rec.Code, rec.Body.String())
	}
	var listData map[string]any
	_ = json.NewDecoder(rec.Body).Decode(&listData)
	itemsList, _ := listData["items"].([]any)
	if len(itemsList) != 1 {
		t.Errorf("expected 1 python file in items, got %d", len(itemsList))
	}

	// 4. Propose Change
	newContent := "def hello():\n    return 'updated'\n"
	changeReqBody, _ := json.Marshal(map[string]any{
		"owner_id": "user_test_123",
		"path":     "src/main.py",
		"content":  newContent,
	})
	req = httptest.NewRequest("POST", "/v1/workspaces/"+wsID+"/changes", bytes.NewReader(changeReqBody))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK && rec.Code != http.StatusCreated {
		t.Fatalf("proposeChange returned %d: %s", rec.Code, rec.Body.String())
	}
	var changeData map[string]any
	_ = json.NewDecoder(rec.Body).Decode(&changeData)
	changeID, _ := changeData["change_id"].(string)
	if !strings.HasPrefix(changeID, "chg_") {
		t.Fatalf("invalid change id: %s", changeID)
	}

	// 5. Apply Change
	applyReqBody, _ := json.Marshal(map[string]any{
		"owner_id": "user_test_123",
	})
	req = httptest.NewRequest("POST", "/v1/workspaces/"+wsID+"/changes/"+changeID+"/apply", bytes.NewReader(applyReqBody))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("applyChange returned %d: %s", rec.Code, rec.Body.String())
	}

	// 6. Verify Content was updated
	req = httptest.NewRequest("POST", "/v1/workspaces/"+wsID+"/files/read", bytes.NewReader(readReqBody))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	_ = json.NewDecoder(rec.Body).Decode(&fileData)
	if updatedStr, _ := fileData["content"].(string); !strings.Contains(updatedStr, "updated") {
		t.Errorf("content not updated: got %v", fileData["content"])
	}

	// 7. Rollback Change
	rollbackReqBody, _ := json.Marshal(map[string]any{
		"owner_id": "user_test_123",
	})
	req = httptest.NewRequest("POST", "/v1/workspaces/"+wsID+"/changes/"+changeID+"/rollback", bytes.NewReader(rollbackReqBody))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("rollbackChange returned %d: %s", rec.Code, rec.Body.String())
	}

	// 8. Verify Content rolled back
	req = httptest.NewRequest("POST", "/v1/workspaces/"+wsID+"/files/read", bytes.NewReader(readReqBody))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	_ = json.NewDecoder(rec.Body).Decode(&fileData)
	if restoredStr, _ := fileData["content"].(string); !strings.Contains(restoredStr, "initial") {
		t.Errorf("content not rolled back: got %v", fileData["content"])
	}

	// 9. Delete Workspace
	req = httptest.NewRequest("DELETE", "/v1/workspaces/"+wsID+"?owner_id=user_test_123", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK && rec.Code != http.StatusNoContent {
		t.Errorf("delete workspace returned %d", rec.Code)
	}
}

func TestUnsafePathRejection(t *testing.T) {
	token := "security-token"
	handler, _ := setupTestHandler(t, token)

	zipBytes := makeZip(t, map[string]string{
		"../outside.py": "print('malicious')",
	})

	var body bytes.Buffer
	mw := multipart.NewWriter(&body)
	fw, _ := mw.CreateFormFile("project", "bad.zip")
	_, _ = fw.Write(zipBytes)
	mw.Close()

	req := httptest.NewRequest("POST", "/v1/project-reviews", &body)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("expected 400 Bad Request for path traversal, got %d", rec.Code)
	}
}

func TestCleanupExpiredWorkspaces(t *testing.T) {
	tmpDir := t.TempDir()
	s := &service{
		root: tmpDir,
		ttl:  1 * time.Hour,
	}

	wsDir := filepath.Join(tmpDir, "ws_0123456789abcdef01234567")
	if err := os.MkdirAll(wsDir, 0700); err != nil {
		t.Fatal(err)
	}

	// Write expired manifest
	manifest := workspaceManifest{
		SchemaVersion: "aria.workspace.v1",
		WorkspaceID:   "ws_0123456789abcdef01234567",
		OwnerHash:     subjectHash("test"),
		ExpiresAt:     time.Now().Add(-2 * time.Hour).UTC().Format(time.RFC3339Nano),
	}
	data, _ := json.Marshal(manifest)
	if err := os.WriteFile(filepath.Join(wsDir, ".aria-workspace.json"), data, 0600); err != nil {
		t.Fatal(err)
	}

	s.cleanupExpired(time.Now())

	if _, err := os.Stat(wsDir); !os.IsNotExist(err) {
		t.Errorf("expected expired workspace directory to be removed, but stat was: %v", err)
	}
}
