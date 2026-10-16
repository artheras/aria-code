package main

import (
	"runtime"
	"time"
)

// Diagnostics are an allowlisted summary, not a redacted config dump. Never
// serialize conversation values, environment, endpoints, paths or credentials.
// Availability of an installed executable is not proof of parser readiness.
func nativeDiagnosticBundle(messages []message, now time.Time) (string, error) {
	roles := map[string]int{"user": 0, "assistant": 0, "system": 0, "tool": 0, "other": 0}
	attachments := 0
	for _, m := range messages {
		role, _ := m["role"].(string)
		if _, known := roles[role]; !known {
			role = "other"
		}
		roles[role]++
		if messageHasAttachments(m) {
			attachments++
		}
	}
	return exportJSON(map[string]any{
		"schema":               "aria.native_diagnostic_bundle.v1",
		"generated_at":         now.UTC().Format(time.RFC3339),
		"runtime":              map[string]string{"implementation": "go", "go_version": runtime.Version(), "os": runtime.GOOS, "arch": runtime.GOARCH},
		"conversation_summary": map[string]any{"message_count": len(messages), "roles": roles, "messages_with_attachment_fields": attachments},
		"document_support":     map[string]string{"utf8": "supported", "docx": "body_and_table_text", "pptx": "slide_text", "pdf": "requires_poppler_and_execution_permission", "ocr": "requires_tesseract_and_selected_language_data", "vision": "opt_in_PNG_JPEG_PDF_requires_vision_model", "restricted_conversion": "macOS_Seatbelt_only_other_platforms_fail_closed"},
		"privacy":              "No message content, session IDs, file paths, configuration, environment, credentials or network probes included.",
		"not_collected":        []string{"provider_health", "runtime_trace", "mcp_health", "artifact_validation"},
	})
}
