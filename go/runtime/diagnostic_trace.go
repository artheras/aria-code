package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"time"
)

type diagnosticEvent struct {
	Status  string `json:"status,omitempty"`
	Time    string `json:"time"`
	Kind    string `json:"kind"`
	Tool    string `json:"tool,omitempty"`
	Success *bool  `json:"success,omitempty"`
}

func safeDiagnosticEvent(kind string, fields map[string]any) (diagnosticEvent, bool) {
	switch kind {
	case "turn.started", "turn.completed", "tool.started", "tool.completed", "approval.requested", "input.requested", "dialog.closed", "protocol.error", "session.closed":
	default:
		return diagnosticEvent{}, false
	}
	event := diagnosticEvent{Time: time.Now().UTC().Format(time.RFC3339Nano), Kind: kind}
	if status, ok := fields["status"].(string); ok && (status == "ok" || status == "failed" || status == "cancelled") {
		event.Status = status
	}
	if success, ok := fields["success"].(bool); ok {
		event.Success = &success
	}
	tool, _ := fields["tool"].(string)
	for _, group := range nativeTools() {
		for _, definition := range group.FunctionDeclarations {
			if definition.Name == tool {
				event.Tool = tool
			}
		}
	}
	return event, true
}

func (s *server) diagnosticTracePath() string {
	if !validID.MatchString(s.snap.ID) || s.opts.root == "" {
		return ""
	}
	return filepath.Join(s.opts.root, "diagnostics", s.snap.ID+".json")
}

// Keep only typed, allowlisted metadata. No prompts, errors, command arguments,
// provider URLs, image bytes or user-assigned turn IDs ever reach this file.
func (s *server) recordDiagnostic(kind string, fields map[string]any) {
	event, ok := safeDiagnosticEvent(kind, fields)
	if !ok {
		return
	}
	s.traceMu.Lock()
	defer s.traceMu.Unlock()
	path := s.diagnosticTracePath()
	if s.tracePath != path {
		s.tracePath = path
		s.trace = nil
		s.traceDropped = 0
		s.traceWriteFailed = false
		if info, err := os.Stat(path); err == nil && info.Size() <= 256<<10 {
			if data, err := os.ReadFile(path); err == nil {
				var previous []diagnosticEvent
				if json.Unmarshal(data, &previous) == nil && len(previous) <= 512 {
					for _, e := range previous {
						fields := map[string]any{"tool": e.Tool, "status": e.Status}
						if e.Success != nil {
							fields["success"] = *e.Success
						}
						if clean, ok := safeDiagnosticEvent(e.Kind, fields); ok {
							if when, err := time.Parse(time.RFC3339Nano, e.Time); err == nil {
								clean.Time = when.UTC().Format(time.RFC3339Nano)
								s.trace = append(s.trace, clean)
							}
						}
					}
				}
			}
		}
	}
	s.trace = append(s.trace, event)
	if len(s.trace) > 512 {
		s.trace = s.trace[len(s.trace)-512:]
		s.traceDropped++
	}
	if path != "" {
		s.traceWriteFailed = atomicJSON(path, s.trace) != nil
	}
}

func (s *server) diagnosticBundle(now time.Time) (string, error) {
	body, err := nativeDiagnosticBundle(s.snap.Messages, now)
	if err != nil {
		return "", err
	}
	var bundle map[string]any
	if err := json.Unmarshal([]byte(body), &bundle); err != nil {
		return "", err
	}
	s.traceMu.Lock()
	defer s.traceMu.Unlock()
	bundle["runtime_trace"] = append([]diagnosticEvent{}, s.trace...)
	bundle["trace_status"] = map[string]any{"capacity": 512, "dropped_this_process": s.traceDropped, "persist_failed": s.traceWriteFailed, "content": "allowlisted lifecycle metadata only"}
	bundle["not_collected"] = []string{"provider_health_probe", "mcp_health_probe", "raw_model_reasoning", "raw_tool_output"}
	return exportJSON(bundle)
}
