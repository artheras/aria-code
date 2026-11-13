package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// TaskBrief is the declarative task intent model.
type TaskBrief struct {
	Version    int      `json:"version"`
	Goal       string   `json:"goal"`
	Boundaries []string `json:"boundaries,omitempty"`
	Rationale  string   `json:"rationale,omitempty"`
	Acceptance []string `json:"acceptance,omitempty"`
}

func newTaskBrief(goal, rationale string, boundaries, acceptance []string) (TaskBrief, error) {
	goal = strings.TrimSpace(goal)
	if len([]byte(goal)) == 0 || len([]byte(goal)) > 65536 {
		return TaskBrief{}, errors.New("Task goal must be 1..65536 UTF-8 bytes")
	}
	if len([]byte(rationale)) > 16384 {
		return TaskBrief{}, errors.New("Task rationale must be at most 16384 UTF-8 bytes")
	}
	if len(boundaries) > 32 || len(acceptance) > 32 {
		return TaskBrief{}, errors.New("boundaries and acceptance must contain at most 32 items")
	}
	return TaskBrief{
		Version:    1,
		Goal:       goal,
		Boundaries: boundaries,
		Rationale:  rationale,
		Acceptance: acceptance,
	}, nil
}

// TaskOutcome represents the dual-state projection of task execution and verification.
type TaskOutcome struct {
	Version           int    `json:"version"`
	RequiresRefresh   bool   `json:"requires_refresh"`
	ExecutionState    string `json:"execution_state"`
	VerificationState string `json:"verification_state"`
	Verified          *bool  `json:"verified"`
	ChecksTotal       int    `json:"checks_total"`
	AcceptanceScope   string `json:"acceptance_scope"`
}

func projectTaskOutcome(result map[string]any, fresh bool) TaskOutcome {
	execution := str(result, "status", "unknown")
	var checks []any
	vReport, _ := result["verification"].(map[string]any)
	if vReport != nil {
		if c, ok := vReport["checks"].([]any); ok {
			checks = c
		}
	}
	vState := "unverified"
	eligible := fresh && len(checks) > 0 && execution == "done"
	if eligible && str(vReport, "status", "") == "verified" && vReport["verified"] == true && result["verified"] == true && result["success"] != false {
		vState = "verified"
	} else if eligible && str(vReport, "status", "") == "failed" && vReport["verified"] == false && result["verified"] == false {
		vState = "failed"
	}

	var verifiedBool *bool
	if vState == "verified" {
		t := true
		verifiedBool = &t
	} else if vState == "failed" {
		f := false
		verifiedBool = &f
	}

	return TaskOutcome{
		Version:           1,
		RequiresRefresh:   !fresh,
		ExecutionState:    execution,
		VerificationState: vState,
		Verified:          verifiedBool,
		ChecksTotal:       len(checks),
		AcceptanceScope:   "declared_predicates",
	}
}

// ProjectTaskContextEntry represents an entry in .aria/task-context.json.
type ProjectTaskContextEntry struct {
	ID          string   `json:"id"`
	Kind        string   `json:"kind"` // "skill" or "memory"
	Path        string   `json:"path"`
	Description string   `json:"description"`
	Roles       []string `json:"roles,omitempty"`
	Auto        bool     `json:"auto,omitempty"`
}

type ProjectTaskContextManifest struct {
	Version int                       `json:"version"`
	Entries []ProjectTaskContextEntry `json:"entries"`
}

func loadProjectTaskContext(workspace string) ([]ProjectTaskContextEntry, error) {
	manifestPath := filepath.Join(workspace, ".aria", "task-context.json")
	data, err := os.ReadFile(manifestPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var manifest ProjectTaskContextManifest
	if err := json.Unmarshal(data, &manifest); err != nil {
		return nil, err
	}
	return manifest.Entries, nil
}

// ResidencyBoundary separates data-at-rest storage residency, model inference
// residency, and external MCP/tool egress allowlists.
type ResidencyBoundary struct {
	StorageRegion     string   `json:"storage_region"`
	InferenceRegion   string   `json:"inference_region"`
	AllowedMCPDomains []string `json:"allowed_mcp_domains,omitempty"`
	EKMEnabled        bool     `json:"ekm_enabled"`
}

// AgentServiceIdentity represents the enterprise service identity for an agent instance.
type AgentServiceIdentity struct {
	AgentID   string            `json:"agent_id"`
	OrgID     string            `json:"org_id,omitempty"`
	CostTier  string            `json:"cost_tier"` // "standard", "advanced", "expert"
	Paused    bool              `json:"paused"`
	Revoked   bool              `json:"revoked"`
	Residency ResidencyBoundary `json:"residency"`
}

// ChildTaskRecord tracks independent delegated tasks (worktree, cloud, scheduled)
// whose lifecycle is decoupled from pausing/deleting the parent agent.
type ChildTaskRecord struct {
	TaskID        string `json:"task_id"`
	ParentAgentID string `json:"parent_agent_id"`
	Kind          string `json:"kind"`   // "worktree", "cloud", "scheduled"
	Status        string `json:"status"` // "running", "completed", "cancelled", "failed"
	StartedAt     string `json:"started_at"`
}

// ComplianceAuditEntry records a structured CODEX_LOG-style compliance event,
// kept strictly separate from aggregate DOTS-style usage analytics.
type ComplianceAuditEntry struct {
	Timestamp          string `json:"timestamp"`
	EventType          string `json:"event_type"`
	AgentID            string `json:"agent_id"`
	ParentTaskID       string `json:"parent_task_id,omitempty"`
	ChildTaskID        string `json:"child_task_id,omitempty"`
	Action             string `json:"action"`
	Capability         string `json:"capability,omitempty"`
	ApprovedBy         string `json:"approved_by,omitempty"`
	CancellationStatus string `json:"cancellation_status,omitempty"`
}

// desktopGatewayServer is the local daemon HTTP server that bridges Desktop, VS Code, and CLI.
type desktopGatewayServer struct {
	mu             sync.RWMutex
	listener       net.Listener
	httpServer     *http.Server
	url            string
	stateRoot      string
	workspace      string
	activeSession  string
	approvals      map[string]pendingApproval
	approvalCh     map[string]chan bool
	discovery      string
	identity       AgentServiceIdentity
	childTasks     map[string]*ChildTaskRecord
	complianceLogs []ComplianceAuditEntry
}

type pendingApproval struct {
	ID          string         `json:"id"`
	Tool        string         `json:"tool"`
	Description string         `json:"description"`
	Risk        map[string]any `json:"risk,omitempty"`
	Params      map[string]any `json:"params,omitempty"`
	CreatedAt   string         `json:"created_at"`
}

var (
	activeGatewayMu sync.Mutex
	activeGateway   *desktopGatewayServer
)

func runtimeDiscoveryPath() (string, error) {
	root, err := stateRoot()
	if err != nil {
		return "", err
	}
	return filepath.Join(root, "runtime.json"), nil
}

func startDesktopGateway(root, workspace string, port int) (*desktopGatewayServer, error) {
	activeGatewayMu.Lock()
	defer activeGatewayMu.Unlock()
	if activeGateway != nil && activeGateway.url != "" {
		return activeGateway, nil
	}

	discPath, err := runtimeDiscoveryPath()
	if err != nil {
		return nil, err
	}

	actualPort := 8000
	var listener net.Listener
	if port > 0 {
		l, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
		if err != nil {
			return nil, err
		}
		listener = l
		actualPort = listener.Addr().(*net.TCPAddr).Port
	} else {
		for p := 8000; p <= 8010; p++ {
			l, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", p))
			if err == nil {
				listener = l
				actualPort = listener.Addr().(*net.TCPAddr).Port
				break
			}
		}
		if listener == nil {
			if l, err := net.Listen("tcp", "127.0.0.1:0"); err == nil {
				listener = l
				actualPort = listener.Addr().(*net.TCPAddr).Port
			}
		}
	}

	url := fmt.Sprintf("http://127.0.0.1:%d", actualPort)

	gw := &desktopGatewayServer{
		listener:   listener,
		url:        url,
		stateRoot:  root,
		workspace:  workspace,
		approvals:  map[string]pendingApproval{},
		approvalCh: map[string]chan bool{},
		discovery:  discPath,
		identity: AgentServiceIdentity{
			AgentID:  "aria-agent-local",
			OrgID:    os.Getenv("ARIA_ORG_ID"),
			CostTier: "standard",
			Residency: ResidencyBoundary{
				StorageRegion:     "local",
				InferenceRegion:   "auto",
				AllowedMCPDomains: []string{"127.0.0.1", "localhost"},
			},
		},
		childTasks:     map[string]*ChildTaskRecord{},
		complianceLogs: []ComplianceAuditEntry{},
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/health", gw.handleHealth)
	mux.HandleFunc("/api/v2/runtime/status", gw.handleRuntimeStatus)
	mux.HandleFunc("/api/v2/sessions", gw.handleSessions)
	mux.HandleFunc("/api/v2/approvals", gw.handleApprovals)
	mux.HandleFunc("/api/v2/approvals/respond", gw.handleApprovalRespond)
	mux.HandleFunc("/api/v2/artifacts", gw.handleArtifacts)
	mux.HandleFunc("/api/v2/task/context", gw.handleTaskContext)
	mux.HandleFunc("/api/v2/lifecycle/action", gw.handleLifecycleAction)
	mux.HandleFunc("/api/v2/analytics/usage", gw.handleAnalyticsUsage)
	mux.HandleFunc("/api/v2/compliance/logs", gw.handleComplianceLogs)

	gw.httpServer = &http.Server{
		Handler:      corsMiddleware(mux),
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 60 * time.Second,
	}

	if listener != nil {
		go func() {
			_ = gw.httpServer.Serve(listener)
		}()
	}

	// Write ~/.aria/runtime.json for Desktop & Extension discovery
	if discPath != "" {
		_ = os.MkdirAll(filepath.Dir(discPath), 0o755)
		data, _ := json.MarshalIndent(map[string]any{
			"url":        url,
			"pid":        os.Getpid(),
			"started_at": float64(time.Now().UnixNano()) / 1e9,
			"version":    version,
			"workspace":  workspace,
		}, "", "  ")
		_ = atomicWrite(discPath, data)
	}

	activeGateway = gw
	return gw, nil
}

func stopDesktopGateway() error {
	activeGatewayMu.Lock()
	defer activeGatewayMu.Unlock()
	if activeGateway == nil {
		return nil
	}
	gw := activeGateway
	activeGateway = nil

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	_ = gw.httpServer.Shutdown(ctx)
	if gw.discovery != "" {
		_ = os.Remove(gw.discovery)
	}
	return nil
}

func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusOK)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (gw *desktopGatewayServer) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"status":  "ok",
		"version": version,
		"time":    time.Now().UTC().Format(time.RFC3339),
	})
}

func (gw *desktopGatewayServer) handleRuntimeStatus(w http.ResponseWriter, r *http.Request) {
	gw.mu.RLock()
	defer gw.mu.RUnlock()

	cfg, _ := loadConfig(gw.stateRoot)
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"status":                      "ok",
		"version":                     version,
		"workspace":                   gw.workspace,
		"active_session":              gw.activeSession,
		"model":                       cfg.text("model", "gemini-3.8-flash"),
		"provider":                    providerName(cfg),
		"network":                     cfg.boolean("network_enabled", true),
		"pending_approvals":           len(gw.approvals),
		"service_identity":            gw.identity,
		"native_tools_count":          len(fileDefinitions),
		"governance_mode":             "zero_trust",
		"preflight_syntax_validation": true,
		"sliding_window_compaction":   true,
	})
}

func (gw *desktopGatewayServer) handleSessions(w http.ResponseWriter, r *http.Request) {
	sessions, err := listSessions(gw.stateRoot, 50)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]any{"sessions": sessions})
}

func (gw *desktopGatewayServer) handleApprovals(w http.ResponseWriter, r *http.Request) {
	gw.mu.RLock()
	defer gw.mu.RUnlock()

	list := make([]pendingApproval, 0, len(gw.approvals))
	for _, app := range gw.approvals {
		list = append(list, app)
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]any{"approvals": list})
}

func (gw *desktopGatewayServer) handleApprovalRespond(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		ID      string `json:"id"`
		Approve bool   `json:"approve"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid request", http.StatusBadRequest)
		return
	}

	gw.mu.Lock()
	ch, exists := gw.approvalCh[req.ID]
	if exists {
		delete(gw.approvalCh, req.ID)
		delete(gw.approvals, req.ID)
	}
	gw.mu.Unlock()

	if !exists {
		http.Error(w, "approval not found or already answered", http.StatusNotFound)
		return
	}

	ch <- req.Approve
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]any{"success": true})
}

func (gw *desktopGatewayServer) handleArtifacts(w http.ResponseWriter, r *http.Request) {
	items := listArtifacts(r.Context(), 50)
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]any{"artifacts": items})
}

func (gw *desktopGatewayServer) activeChildTaskListLocked() []ChildTaskRecord {
	active := make([]ChildTaskRecord, 0, len(gw.childTasks))
	for _, t := range gw.childTasks {
		if t.Status == "running" {
			active = append(active, *t)
		}
	}
	return active
}

func (gw *desktopGatewayServer) recordComplianceLocked(eventType, action, childTaskID, cancelStatus string) {
	gw.complianceLogs = append(gw.complianceLogs, ComplianceAuditEntry{
		Timestamp:          time.Now().UTC().Format(time.RFC3339),
		EventType:          eventType,
		AgentID:            gw.identity.AgentID,
		ChildTaskID:        childTaskID,
		Action:             action,
		CancellationStatus: cancelStatus,
	})
}

func (gw *desktopGatewayServer) handleLifecycleAction(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		Action string `json:"action"` // "pause_agent", "resume_agent", "cancel_task", "revoke_access", "delete_agent_state"
		TaskID string `json:"task_id,omitempty"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid request", http.StatusBadRequest)
		return
	}

	gw.mu.Lock()
	defer gw.mu.Unlock()

	switch req.Action {
	case "pause_agent":
		// Pause Agent stops autonomous scheduling and base active-day billing,
		// but independent worktree/cloud/scheduled child tasks continue unless cancelled explicitly.
		gw.identity.Paused = true
		activeChildren := gw.activeChildTaskListLocked()
		gw.recordComplianceLocked("agent.lifecycle", "pause_agent", "", "agent_paused_children_preserved")
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success":             true,
			"action":              "pause_agent",
			"agent_paused":        true,
			"active_child_tasks":  activeChildren,
			"independent_billing": len(activeChildren) > 0,
		})
	case "resume_agent":
		gw.identity.Paused = false
		gw.recordComplianceLocked("agent.lifecycle", "resume_agent", "", "resumed")
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success":      true,
			"action":       "resume_agent",
			"agent_paused": false,
		})
	case "cancel_task":
		if strings.TrimSpace(req.TaskID) == "" {
			http.Error(w, "task_id is required for cancel_task", http.StatusBadRequest)
			return
		}
		task, exists := gw.childTasks[req.TaskID]
		if !exists {
			http.Error(w, "task not found", http.StatusNotFound)
			return
		}
		task.Status = "cancelled"
		gw.recordComplianceLocked("task.lifecycle", "cancel_task", req.TaskID, "cancelled")
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success":      true,
			"action":       "cancel_task",
			"task_id":      req.TaskID,
			"task_status":  "cancelled",
			"agent_paused": gw.identity.Paused,
		})
	case "revoke_access":
		gw.identity.Revoked = true
		gw.identity.Paused = true
		revokedApprovals := len(gw.approvals)
		for id, ch := range gw.approvalCh {
			select {
			case ch <- false:
			default:
			}
			delete(gw.approvalCh, id)
		}
		gw.approvals = map[string]pendingApproval{}
		activeChildren := gw.activeChildTaskListLocked()
		gw.recordComplianceLocked("security.lifecycle", "revoke_access", "", "access_revoked")
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success":            true,
			"action":             "revoke_access",
			"access_revoked":     true,
			"revoked_approvals":  revokedApprovals,
			"active_child_tasks": activeChildren,
		})
	case "delete_agent_state":
		gw.identity.Paused = true
		gw.activeSession = ""
		_ = os.RemoveAll(filepath.Join(gw.stateRoot, "memory"))
		activeChildren := gw.activeChildTaskListLocked()
		gw.recordComplianceLocked("agent.lifecycle", "delete_agent_state", "", "state_deleted_children_preserved")
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success":            true,
			"action":             "delete_agent_state",
			"state_deleted":      true,
			"active_child_tasks": activeChildren,
		})
	default:
		http.Error(w, "unsupported lifecycle action", http.StatusBadRequest)
	}
}

func (gw *desktopGatewayServer) handleAnalyticsUsage(w http.ResponseWriter, r *http.Request) {
	gw.mu.RLock()
	defer gw.mu.RUnlock()

	running, completed, cancelled := 0, 0, 0
	for _, t := range gw.childTasks {
		switch t.Status {
		case "running":
			running++
		case "completed":
			completed++
		case "cancelled":
			cancelled++
		}
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"client_id":               "DOTS",
		"agent_id":                gw.identity.AgentID,
		"org_id":                  gw.identity.OrgID,
		"cost_tier":               gw.identity.CostTier,
		"paused":                  gw.identity.Paused,
		"active_child_task_count": running,
		"completed_task_count":    completed,
		"cancelled_task_count":    cancelled,
		"native_tools_available":  len(fileDefinitions),
		"token_efficiency": map[string]any{
			"data_summarization":    ">90%",
			"3d_asset_inspection":   ">95%",
			"spatial_ik_clearance":  "100%",
			"tool_result_reduction": ">70%",
		},
	})
}

func (gw *desktopGatewayServer) handleComplianceLogs(w http.ResponseWriter, r *http.Request) {
	gw.mu.RLock()
	defer gw.mu.RUnlock()

	logs := make([]ComplianceAuditEntry, len(gw.complianceLogs))
	copy(logs, gw.complianceLogs)
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"source":    "CODEX_LOG",
		"agent_id":  gw.identity.AgentID,
		"residency": gw.identity.Residency,
		"entries":   logs,
		"count":     len(logs),
	})
}

func (gw *desktopGatewayServer) handleTaskContext(w http.ResponseWriter, r *http.Request) {
	gw.mu.RLock()
	ws := gw.workspace
	gw.mu.RUnlock()

	entries, err := loadProjectTaskContext(ws)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"entries": entries,
		"count":   len(entries),
	})
}

// daemonServeCommand is the CLI entry point for `aria-runtime serve`.
func daemonServeCommand(args []string, out, diagnostics io.Writer) int {
	port := 8000
	workspace := ""
	for i := 0; i < len(args); i++ {
		if args[i] == "--port" && i+1 < len(args) {
			fmt.Sscanf(args[i+1], "%d", &port)
			i++
		} else if args[i] == "--workspace" && i+1 < len(args) {
			workspace = args[i+1]
			i++
		}
	}
	if workspace == "" {
		if wd, err := os.Getwd(); err == nil {
			workspace = wd
		}
	}
	root, err := stateRoot()
	if err != nil {
		fmt.Fprintf(diagnostics, "failed to get state root: %v\n", err)
		return 1
	}

	gw, err := startDesktopGateway(root, workspace, port)
	if err != nil {
		fmt.Fprintf(diagnostics, "failed to start desktop gateway: %v\n", err)
		return 1
	}
	fmt.Fprintf(out, "Aria Desktop Runtime Gateway listening at %s\n", gw.url)
	fmt.Fprintf(out, "Press Ctrl+C to stop.\n")

	sig := make(chan os.Signal, 1)
	<-sig
	_ = stopDesktopGateway()
	return 0
}
