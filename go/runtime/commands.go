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
	return []string{"/help", "/model", "/config", "/health", "/sessions", "/export", "/import", "/new", "/resume", "/permissions", "/undo", "/rewind", "/changes", "/apply-change", "/reject-change", "/clear", "/status", "/context", "/tools", "/diff", "/compact", "/btw", "/recap", "/regen", "/history", "/rename", "/fork", "/save", "/load", "/init", "/memory", "/note", "/recall", "/todo", "/project", "/plan", "/apply-plan", "/checkpoint", "/git", "/commit", "/pr", "/gh", "/review", "/test", "/verify", "/fix", "/bug", "/trace", "/quality", "/search", "/read", "/ls", "/write", "/edit", "/code", "/run", "/build", "/task", "/worktree", "/tasks", "/delegate", "/agents", "/workflow", "/wf", "/consult", "/mcp", "/skills", "/vision", "/upload-image", "/providers", "/apikey", "/whoami", "/logout", "/doctor", "/update", "/feedback", "/privacy", "/cost", "/thinking", "/graph", "/impact", "/lsp", "/hooks", "/browser", "/screenshot", "/login", "/finance", "/broker"}
}

// commandInfo describes each command for the "/" list: [English, Chinese].
var commandInfo = map[string][2]string{
	"/help":          {"Show commands and shortcuts", "查看命令和快捷键"},
	"/model":         {"Choose, list, or switch model", "选择、查看或切换模型"},
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
	"/clear":         {"Clear the conversation, keep the session", "清空对话（保留会话）"},
	"/status":        {"Show model, session and permissions", "查看模型、会话和权限"},
	"/context":       {"Show what the model will see next turn", "查看下一轮模型可见的上下文"},
	"/tools":         {"List tools and whether they ask first", "列出工具及其审批方式"},
	"/diff":          {"Show workspace changes (--patch for the patch)", "查看工作区改动（--patch 显示补丁）"},
	"/compact":       {"Summarise the history to free context", "压缩历史以释放上下文"},
	"/btw":           {"Ask a side question that is not kept", "顺便问一句（不计入对话）"},
	"/recap":         {"Recap the conversation so far", "回顾目前的对话"},
	"/regen":         {"Regenerate the last answer", "重新生成上一个回答"},
	"/history":       {"List recent prompts", "列出最近的提问"},
	"/rename":        {"Rename this session", "重命名当前会话"},
	"/fork":          {"Save a copy of this session", "复制当前会话为新会话"},
	"/save":          {"Save the session to a file (/export session)", "保存会话到文件（/export session）"},
	"/load":          {"Load a session file (/import)", "从文件载入会话（/import）"},
	"/init":          {"Write ARIA.md instructions for this project", "为当前项目生成 ARIA.md 说明"},
	"/memory":        {"Show or add standing instructions", "查看或添加长期记忆"},
	"/note":          {"Add a dated note to ARIA.md", "在 ARIA.md 记一条带日期的笔记"},
	"/recall":        {"Search saved sessions", "搜索已保存的会话"},
	"/todo":          {"Keep a task list", "管理待办清单"},
	"/project":       {"Summarise this project", "查看项目概况"},
	"/plan":          {"Plan a task without changing anything", "只读规划任务（不修改文件）"},
	"/apply-plan":    {"Carry out the last plan", "执行上一个计划"},
	"/checkpoint":    {"List or restore checkpoints", "列出或恢复检查点"},
	"/git":           {"Run git (read-only commands run directly)", "运行 git（只读命令直接执行）"},
	"/commit":        {"Commit all changes (message written for you)", "提交全部改动（可自动生成说明）"},
	"/pr":            {"Show or create the branch's pull request", "查看或创建当前分支的 PR"},
	"/gh":            {"Run the GitHub CLI", "运行 GitHub CLI"},
	"/review":        {"Review the change with an independent reviewer", "由独立审查者审查改动"},
	"/test":          {"Run the project's checks", "运行项目检查"},
	"/verify":        {"Run checks, then review the change", "运行检查后审查改动"},
	"/fix":           {"Find and fix a failure", "定位并修复失败"},
	"/bug":           {"Investigate and fix a bug", "排查并修复缺陷"},
	"/trace":         {"Trace an error to its cause (read-only)", "追溯错误根因（只读）"},
	"/quality":       {"Assess the change's quality (read-only)", "评估改动质量（只读）"},
	"/search":        {"Search the code with a regex", "用正则搜索代码"},
	"/read":          {"Show a file with line numbers", "带行号查看文件"},
	"/ls":            {"List a directory", "列出目录"},
	"/write":         {"Write a file (content on the next lines)", "写入文件（内容写在下一行起）"},
	"/edit":          {"Ask for a change to one file", "让 Aria 修改指定文件"},
	"/code":          {"Write code for a description", "按描述编写代码"},
	"/run":           {"Run a shell command (same as !)", "运行 shell 命令（同 !）"},
	"/build":         {"Run the project's build", "运行项目构建"},
	"/task":          {"Run a task in its own git worktree", "在独立 git worktree 中执行任务"},
	"/worktree":      {"Show, diff, apply or discard a task (same as /task)", "查看、对比、应用或丢弃任务（同 /task）"},
	"/tasks":         {"List worktree tasks", "列出 worktree 任务"},
	"/delegate":      {"Hand a task to a fresh-context subagent", "交给全新上下文的子代理执行"},
	"/agents":        {"List project agents (.aria/agents)", "列出项目代理（.aria/agents）"},
	"/workflow":      {"List, show or run .aria/workflows", "列出、查看或运行工作流"},
	"/wf":            {"Same as /workflow", "同 /workflow"},
	"/consult":       {"Ask another model for a second opinion", "请另一个模型给出第二意见"},
	"/mcp":           {"Show MCP servers and tools, or reload them", "查看或重新加载 MCP 服务与工具"},
	"/skills":        {"List, show or use SKILL.md skills", "列出、查看或使用技能"},
	"/vision":        {"Ask about an image", "就图片提问"},
	"/upload-image":  {"Same as /vision", "同 /vision"},
	"/providers":     {"List model providers and whether they are ready", "列出模型服务商及其可用状态"},
	"/apikey":        {"Show where each provider reads its key", "查看各服务商的密钥来源"},
	"/whoami":        {"Show the Arthera account", "查看 Arthera 账户"},
	"/logout":        {"Sign out of Arthera on this machine", "在本机退出 Arthera 账户"},
	"/doctor":        {"Check the installation and settings", "检查安装与设置"},
	"/update":        {"How to update Aria", "如何更新 Aria"},
	"/feedback":      {"Rate the last answer (kept locally)", "评价上一个回答（保存在本地）"},
	"/privacy":       {"Show what leaves this machine", "查看哪些数据会离开本机"},
	"/cost":          {"Show token usage for this session", "查看本会话的 token 用量"},
	"/thinking":      {"Set how much the model thinks", "设置模型思考强度"},
	"/graph":         {"Summarise the project graph", "查看项目依赖图概况"},
	"/impact":        {"Show what changing a file or symbol reaches", "查看修改文件或符号的影响范围"},
	"/lsp":           {"Run a file's language checker", "运行文件的语言检查器"},
	"/hooks":         {"List hooks, or trust the project's", "查看钩子或信任项目钩子"},
	"/browser":       {"Screenshot a web page", "网页截图"},
	"/screenshot":    {"Capture the screen", "截取屏幕"},
	"/login":         {"Sign in to Arthera in the browser", "在浏览器中登录 Arthera"},
	"/finance":       {"Connect and check the financial-service", "连接并检查金融服务"},
	"/broker":        {"Read broker accounts, positions and orders", "查看券商账户、持仓与委托"},
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
	case "/clear":
		if len(fields) != 1 {
			return "", errors.New("usage: /clear")
		}
		return s.clearConversation()
	case "/status":
		return s.statusCommand()
	case "/context":
		return s.contextCommand()
	case "/tools":
		return s.toolsCommand(ctx)
	case "/diff":
		return s.diffCommand(ctx, fields[1:])
	case "/compact":
		return s.compactCommand(ctx, rest(text, fields[0]))
	case "/btw":
		return s.btwCommand(ctx, rest(text, fields[0]))
	case "/recap":
		return s.recapCommand(ctx)
	case "/regen":
		// runTurn turns /regen into the previous prompt before it gets here.
		return "", errors.New("nothing to regenerate")
	case "/history":
		return s.historyCommand(fields[1:])
	case "/rename":
		return s.renameCommand(rest(text, fields[0]))
	case "/fork":
		return s.forkCommand(rest(text, fields[0]))
	case "/save":
		return s.exportCommand(ctx, turn, append([]string{"session"}, fields[1:]...))
	case "/load":
		return s.importCommand(ctx, turn, rest(text, fields[0]))
	case "/memory":
		return s.memoryCommand(rest(text, fields[0]))
	case "/note":
		return s.noteCommand(rest(text, fields[0]))
	case "/recall":
		return s.recallCommand(rest(text, fields[0]))
	case "/todo":
		return s.todoCommand(rest(text, fields[0]))
	case "/project":
		return s.projectCommand(ctx)
	case "/checkpoint":
		return s.checkpointCommand(ctx, turn, fields[1:])
	case "/git":
		return s.gitCommand(ctx, turn, fields[1:])
	case "/commit":
		return s.commitCommand(ctx, turn, rest(text, fields[0]))
	case "/pr":
		return s.prCommand(ctx, turn, fields[1:])
	case "/gh":
		return s.ghCommand(ctx, turn, fields[1:])
	case "/review":
		return s.reviewChange(ctx, fields[1:])
	case "/test":
		return s.testCommand(ctx, turn, fields[1:])
	case "/verify":
		return s.verifyCommand(ctx, turn)
	case "/search":
		return s.searchCommand(ctx, fields[1:])
	case "/read":
		return s.readCommand(ctx, fields[1:])
	case "/ls":
		return s.lsCommand(ctx, fields[1:])
	case "/write":
		return s.writeCommand(ctx, turn, text)
	case "/run":
		command := rest(text, fields[0])
		if command == "" {
			return "", errors.New("usage: /run <command>")
		}
		return s.userCommand(ctx, turn, command)
	case "/build":
		return s.buildCommand(ctx, turn)
	case "/task", "/worktree":
		return s.taskCommand(ctx, turn, text)
	case "/tasks":
		return s.tasksCommand(ctx)
	case "/delegate":
		return s.delegateCommand(ctx, turn, rest(text, fields[0]))
	case "/agents":
		return s.agentsCommand()
	case "/workflow", "/wf":
		return s.workflowCommand(ctx, turn, text)
	case "/consult":
		return s.consultCommand(ctx, rest(text, fields[0]))
	case "/mcp":
		return s.mcpCommand(ctx, fields[1:])
	case "/skills":
		return s.skillsCommand(text)
	case "/providers":
		return s.providersCommand()
	case "/apikey":
		return s.apikeyCommand()
	case "/whoami":
		return s.whoamiCommand()
	case "/logout":
		return s.logoutCommand()
	case "/doctor":
		return s.doctorCommand(ctx)
	case "/update":
		return s.updateCommand()
	case "/feedback":
		return s.feedbackCommand(rest(text, fields[0]))
	case "/privacy":
		return s.privacyCommand(fields[1:])
	case "/cost":
		return s.costCommand()
	case "/thinking":
		return s.thinkingCommand(fields[1:])
	case "/graph":
		return s.graphCommand(ctx, fields[1:])
	case "/impact":
		return s.impactCommand(ctx, fields[1:])
	case "/lsp":
		return s.lspCommand(ctx, turn, fields[1:])
	case "/hooks":
		return s.hooksCommand(ctx, fields[1:])
	case "/browser":
		return s.browserCommand(ctx, turn, fields[1:])
	case "/screenshot":
		return s.screenshotCommand(ctx, turn)
	case "/login":
		return s.loginCommand(ctx, turn)
	case "/init", "/plan", "/apply-plan", "/fix", "/bug", "/trace", "/quality", "/edit", "/code", "/vision", "/upload-image":
		// runTurn sends these to the model before they get here.
		return "", fmt.Errorf("%s runs as a turn in the interactive application", fields[0])
	case "/help":
		extraHelp := "\n/import <path> — create a new session from JSON\n/export session [path] — complete session including file snapshots\n/export bundle [path] — private diagnostics and lifecycle trace (no conversation or config)\n@ references extract DOCX/PPTX text and PDF text/OCR.\n/config set document_ocr=true — OCR empty PDF pages (Tesseract language data required)\n/config set ocr_language=eng+chi_sim+chi_tra\n/config set document_vision=true — send image/PDF-page evidence to your selected vision-capable provider; off by default\n"
		return "Aria native interface\n\nEnter sends · Ctrl+J / Alt+Enter adds a line\nEsc cancels · Ctrl+O shows operation details · F1 opens help\nTab completes /commands and @file references\n\n/model — interactive model picker, or /model [provider] to list, /model <provider> <model> to switch\n@file:path or @\"path with spaces\" — read a text file into this turn\n/config set gcp_project=PROJECT · gcp_location=REGION · ollama_base_url=URL\n/export <format> [path] — export saved conversation\n/changes · /apply-change ID · /reject-change ID — review edits\n\n" + strings.Join(nativeCommands(), " · ") + extraHelp, nil
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
		if len(fields) == 1 {
			name := providerName(s.cfg)
			if name == "google" {
				text := fmt.Sprintf("google / %s\n/model [provider]: list available models\n/model <provider> <model>: switch provider and model\n/model <model>: switch within current provider", s.cfg.text("model", "gemini-3.8-flash"))
				if tuned := tunedModelNames(s.cfg); len(tuned) > 0 {
					text += "\nTuned models: " + strings.Join(tuned, ", ")
				}
				return text, nil
			}
			return fmt.Sprintf("%s / %s\n/model %s: list available models\n/model <provider> <model>: switch provider and model", name, s.cfg.text("model", ""), name), nil
		}
		if fields[1] == "list" || fields[1] == "ls" {
			return s.modelsCommand(ctx, fields[2:])
		}
		if len(fields) == 3 {
			return s.selectModel(ctx, fields[1], fields[2])
		}
		if len(fields) == 2 {
			arg := fields[1]
			// Support /model provider/model (e.g. /model ollama/qwen2.5:7b or /model google/gemini-2.5-flash)
			if p, m, ok := strings.Cut(arg, "/"); ok && p != "" && m != "" && !strings.HasPrefix(arg, "projects/") {
				return s.selectModel(ctx, p, m)
			}
			// If arg is a known provider name, list models for that provider (e.g. /model ollama)
			if isProviderName(arg) {
				return s.modelsCommand(ctx, []string{arg})
			}
			// Otherwise switch model within current provider
			name := providerName(s.cfg)
			model := arg
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
		}
		return "", errors.New("usage: /model [provider] [model name] or /model list [provider]")
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
		if len(fields) < 3 || fields[1] != "set" {
			return "", errors.New("usage: /config set key=value")
		}
		// The value is the rest of the line, so a command keeps its spaces.
		key, value, ok := strings.Cut(rest(rest(text, "/config"), "set"), "=")
		key = strings.TrimSpace(key)
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
		case "token_price":
			var in, out float64
			if _, err := fmt.Sscanf(strings.ReplaceAll(value, " ", ""), "%g,%g", &in, &out); err != nil || in < 0 || out < 0 {
				return "", errors.New("token_price must be <input>,<output> per million tokens, e.g. 0.3,2.5")
			}
		case "stream_usage":
			switch strings.ToLower(value) {
			case "true", "on", "1":
				stored = true
			case "false", "off", "0":
				stored = false
			default:
				return "", errors.New("stream_usage must be: true | false")
			}
		case "build_command":
			if strings.TrimSpace(value) == "" || len(value) > 4096 {
				return "", errors.New("build_command must be a command of at most 4096 bytes")
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
	case "/finance":
		return s.financeCommand(ctx, fields[1:])
	case "/broker", "/positions", "/orders", "/account":
		return s.brokerCommand(ctx, fields[0], fields[1:])
	default:
		if financeCommands[fields[0]] {
			// runTurn sends these to the model; reached only without it.
			if _, err := s.requireFinance(ctx); err != nil {
				return "", err
			}
			return "", fmt.Errorf("%s runs as a turn in the interactive application", fields[0])
		}
		return "", fmt.Errorf("unknown command: %s; use /help", fields[0])
	}
}

// rest is what follows the command name, spacing inside it kept.
func rest(text, name string) string {
	return strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(text), name))
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

func isProviderName(name string) bool {
	switch strings.ToLower(name) {
	case "google", "ollama", "vertexai", "gcp", "vllm", "llamacpp", "local":
		return true
	default:
		return false
	}
}
