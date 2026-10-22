package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
)

func nativeCommands() []string {
	return []string{"/help", "/model", "/models", "/config", "/health", "/sessions", "/export", "/import", "/new", "/resume", "/permissions", "/undo", "/rewind", "/changes", "/apply-change", "/reject-change"}
}

// commandInfo describes each command for the "/" list: [English, Chinese].
var commandInfo = map[string][2]string{
	"/help":          {"Show commands and shortcuts", "查看命令和快捷键"},
	"/model":         {"Choose a provider and model", "选择服务商和模型"},
	"/models":        {"List models without generating", "列出可用模型（不产生调用）"},
	"/config":        {"Show or change settings", "查看或修改设置"},
	"/health":        {"Check the model connection", "检查模型连接"},
	"/sessions":      {"List saved sessions", "列出已保存的会话"},
	"/export":        {"Export this conversation or diagnostics", "导出当前会话或诊断包"},
	"/import":        {"Start a session from exported JSON", "从导出的 JSON 新建会话"},
	"/new":           {"Start a new session", "开始新会话"},
	"/resume":        {"Resume a saved session", "恢复已保存的会话"},
	"/permissions":   {"Show what Aria may read and write", "查看可读写的范围"},
	"/undo":          {"Undo the last exchange", "撤销上一轮对话"},
	"/rewind":        {"Rewind to an earlier point", "回退到更早的位置"},
	"/changes":       {"Review pending file edits", "查看待确认的文件修改"},
	"/apply-change":  {"Apply a pending edit by ID", "按 ID 应用待确认修改"},
	"/reject-change": {"Discard a pending edit by ID", "按 ID 丢弃待确认修改"},
}

// commandInfoList is nativeCommands in order, each with its description in lang.
func commandInfoList(lang string) []map[string]string {
	index := 0
	if strings.HasPrefix(strings.ToLower(lang), "zh") {
		index = 1
	}
	out := make([]map[string]string, 0, len(commandInfo))
	for _, name := range nativeCommands() {
		out = append(out, map[string]string{"name": name, "description": commandInfo[name][index]})
	}
	return out
}

func (s *server) command(ctx context.Context, turn, text string) (string, error) {
	fields := strings.Fields(text)
	if len(fields) == 0 {
		return "", nil
	}
	switch fields[0] {
	case "/import":
		return s.importCommand(ctx, turn, strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(text), "/import")))
	case "/":
		return s.command(ctx, turn, "/help")
	case "/models":
		return s.modelsCommand(ctx, fields[1:])
	case "/export":
		return s.exportCommand(ctx, turn, fields[1:])
	case "/changes", "/apply-change", "/reject-change":
		return s.stageCommand(ctx, turn, fields[0], fields[1:])
	case "/undo":
		if len(fields) != 1 {
			return "", errors.New("usage: /undo")
		}
		return s.undoConversation()
	case "/rewind":
		return s.rewindCommand(ctx, turn, fields[1:])
	case "/help":
		extraHelp := "\n/import <path> — create a new session from JSON\n/export session [path] — complete session including file snapshots\n/export bundle [path] — private diagnostics and lifecycle trace (no conversation or config)\n@ references extract DOCX/PPTX text and PDF text/OCR.\n/config set document_ocr=true — OCR empty PDF pages (Tesseract language data required)\n/config set ocr_language=eng+chi_sim+chi_tra\n/config set document_vision=true — send image/PDF-page evidence to your selected vision-capable provider; off by default\n"
		return "Aria native interface\n\nEnter sends · Ctrl+J / Alt+Enter adds a line\nEsc cancels · Ctrl+O shows operation details · F1 opens help\nTab completes /commands and @file references\n\n/model — interactive provider/model selection\n/models [google|gcp|ollama] — discover models without generation\n/model <provider> <model> — switch explicitly\n@file:path or @\"path with spaces\" — read a text file into this turn\n/config set gcp_project=PROJECT · gcp_location=REGION · ollama_base_url=URL\n/export <format> [path] — export saved conversation\n/changes · /apply-change ID · /reject-change ID — review edits\n\n" + strings.Join(nativeCommands(), " · ") + extraHelp, nil
	case "/sessions":
		sessions, err := listSessions(s.opts.root, 20)
		if err != nil {
			return "", err
		}
		body, err := json.MarshalIndent(sessions, "", "  ")
		return string(body), err
	case "/permissions":
		body, err := json.MarshalIndent(map[string]any{"read_roots": s.scope.readRoots, "write_roots": s.scope.writeRoots, "session_grants": s.grants, "skip_default_prompts": s.opts.skipPermissions}, "", "  ")
		return string(body), err
	case "/new", "/resume":
		return "Use the frontend session command to start or resume a session.", nil
	case "/health":
		if p, ok := s.provider.(*openAIProvider); ok {
			if p.declared != nil {
				names, err := declaredModels(ctx, p.declared)
				if err != nil {
					return "", err
				}
				return fmt.Sprintf("%s reachable: %d models. Current model: %s. No generation performed.", p.declared.ID, len(names), p.model), nil
			}
			if _, _, err := p.connection(ctx); err != nil {
				return "", err
			}
			return "Google Cloud Chat Completions configured. A generation request is required to verify model availability.", nil
		}
		p, ok := s.provider.(*googleProvider)
		if !ok {
			return "Provider test adapter configured.", nil
		}
		if _, err := p.getClient(ctx); err != nil {
			return "", err
		}
		return "Google credentials configured. Network and model availability are verified by a model request; no generation was performed by this check.", nil
	case "/model":
		if len(fields) == 1 && !s.opts.noninteractive {
			return s.modelPicker(ctx, turn)
		}
		if len(fields) == 3 {
			return s.selectModel(fields[1], fields[2])
		}
		name := providerName(s.cfg)
		if len(fields) == 1 {
			if name == "google" {
				text := fmt.Sprintf("google / %s\n/models google: list cloud models\n/models ollama: list local models\n/model <provider> <model>: switch provider and model\n/model <model>: switch within current provider", s.cfg.text("model", "gemini-3.8-flash"))
				if tuned := tunedModelNames(s.cfg); len(tuned) > 0 {
					text += "\nTuned models: " + strings.Join(tuned, ", ")
				}
				return text, nil
			}
			return fmt.Sprintf("%s / %s\n/models %s: list models\n/model <provider> <model>: switch provider and model", name, s.cfg.text("model", ""), name), nil
		}
		if len(fields) != 2 {
			return "", errors.New("usage: /model <model name>")
		}
		model := fields[1]
		if name == "google" {
			model = strings.TrimPrefix(model, "google/")
			_, alias := mappingHas(s.cfg["tuned_models"], model)
			if !strings.HasPrefix(model, "gemini-") && !alias && !tunedEndpointName.MatchString(model) {
				return "", errors.New("unknown Google model name; use a gemini- model, a name from tuned_models or projects/…/endpoints/…")
			}
		}
		next := config{}
		for key, value := range s.cfg {
			next[key] = value
		}
		next["model"] = model
		provider, err := modelProviderFor(next)
		if err != nil {
			return "", err
		}
		if err := s.saveSetting("model", model); err != nil {
			return "", err
		}
		s.cfg["model"] = model
		s.provider = provider
		return "Model: " + model, nil
	case "/config":
		if len(fields) == 1 {
			autoLevel, ok := pyInt(s.cfg["auto_approve_level"])
			if !ok {
				autoLevel = 1
			}
			return fmt.Sprintf("ui_lang=%s\nmodel=%s\nui_theme=%s\napproval_mode=%s\nauto_approve_level=%d\nreview_gate=%t\ndelivery_report=%t",
				s.cfg.text("ui_lang", "en"), s.cfg.text("model", "gemini-3.8-flash"), s.cfg.text("ui_theme", "dark"),
				s.cfg.text("approval_mode", "manual"), autoLevel, s.cfg.boolean("review_gate", false), s.cfg.boolean("delivery_report", true)), nil
		}
		if len(fields) != 3 || fields[1] != "set" {
			return "", errors.New("usage: /config set key=value")
		}
		key, value, ok := strings.Cut(fields[2], "=")
		if !ok {
			return "", errors.New("setting requires key=value")
		}
		var stored any = value
		switch key {
		case "gcp_project", "gcp_location":
			if !validID.MatchString(value) {
				return "", errors.New("project/region must be a nonempty identifier")
			}
		case "ollama_base_url":
			if _, err := declaredProviderFor(config{"local_provider": "ollama", "ollama_base_url": value}); err != nil {
				return "", err
			}
		case "ui_lang":
			if value != "zh" && value != "en" {
				return "", errors.New("ui_lang must be zh or en")
			}
		case "ui_theme":
			if value != "dark" && value != "light" {
				return "", errors.New("ui_theme must be dark or light")
			}
		// The same values and messages as the Python /config (model_cmds.py).
		case "approval_mode":
			if value != "manual" && value != "risk" {
				return "", errors.New("approval_mode must be one of: manual | risk (risk: auto-approve actions at or below auto_approve_level)")
			}
		case "auto_approve_level":
			if value != "0" && value != "1" && value != "2" {
				return "", errors.New("auto_approve_level must be 0, 1 or 2 (L3 and L4 always ask)")
			}
			stored = int(value[0] - '0')
		case "ocr_language":
			if value != "eng" && value != "chi_sim" && value != "chi_tra" && value != "eng+chi_sim+chi_tra" {
				return "", errors.New("ocr_language must be eng, chi_sim, chi_tra or eng+chi_sim+chi_tra")
			}
		case "review_gate", "delivery_report", "document_ocr", "document_vision":
			switch strings.ToLower(value) {
			case "true", "1", "yes", "on":
				stored = true
			case "false", "0", "no", "off":
				stored = false
			default:
				return "", fmt.Errorf("%s must be: true | false", key)
			}
		default:
			return "", errors.New("unknown native setting")
		}
		next := config{}
		for k, v := range s.cfg {
			next[k] = v
		}
		next[key] = stored
		var replacement modelProvider
		if key == "gcp_project" || key == "gcp_location" || key == "ollama_base_url" {
			var err error
			replacement, err = modelProviderFor(next)
			if err != nil {
				return "", err
			}
		}
		if err := s.saveSetting(key, stored); err != nil {
			return "", err
		}
		s.cfg[key] = stored
		if replacement != nil {
			s.provider = replacement
		}
		return fmt.Sprintf("%s=%v", key, stored), nil
	default:
		return "", fmt.Errorf("unknown command: %s; use /help", fields[0])
	}
}
func (s *server) saveSetting(key string, value any) error {
	// Read the latest file so another terminal's unrelated settings survive.
	cfg, err := loadConfig(s.opts.root)
	if err != nil {
		return err
	}
	cfg[key] = value
	return atomicJSON(filepath.Join(s.opts.root, "config.json"), cfg)
}

func mappingHas(value any, key string) (any, bool) {
	m, _ := asMapping(value)
	item, ok := m[key]
	return item, ok
}
