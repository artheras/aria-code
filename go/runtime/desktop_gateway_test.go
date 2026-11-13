package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestTaskBriefAndDualStateOutcome(t *testing.T) {
	// 1. TaskBrief validation
	brief, err := newTaskBrief("Build dashboard", "Executive review", []string{"ui/dashboard.js"}, []string{"tests pass"})
	if err != nil {
		t.Fatal(err)
	}
	if brief.Goal != "Build dashboard" || len(brief.Boundaries) != 1 {
		t.Fatalf("unexpected brief: %+v", brief)
	}

	// Empty goal rejected
	if _, err := newTaskBrief("", "", nil, nil); err == nil {
		t.Fatal("expected error on empty goal")
	}

	// 2. Dual-state TaskOutcome: unverified when not fresh or checks missing
	outcome := projectTaskOutcome(map[string]any{"status": "done"}, false)
	if outcome.VerificationState != "unverified" || outcome.RequiresRefresh != true {
		t.Fatalf("expected unverified and requires_refresh: %+v", outcome)
	}

	// Dual-state: verified when fresh and checks green
	outcomeVerified := projectTaskOutcome(map[string]any{
		"status":   "done",
		"verified": true,
		"verification": map[string]any{
			"status":   "verified",
			"verified": true,
			"checks":   []any{map[string]any{"name": "unit-test", "success": true}},
		},
	}, true)
	if outcomeVerified.VerificationState != "verified" || *outcomeVerified.Verified != true {
		t.Fatalf("expected verified outcome: %+v", outcomeVerified)
	}

	// Dual-state: failed when verification checks failed
	outcomeFailed := projectTaskOutcome(map[string]any{
		"status":   "done",
		"verified": false,
		"verification": map[string]any{
			"status":   "failed",
			"verified": false,
			"checks":   []any{map[string]any{"name": "lint", "success": false}},
		},
	}, true)
	if outcomeFailed.VerificationState != "failed" || *outcomeFailed.Verified != false {
		t.Fatalf("expected failed outcome: %+v", outcomeFailed)
	}
}

func TestDesktopGatewayLifecycleAndDiscovery(t *testing.T) {
	root := t.TempDir()
	workspace := t.TempDir()
	t.Setenv("ARIA_HOME", root)

	// Create a sample .aria/task-context.json
	_ = os.MkdirAll(filepath.Join(workspace, ".aria"), 0o755)
	manifestData := `{
		"version": 1,
		"entries": [
			{
				"id": "ui-guidelines",
				"kind": "skill",
				"path": "docs/UI.md",
				"description": "Design system and UI guidelines",
				"auto": true
			}
		]
	}`
	_ = os.WriteFile(filepath.Join(workspace, ".aria", "task-context.json"), []byte(manifestData), 0o644)

	// 1. Start gateway
	gw, err := startDesktopGateway(root, workspace, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer stopDesktopGateway()

	disc, err := runtimeDiscoveryPath()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(disc); err != nil {
		t.Fatalf("expected runtime.json to exist: %s", disc)
	}

	// 2. Health check via ServeHTTP
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	gw.httpServer.Handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"status":"ok"`) {
		t.Fatalf("unexpected health response: %d %s", rec.Code, rec.Body.String())
	}

	// 3. Runtime status
	recStatus := httptest.NewRecorder()
	reqStatus := httptest.NewRequest(http.MethodGet, "/api/v2/runtime/status", nil)
	gw.httpServer.Handler.ServeHTTP(recStatus, reqStatus)
	var statusResp map[string]any
	if err := json.Unmarshal(recStatus.Body.Bytes(), &statusResp); err != nil {
		t.Fatalf("failed to decode runtime status: %v", err)
	}
	if recStatus.Code != http.StatusOK || statusResp["workspace"] != workspace {
		t.Fatalf("unexpected runtime status response: %d %s", recStatus.Code, recStatus.Body.String())
	}
	if statusResp["governance_mode"] != "zero_trust" || statusResp["preflight_syntax_validation"] != true {
		t.Fatalf("expected zero_trust governance in statusResp: %v", statusResp)
	}

	// 4. Task Context endpoint
	recCtx := httptest.NewRecorder()
	reqCtx := httptest.NewRequest(http.MethodGet, "/api/v2/task/context", nil)
	gw.httpServer.Handler.ServeHTTP(recCtx, reqCtx)
	if recCtx.Code != http.StatusOK || !strings.Contains(recCtx.Body.String(), "ui-guidelines") {
		t.Fatalf("unexpected task context response: %d %s", recCtx.Code, recCtx.Body.String())
	}

	// 5. Approvals Inbox workflow: add approval, fetch, approve via POST
	gw.mu.Lock()
	appCh := make(chan bool, 1)
	gw.approvalCh["app_1"] = appCh
	gw.approvals["app_1"] = pendingApproval{
		ID:          "app_1",
		Tool:        "edit_file",
		Description: "Modify config.go",
	}
	gw.mu.Unlock()

	recApps := httptest.NewRecorder()
	reqApps := httptest.NewRequest(http.MethodGet, "/api/v2/approvals", nil)
	gw.httpServer.Handler.ServeHTTP(recApps, reqApps)
	if !strings.Contains(recApps.Body.String(), "app_1") {
		t.Fatalf("expected app_1 in approvals: %s", recApps.Body.String())
	}

	// Respond to approval
	recResp := httptest.NewRecorder()
	reqResp := httptest.NewRequest(http.MethodPost, "/api/v2/approvals/respond", strings.NewReader(`{"id":"app_1","approve":true}`))
	reqResp.Header.Set("Content-Type", "application/json")
	gw.httpServer.Handler.ServeHTTP(recResp, reqResp)
	if recResp.Code != http.StatusOK {
		t.Fatalf("expected 200 from approval respond: %d %s", recResp.Code, recResp.Body.String())
	}
	select {
	case approved := <-appCh:
		if !approved {
			t.Fatal("expected approved=true")
		}
	default:
		t.Fatal("approval channel did not receive response")
	}

	// 6. Four-way Agent Lifecycle separation & Dual-Track Observability (DOTS vs CODEX_LOG)
	gw.mu.Lock()
	gw.childTasks["worktree-101"] = &ChildTaskRecord{
		TaskID:        "worktree-101",
		ParentAgentID: gw.identity.AgentID,
		Kind:          "worktree",
		Status:        "running",
		StartedAt:     "2026-11-11T12:00:00Z",
	}
	gw.mu.Unlock()

	// 6a. Pause Agent: pauses agent scheduling, preserves running worktree-101
	recPause := httptest.NewRecorder()
	reqPause := httptest.NewRequest(http.MethodPost, "/api/v2/lifecycle/action", strings.NewReader(`{"action":"pause_agent"}`))
	reqPause.Header.Set("Content-Type", "application/json")
	gw.httpServer.Handler.ServeHTTP(recPause, reqPause)
	if recPause.Code != http.StatusOK || !strings.Contains(recPause.Body.String(), `"agent_paused":true`) || !strings.Contains(recPause.Body.String(), `"worktree-101"`) {
		t.Fatalf("expected pause_agent to preserve running worktree-101: %d %s", recPause.Code, recPause.Body.String())
	}

	// 6b. Cancel Task: explicitly cancels worktree-101
	recCancel := httptest.NewRecorder()
	reqCancel := httptest.NewRequest(http.MethodPost, "/api/v2/lifecycle/action", strings.NewReader(`{"action":"cancel_task","task_id":"worktree-101"}`))
	reqCancel.Header.Set("Content-Type", "application/json")
	gw.httpServer.Handler.ServeHTTP(recCancel, reqCancel)
	if recCancel.Code != http.StatusOK || !strings.Contains(recCancel.Body.String(), `"task_status":"cancelled"`) {
		t.Fatalf("expected cancel_task to mark worktree-101 cancelled: %d %s", recCancel.Code, recCancel.Body.String())
	}

	// 6c. Usage Analytics (DOTS) vs Compliance Logs (CODEX_LOG)
	recUsage := httptest.NewRecorder()
	reqUsage := httptest.NewRequest(http.MethodGet, "/api/v2/analytics/usage", nil)
	gw.httpServer.Handler.ServeHTTP(recUsage, reqUsage)
	if recUsage.Code != http.StatusOK || !strings.Contains(recUsage.Body.String(), `"client_id":"DOTS"`) || !strings.Contains(recUsage.Body.String(), `"cancelled_task_count":1`) {
		t.Fatalf("unexpected analytics usage response: %d %s", recUsage.Code, recUsage.Body.String())
	}

	recComp := httptest.NewRecorder()
	reqComp := httptest.NewRequest(http.MethodGet, "/api/v2/compliance/logs", nil)
	gw.httpServer.Handler.ServeHTTP(recComp, reqComp)
	if recComp.Code != http.StatusOK || !strings.Contains(recComp.Body.String(), `"source":"CODEX_LOG"`) || !strings.Contains(recComp.Body.String(), `"agent_paused_children_preserved"`) {
		t.Fatalf("unexpected compliance logs response: %d %s", recComp.Code, recComp.Body.String())
	}

	// Stop gateway
	if err := stopDesktopGateway(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(disc); !os.IsNotExist(err) {
		t.Fatalf("expected runtime.json to be cleaned up after stop, err: %v", err)
	}
}

func TestDiagnosticsReportEndpoint(t *testing.T) {
	root := t.TempDir()
	ws := t.TempDir()
	t.Setenv("ARIA_HOME", root)

	gw, err := startDesktopGateway(root, ws, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer stopDesktopGateway()

	// 1. Report diagnostics for main.go
	payload := `{
		"path": "main.go",
		"diagnostics": [
			{"line": 10, "col": 5, "severity": "error", "message": "undefined: calculate"},
			{"line": 20, "col": 1, "severity": "warning", "message": "unused variable 'x'"}
		]
	}`
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v2/diagnostics/report", strings.NewReader(payload))
	req.Header.Set("Content-Type", "application/json")
	gw.httpServer.Handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"status":"recorded"`) {
		t.Fatalf("unexpected POST diagnostics response: %d %s", rec.Code, rec.Body.String())
	}

	// 2. Query diagnostics via GET
	recGet := httptest.NewRecorder()
	reqGet := httptest.NewRequest(http.MethodGet, "/api/v2/diagnostics/report", nil)
	gw.httpServer.Handler.ServeHTTP(recGet, reqGet)
	if recGet.Code != http.StatusOK {
		t.Fatalf("expected 200 on GET diagnostics, got %d", recGet.Code)
	}
	var res map[string]any
	if err := json.Unmarshal(recGet.Body.Bytes(), &res); err != nil {
		t.Fatal(err)
	}
	if res["total_errors"] != float64(1) || res["total_warnings"] != float64(1) || res["files_count"] != float64(1) {
		t.Fatalf("unexpected diagnostics summary: %+v", res)
	}

	// 3. Runtime status includes diagnostics feedback flag
	recStatus := httptest.NewRecorder()
	reqStatus := httptest.NewRequest(http.MethodGet, "/api/v2/runtime/status", nil)
	gw.httpServer.Handler.ServeHTTP(recStatus, reqStatus)
	if !strings.Contains(recStatus.Body.String(), `"diagnostics_feedback":true`) || !strings.Contains(recStatus.Body.String(), `"active_diagnostics_files":1`) {
		t.Fatalf("runtime status missing diagnostics feedback: %s", recStatus.Body.String())
	}

	// 4. Clear diagnostics by reporting empty array
	recClear := httptest.NewRecorder()
	reqClear := httptest.NewRequest(http.MethodPost, "/api/v2/diagnostics/report", strings.NewReader(`{"path":"main.go","diagnostics":[]}`))
	reqClear.Header.Set("Content-Type", "application/json")
	gw.httpServer.Handler.ServeHTTP(recClear, reqClear)
	if recClear.Code != http.StatusOK {
		t.Fatalf("expected 200 on clear diagnostics, got %d", recClear.Code)
	}

	recGet2 := httptest.NewRecorder()
	reqGet2 := httptest.NewRequest(http.MethodGet, "/api/v2/diagnostics/report", nil)
	gw.httpServer.Handler.ServeHTTP(recGet2, reqGet2)
	var res2 map[string]any
	_ = json.Unmarshal(recGet2.Body.Bytes(), &res2)
	if res2["files_count"] != float64(0) {
		t.Fatalf("expected 0 files after clearing, got %+v", res2)
	}
}

func TestDesktopGatewayAuthComplianceAndSSE(t *testing.T) {
	root := t.TempDir()
	workspace := t.TempDir()
	t.Setenv("ARIA_HOME", root)

	gw, err := startDesktopGateway(root, workspace, 0)
	if err != nil {
		t.Fatal(err)
	}

	if gw.token == "" {
		t.Fatal("expected non-empty gateway token")
	}

	// 1. Cross-origin request without token must be rejected with 401
	recUnauth := httptest.NewRecorder()
	reqUnauth := httptest.NewRequest(http.MethodGet, "/api/v2/runtime/status", nil)
	reqUnauth.Header.Set("Origin", "http://localhost:3000")
	gw.httpServer.Handler.ServeHTTP(recUnauth, reqUnauth)
	if recUnauth.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 for cross-origin without token, got %d", recUnauth.Code)
	}

	// 2. Valid Bearer token succeeds and sets local CORS header
	recAuth := httptest.NewRecorder()
	reqAuth := httptest.NewRequest(http.MethodGet, "/api/v2/runtime/status", nil)
	reqAuth.Header.Set("Origin", "http://localhost:3000")
	reqAuth.Header.Set("Authorization", "Bearer "+gw.token)
	gw.httpServer.Handler.ServeHTTP(recAuth, reqAuth)
	if recAuth.Code != http.StatusOK {
		t.Fatalf("expected 200 with valid bearer token, got %d", recAuth.Code)
	}
	if recAuth.Header().Get("Access-Control-Allow-Origin") != "http://localhost:3000" {
		t.Fatalf("unexpected CORS origin header: %q", recAuth.Header().Get("Access-Control-Allow-Origin"))
	}

	// 3. SSE /api/v2/events emits gateway.connected and diagnostics.updated
	ctx, cancel := context.WithCancel(context.Background())
	recSSE := httptest.NewRecorder()
	reqSSE := httptest.NewRequest(http.MethodGet, "/api/v2/events?token="+gw.token, nil).WithContext(ctx)
	done := make(chan struct{})
	go func() {
		gw.httpServer.Handler.ServeHTTP(recSSE, reqSSE)
		close(done)
	}()
	time.Sleep(30 * time.Millisecond)
	gw.PublishEvent("diagnostics.updated", map[string]any{"path": "main.go", "count": 2})
	time.Sleep(30 * time.Millisecond)
	cancel()
	<-done

	sseBody := recSSE.Body.String()
	if !strings.Contains(sseBody, "event: gateway.connected") || !strings.Contains(sseBody, "event: diagnostics.updated") {
		t.Fatalf("expected SSE events in body, got %q", sseBody)
	}

	// 4. Record compliance event, stop gateway, restart, verify persisted logs load back
	gw.mu.Lock()
	gw.recordComplianceLocked("agent.lifecycle", "pause_agent", "", "paused")
	gw.mu.Unlock()
	_ = stopDesktopGateway()

	gw2, err := startDesktopGateway(root, workspace, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer stopDesktopGateway()

	gw2.mu.RLock()
	loadedCount := len(gw2.complianceLogs)
	gw2.mu.RUnlock()
	if loadedCount == 0 {
		t.Fatal("expected persisted compliance logs to reload on gateway restart")
	}
}
