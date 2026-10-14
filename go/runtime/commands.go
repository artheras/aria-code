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
	return []string{"/help", "/model", "/config", "/health", "/sessions", "/new", "/resume", "/permissions", "/undo", "/rewind", "/changes", "/apply-change", "/reject-change"}
}
func (s *server) command(ctx context.Context, turn, text string) (string, error) {
	fields := strings.Fields(text)
	if len(fields) == 0 {
		return "", nil
	}
	switch fields[0] {
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
		return "Aria native interface\n\nEnter sends · Ctrl+J / Alt+Enter adds a line\nEsc cancels · Ctrl+O shows operation details · F1 opens help\n\n" + strings.Join(nativeCommands(), " · "), nil
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
		name := providerName(s.cfg)
		if len(fields) == 1 {
			if name == "google" {
				text := fmt.Sprintf("google / %s\nUse /model <Google model name>.", s.cfg.text("model", "gemini-3.5-flash"))
				if tuned := tunedModelNames(s.cfg); len(tuned) > 0 {
					text += "\nTuned models: " + strings.Join(tuned, ", ")
				}
				return text, nil
			}
			return fmt.Sprintf("%s / %s\nUse /model <model name served by %s>.", name, s.cfg.text("model", ""), name), nil
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
				s.cfg.text("ui_lang", "en"), s.cfg.text("model", "gemini-3.5-flash"), s.cfg.text("ui_theme", "dark"),
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
		case "review_gate", "delivery_report":
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
		if err := s.saveSetting(key, stored); err != nil {
			return "", err
		}
		s.cfg[key] = stored
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
