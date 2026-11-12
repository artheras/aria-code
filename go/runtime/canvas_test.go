package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCanvasLifecycleAndPublishArtifact(t *testing.T) {
	home := t.TempDir()
	workspace := t.TempDir()
	t.Setenv("ARIA_HOME", home)

	sc, err := newScope(workspace, nil, nil, true)
	if err != nil {
		t.Fatal(err)
	}

	// 1. Tool validation: missing both path & content
	res := publishArtifactTool(context.Background(), sc, home, map[string]any{})
	if res.Success {
		t.Fatal("expected failure when neither path nor content provided")
	}

	// 2. Tool validation: unrenderable extension
	res = publishArtifactTool(context.Background(), sc, home, map[string]any{
		"content":  "binary",
		"filename": "run.exe",
	})
	if res.Success || !strings.Contains(res.Error, "cannot be rendered") {
		t.Fatalf("expected unrenderable error, got %v", res)
	}

	// 3. Tool validation: path traversal in filename
	res = publishArtifactTool(context.Background(), sc, home, map[string]any{
		"content":  "bad",
		"filename": "../escape.html",
	})
	if res.Success || !strings.Contains(res.Error, "bare file name") {
		t.Fatalf("expected bare file name error, got %v", res)
	}

	// 4. Start canvas server
	cs, err := startCanvasServer()
	if err != nil {
		t.Fatal(err)
	}
	defer stopCanvasServer()

	disc, err := canvasDiscoveryPath()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(disc); err != nil {
		t.Fatalf("expected canvas.json to exist at %s", disc)
	}

	// 5. Publish inline HTML artifact
	htmlContent := "<h1>Revenue Report</h1>"
	res = publishArtifactTool(context.Background(), sc, home, map[string]any{
		"content":  htmlContent,
		"filename": "revenue.html",
		"title":    "Q3 Revenue",
		"topic":    "Financials",
	})
	if !res.Success {
		t.Fatalf("failed to publish inline artifact: %v", res)
	}
	v1Path := res.Data["path"].(string)
	if data, err := os.ReadFile(v1Path); err != nil || string(data) != htmlContent {
		t.Fatalf("published artifact content mismatch: %s, %v", string(data), err)
	}
	if res.Data["live"] != true {
		t.Fatalf("expected live=true when server is running: %v", res.Data)
	}

	// 6. Publish v2 for the same topic
	htmlContentV2 := "<h1>Revenue Report v2</h1>"
	res2 := publishArtifactTool(context.Background(), sc, home, map[string]any{
		"content":  htmlContentV2,
		"filename": "revenue.html",
		"title":    "Q3 Revenue v2",
		"topic":    "Financials",
	})
	if !res2.Success {
		t.Fatalf("failed to publish v2 artifact: %v", res2)
	}
	v2Path := res2.Data["path"].(string)
	if v1Path == v2Path {
		t.Fatalf("expected distinct file paths across publishes, got %s", v1Path)
	}

	// 7. Verify /state endpoint returns versions via direct ServeHTTP
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/state", nil)
	cs.handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200 from /state, got %d", rec.Code)
	}
	var state map[string]any
	if err := json.NewDecoder(rec.Body).Decode(&state); err != nil {
		t.Fatal(err)
	}
	threads := state["threads"].(map[string]any)
	finThread, ok := threads["Financials"].(map[string]any)
	if !ok {
		t.Fatalf("thread Financials not found in state: %v", threads)
	}
	versions := finThread["versions"].([]any)
	if len(versions) != 2 {
		t.Fatalf("expected 2 versions in thread Financials, got %d", len(versions))
	}

	// 8. Verify /artifact endpoint serves the file with CSP headers
	recArt := httptest.NewRecorder()
	reqArt := httptest.NewRequest(http.MethodGet, "/artifact/Financials?v=0", nil)
	cs.handler.ServeHTTP(recArt, reqArt)
	if recArt.Code != http.StatusOK {
		t.Fatalf("expected 200 from /artifact, got %d", recArt.Code)
	}
	csp := recArt.Header().Get("Content-Security-Policy")
	if !strings.Contains(csp, "connect-src 'none'") {
		t.Fatalf("missing CSP connect-src 'none': %s", csp)
	}
	body := recArt.Body.String()
	if body != htmlContent {
		t.Fatalf("expected v1 content, got %s", body)
	}

	// 9. Verify /api/bridge POST & GET
	bridgePayload := `{"type":"aria.canvas.action","action":"filter_change","payload":{"year":2026},"topic":"Financials"}`
	recBridgePost := httptest.NewRecorder()
	reqBridgePost := httptest.NewRequest(http.MethodPost, "/api/bridge", strings.NewReader(bridgePayload))
	reqBridgePost.Header.Set("Content-Type", "application/json")
	cs.handler.ServeHTTP(recBridgePost, reqBridgePost)
	if recBridgePost.Code != http.StatusOK {
		t.Fatalf("bridge POST failed: %d", recBridgePost.Code)
	}

	recBridgeGet := httptest.NewRecorder()
	reqBridgeGet := httptest.NewRequest(http.MethodGet, "/api/bridge", nil)
	cs.handler.ServeHTTP(recBridgeGet, reqBridgeGet)
	if recBridgeGet.Code != http.StatusOK {
		t.Fatalf("bridge GET failed: %d", recBridgeGet.Code)
	}
	var bridgeGet map[string]any
	json.NewDecoder(recBridgeGet.Body).Decode(&bridgeGet)
	actions := bridgeGet["actions"].([]any)
	if len(actions) == 0 {
		t.Fatalf("expected logged bridge action, got %v", bridgeGet)
	}

	// 10. Publish from existing workspace file
	wsFile := filepath.Join(workspace, "chart.svg")
	_ = os.WriteFile(wsFile, []byte("<svg></svg>"), 0o644)
	resFile := publishArtifactTool(context.Background(), sc, home, map[string]any{
		"path": "chart.svg",
	})
	if !resFile.Success {
		t.Fatalf("failed to publish workspace file: %v", resFile)
	}

	// 11. Refuse outside workspace
	outsideFile := filepath.Join(home, "secret.svg")
	_ = os.WriteFile(outsideFile, []byte("<svg></svg>"), 0o644)
	resOutside := publishArtifactTool(context.Background(), sc, home, map[string]any{
		"path": outsideFile,
	})
	if resOutside.Success {
		t.Fatal("expected refusal for file outside workspace")
	}

	// 12. Test /canvas and /artifacts commands
	s, err := newServer(appOptions{root: home, workspace: workspace}, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	statusOut, err := s.command(context.Background(), "t", "/canvas status")
	if err != nil || !strings.Contains(statusOut, "running at") {
		t.Fatalf("canvas status failed: %s, %v", statusOut, err)
	}
	bridgeOut, err := s.command(context.Background(), "t", "/canvas bridge")
	if err != nil || !strings.Contains(bridgeOut, "filter_change") {
		t.Fatalf("canvas bridge failed: %s, %v", bridgeOut, err)
	}
	artListOut, err := s.command(context.Background(), "t", "/artifacts")
	if err != nil || !strings.Contains(artListOut, "Recent Artifacts") {
		t.Fatalf("/artifacts command failed: %s, %v", artListOut, err)
	}

	// Stop canvas
	stopOut, err := s.command(context.Background(), "t", "/canvas stop")
	if err != nil || !strings.Contains(stopOut, "Stopped") {
		t.Fatalf("canvas stop failed: %s, %v", stopOut, err)
	}
}
