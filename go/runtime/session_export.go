package main

import (
	"bytes"
	"context"
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"
)

// JSON exports retain the complete legacy message array, including attachment
// blocks and unknown fields. Human-readable exports never print base64 images.
func exportText(content any) (string, bool) {
	if text, ok := content.(string); ok {
		return text, false
	}
	if content == nil {
		return "", false
	}
	if blocks, ok := content.([]any); ok {
		var lines []string
		attachments := false
		for _, value := range blocks {
			block, ok := value.(map[string]any)
			if !ok {
				lines = append(lines, "[Unsupported content block; preserved in JSON export]")
				attachments = true
				continue
			}
			kind, _ := block["type"].(string)
			if kind == "text" || kind == "input_text" || kind == "output_text" {
				if text, ok := block["text"].(string); ok {
					lines = append(lines, text)
					continue
				}
			}
			attachments = true
			lines = append(lines, "["+kind+" attachment; preserved in JSON export]")
		}
		return strings.Join(lines, "\n"), attachments
	}
	body, err := json.Marshal(content)
	if err != nil {
		return "[Unsupported content; use JSON export]", true
	}
	return string(body), true
}

func exportJSON(value any) (string, error) {
	var body bytes.Buffer
	encoder := json.NewEncoder(&body)
	encoder.SetEscapeHTML(false)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(value); err != nil {
		return "", err
	}
	return strings.TrimSuffix(body.String(), "\n"), nil
}

func sessionExport(format string, messages []message, now time.Time) (body, extension, prefix string, err error) {
	format = strings.ToLower(strings.TrimSpace(format))
	if format == "" {
		format = "json"
	}
	extension, prefix = format, "aria_code_chat"
	switch format {
	case "json":
		if messages == nil {
			messages = []message{}
		}
		body, err = exportJSON(messages)
	case "csv":
		var buffer bytes.Buffer
		writer := csv.NewWriter(&buffer)
		err = writer.Write([]string{"role", "content"})
		for _, m := range messages {
			text, _ := exportText(m["content"])
			role, _ := m["role"].(string)
			if role == "" {
				role = "user"
			}
			if err == nil {
				err = writer.Write([]string{role, strings.ReplaceAll(text, "\n", " ")})
			}
		}
		writer.Flush()
		if err == nil {
			err = writer.Error()
		}
		body = strings.TrimSuffix(buffer.String(), "\n")
	case "md":
		lines := []string{"# Aria Code Chat Export — " + now.Format("2006-01-02 15:04") + "\n"}
		for _, m := range messages {
			label := "**Aria:**"
			if m["role"] == "user" {
				label = "**You:**"
			}
			text, _ := exportText(m["content"])
			lines = append(lines, label+"\n"+text+"\n")
		}
		body = strings.Join(lines, "\n")
	case "sft":
		extension, prefix = "json", "aria_sft"
		pairs := []map[string]string{}
		for i := 0; i+1 < len(messages); i++ {
			if messages[i]["role"] != "user" || messages[i+1]["role"] != "assistant" {
				continue
			}
			user, userMedia := exportText(messages[i]["content"])
			answer, answerMedia := exportText(messages[i+1]["content"])
			user, answer = strings.TrimSpace(user), strings.TrimSpace(answer)
			// Text-only SFT cannot represent attachment-dependent examples.
			if !userMedia && !answerMedia && !messageHasAttachments(messages[i]) && !messageHasAttachments(messages[i+1]) && utf8.RuneCountInString(user) > 10 && utf8.RuneCountInString(answer) > 20 && !strings.HasPrefix(user, "Tool results:") {
				pairs = append(pairs, map[string]string{"instruction": user, "input": "", "output": answer, "source": "aria_cli_export", "timestamp": now.Format("2006-01-02")})
			}
			i++
		}
		if len(pairs) == 0 {
			err = errors.New("No user→assistant pairs to export")
			return
		}
		body, err = exportJSON(pairs)
	case "ipynb":
		cells := []map[string]any{{"cell_type": "markdown", "metadata": map[string]any{}, "source": []string{"# Aria Code Chat Export — " + now.Format("2006-01-02 15:04") + "\n\n", "*(This notebook contains the conversation history. Math formulas and LaTeX will render natively here.)*"}}}
		for _, m := range messages {
			if m["role"] != "user" && m["role"] != "assistant" {
				continue
			}
			label := "**Aria:**\n\n"
			if m["role"] == "user" {
				label = "**You:**\n\n"
			}
			text, _ := exportText(m["content"])
			cells = append(cells, map[string]any{"cell_type": "markdown", "metadata": map[string]any{}, "source": []string{label + text}})
		}
		body, err = exportJSON(map[string]any{"cells": cells, "metadata": map[string]any{"kernelspec": map[string]string{"display_name": "Python 3", "language": "python", "name": "python3"}, "language_info": map[string]string{"name": "python"}}, "nbformat": 4, "nbformat_minor": 4})
	default:
		err = errors.New("native export formats: json, csv, md, sft, ipynb; diagnostic bundle remains available in the Python interface")
	}
	if err == nil && len(body) > 4<<20 {
		err = errors.New("export exceeds the native file tool's 4 MiB limit")
	}
	return
}

func messageHasAttachments(m message) bool {
	value := m["attachments"]
	if value == nil {
		return false
	}
	if items, ok := value.([]any); ok {
		return len(items) > 0
	}
	return true
}

func (s *server) exportCommand(ctx context.Context, turn string, args []string) (string, error) {
	format := "json"
	if len(args) > 0 {
		format = strings.ToLower(strings.TrimSpace(args[0]))
	}
	if len(s.snap.Messages) == 0 && format != "bundle" {
		return "Nothing to export", nil
	}
	now := time.Now()
	body, ext, prefix, err := sessionExport(format, s.snap.Messages, now)
	if format == "bundle" {
		body, err = s.diagnosticBundle(now)
		ext, prefix = "json", "aria_native_diagnostics"
	}
	if format == "session" {
		// Full round-trip export includes provider snapshots, such as @ files.
		copy := s.snap
		copy.ProviderContents, err = json.Marshal(s.contents)
		if err == nil {
			body, err = exportJSON(copy)
		}
		ext, prefix = "json", "aria_session"
		if err == nil && len(body) > 4<<20 {
			err = errors.New("session export exceeds the native file tool's 4 MiB limit")
		}
	}
	if err != nil {
		return "", err
	}
	filename := prefix + "_" + now.Format("20060102_150405") + "." + ext
	if len(args) > 1 {
		filename = strings.Join(args[1:], " ")
	}
	params := map[string]any{"path": filename, "content": body}
	contract := projectContract(s.scope.workspace, "Export this session")
	if contract != nil {
		verdict := contract.Check("write_file", params)
		if !verdict.Allowed {
			return "", errors.New(verdict.toolResult("write_file").Error)
		}
	}
	change, err := propose(s.scope, "write_file", params)
	if err != nil {
		return "", err
	}
	if !change.existed {
		change.mode = 0o600
	}
	allowed, reason, err := s.approve(ctx, "write_file", params, change)
	if err != nil {
		return "", err
	}
	if !allowed {
		return "", fmt.Errorf("export not written: %s", reason)
	}
	if err := s.emit("tool.started", turn, map[string]any{"tool": "write_file", "params": map[string]any{"path": filename, "purpose": "session export"}}); err != nil {
		return "", err
	}
	result := applyRecordedChange(ctx, s.scope, s.opts.root, s.snap.ID, "write_file", change)
	if err := s.emit("tool.completed", turn, map[string]any{"tool": "write_file", "success": result.Success, "error": result.Error, "path": change.path}); err != nil {
		return "", err
	}
	if !result.Success {
		return "", errors.New(result.Error)
	}
	return "Exported to " + change.path, nil
}
