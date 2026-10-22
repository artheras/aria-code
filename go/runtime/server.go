package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"time"

	"google.golang.org/genai"
)

const maxMessage = 1 << 20

type appOptions struct {
	root, workspace, model, resume, banner string
	readRoots, writeRoots, allowedTools    []string
	resumeLast, noBanner, skipPermissions  bool
	hostContained                          bool
	noninteractive                         bool
}
type request struct {
	Type      string `json:"type"`
	Protocol  int    `json:"protocol"`
	TurnID    string `json:"turn_id"`
	Text      string `json:"text"`
	RequestID string `json:"request_id"`
	Choice    *int   `json:"choice"`
	SessionID string `json:"session_id"`
}
type pendingDialog struct {
	id      string
	turn    string
	replies chan request
}
type server struct {
	traceMu          sync.Mutex
	trace            []diagnosticEvent
	tracePath        string
	traceDropped     int
	traceWriteFailed bool
	opts             appOptions
	cfg              config
	scope            *scope
	snap             snapshot
	contents         []*genai.Content
	provider         modelProvider
	out              io.Writer
	mu               sync.Mutex
	outMu            sync.Mutex
	busy             bool
	turn             string
	cancel           context.CancelFunc
	done             chan struct{}
	dialog           *pendingDialog
	grants           map[string]bool
	stop             context.CancelFunc
	historyReset     bool
	historyNotice    string
}

func newServer(options appOptions, out io.Writer) (*server, error) {
	if options.root == "" {
		root, err := stateRoot()
		if err != nil {
			return nil, err
		}
		options.root = root
	}
	if options.workspace == "" {
		root, err := os.Getwd()
		if err != nil {
			return nil, err
		}
		options.workspace = root
	}
	cfg, err := loadConfig(options.root)
	if err != nil {
		return nil, err
	}
	if options.model != "" {
		cfg["model"] = strings.TrimPrefix(options.model, "google/")
	}
	// Google and the Ollama shortcut are built in; others are user-declared
	// (provider_declared.go). Checked before any state is touched.
	provider, err := modelProviderFor(cfg)
	if err != nil {
		return nil, err
	}
	scope, err := newScope(options.workspace, options.readRoots, options.writeRoots, !remoteScope())
	if err != nil {
		return nil, err
	}
	scope.readOnly = cfg.text("permission_mode", "workspace-write") == "read-only"
	s := &server{opts: options, cfg: cfg, scope: scope, out: out, grants: map[string]bool{}}
	if options.resumeLast {
		options.resume = cfg.text("last_session_id", "")
	}
	if options.resume != "" {
		s.snap, err = loadSession(options.root, options.resume)
	} else {
		id, e := newID()
		err = e
		s.snap = snapshot{ID: id, Messages: []message{}, Metadata: map[string]any{}}
	}
	if err != nil {
		return nil, err
	}
	s.contents, err = sessionContents(s.snap)
	if err != nil {
		return nil, err
	}
	s.provider = provider
	if err := saveSession(options.root, s.snap); err != nil {
		return nil, err
	}
	if err := s.saveSetting("last_session_id", s.snap.ID); err != nil {
		return nil, err
	}
	return s, nil
}
func (s *server) emit(kind, turn string, fields map[string]any) error {
	s.recordDiagnostic(kind, fields)
	if fields == nil {
		fields = map[string]any{}
	}
	fields["type"], fields["protocol"] = kind, 1
	if turn == "" {
		fields["turn_id"] = nil
	} else {
		fields["turn_id"] = turn
	}
	body, err := json.Marshal(fields)
	if err != nil {
		return err
	}
	if len(body) >= maxMessage {
		return errors.New("application event exceeds 1 MiB")
	}
	body = append(body, '\n')
	s.outMu.Lock()
	defer s.outMu.Unlock()
	written, writeErr := s.out.Write(body)
	err = writeErr
	if err == nil && written != len(body) {
		err = io.ErrShortWrite
	}
	if err != nil && s.stop != nil {
		s.stop()
	}
	return err
}
func (s *server) state(kind string) error {
	permission := "workspace-write"
	if s.scope.readOnly {
		permission = "read-only"
	}
	fields := map[string]any{"session_id": s.snap.ID, "model": s.cfg.text("model", "gemini-3.8-flash"), "provider": providerName(s.cfg), "version": version, "workspace": s.scope.workspace, "permission": permission, "network": s.cfg.boolean("network_enabled", true), "ui_lang": s.cfg.text("ui_lang", "en"), "ui_theme": "dark", "messages": displayMessages(s.snap.Messages), "commands": nativeCommands(), "commands_info": commandInfoList(s.cfg.text("ui_lang", "en"))}
	theme := strings.ToLower(os.Getenv("ARIA_THEME"))
	if theme != "light" && theme != "dark" {
		theme = s.cfg.text("ui_theme", "auto")
	}
	fields["ui_theme"] = theme
	fields["reference_completion"] = true
	if home, err := os.UserHomeDir(); err == nil {
		home, _ = canonical(home)
		fields["home_workspace"] = home == s.scope.workspace
	}
	fields["robot"] = map[string]any{"design": "cream-block-v2", "theme": theme}
	if s.historyReset {
		fields["history_reset"] = true
		fields["history_notice"] = s.historyNotice
	}
	if s.opts.noBanner || s.opts.banner == "off" {
		fields["robot"] = []any{}
	}
	err := s.emit(kind, "", fields)
	if err == nil {
		s.historyReset = false
		s.historyNotice = ""
	}
	return err
}
func (s *server) persist() error {
	contents, err := json.Marshal(s.contents)
	if err != nil {
		return err
	}
	s.snap.ProviderContents = contents
	return saveSession(s.opts.root, s.snap)
}
func (s *server) dialogRequest(ctx context.Context, kind, turn string, fields map[string]any) (request, error) {
	if s.opts.noninteractive {
		return request{}, errors.New("input requires the interactive application")
	}
	id, err := newID()
	if err != nil {
		return request{}, err
	}
	d := &pendingDialog{id: id, turn: turn, replies: make(chan request, 1)}
	s.mu.Lock()
	if s.dialog != nil {
		s.mu.Unlock()
		return request{}, errors.New("nested application dialog")
	}
	s.dialog = d
	s.mu.Unlock()
	fields["request_id"] = id
	defer func() {
		s.mu.Lock()
		if s.dialog == d {
			s.dialog = nil
		}
		s.mu.Unlock()
		s.emit("dialog.closed", turn, map[string]any{"request_id": id})
	}()
	if err := s.emit(kind, turn, fields); err != nil {
		return request{}, err
	}
	select {
	case <-ctx.Done():
		return request{}, ctx.Err()
	case r := <-d.replies:
		return r, nil
	}
}
func (s *server) approve(ctx context.Context, name string, params map[string]any, change fileChange) (bool, string, error) {
	if err := ctx.Err(); err != nil {
		return false, "", err
	}
	grant := name
	if name == "run_command" {
		body, _ := json.Marshal(params)
		grant = name + ":" + string(body)
	}
	policy, err := persistentToolPolicy(s.opts.root, name)
	if err != nil {
		return false, "invalid persistent policy: " + err.Error(), nil
	}
	if policy == "deny" {
		return false, "persistent denial", nil
	}
	d, known := definitionFor(name)
	if !known {
		return false, "unknown tool", nil
	}
	// What the call touches decides how it is approved (safety/risk.py). L4
	// is asked about every time: a standing "allow" was given for something
	// milder. Under approval_mode=risk, calls at or below auto_approve_level
	// run without a prompt and say so. An explicit "ask" policy always asks.
	assessment := AssessTool(name, params, s.scope.workspace)
	requirement := approvalRequirement(assessment, s.cfg.text("approval_mode", "manual"), s.cfg["auto_approve_level"])
	if policy != "ask" {
		standing := policy == "allow" || s.opts.skipPermissions || s.grants[grant] || contains(s.opts.allowedTools, name)
		if standing && requirement != "always" {
			return true, "", nil
		}
		if !d.mutate {
			return true, "", nil
		}
		if requirement == "auto" {
			summary := assessment.Summary
			if summary == "" {
				summary = name
			}
			return true, fmt.Sprintf("auto-approved · L%d %s · %s", assessment.Level, assessment.Name, summary), nil
		}
	}
	if s.opts.noninteractive {
		if requirement == "always" {
			return false, fmt.Sprintf("L%d %s actions always need interactive approval: %s", assessment.Level, assessment.Name, strings.Join(assessment.Reasons, "; ")), nil
		}
		return false, "approval required; use chat or grant this tool explicitly before starting", nil
	}
	// The assessment travels as data (the frontend draws the card); the text
	// card used to be the first lines of the context and would now show twice.
	contextText := "Tool: " + name
	if name == "run_command" {
		contextText += "\nCommand: " + str(params, "command", "") + "\nWorking directory: " + str(params, "cwd", s.scope.workspace)
	}
	if path := str(params, "path", ""); path != "" {
		contextText += "\nFile: " + path
	}
	if change.path != "" {
		contextText += "\nBefore SHA-256: " + change.sha + fmt.Sprintf("\nNew content: %d bytes", len(change.after))
		contextText += "\n" + changePreview(change)
	}
	if name == "restore_checkpoint" {
		contextText += "\n" + str(params, "preview", "")
	}
	choices := [][]string{{"Allow once", "Only this operation"}, {"Allow for this session", "Same tool within the selected roots; resets with a new session"}, {"No, explain what to do differently", "Decline and provide instructions"}}
	if name == "run_command" {
		choices[1][1] = "Only this exact command and parameters for this session"
	}
	title := "Approve operation"
	if strings.HasPrefix(s.cfg.text("ui_lang", "en"), "zh") {
		title = "确认操作"
		contextText = strings.ReplaceAll(strings.ReplaceAll(contextText, "Tool:", "工具:"), "File:", "文件:")
		choices = [][]string{{"允许一次", "仅本次操作"}, {"本次会话允许", "仅同一工具和当前授权目录；新会话重置"}, {"拒绝并补充要求", "不执行操作，并告诉 Aria 如何调整"}}
		if name == "run_command" {
			choices[1][1] = "仅相同命令与参数；新会话重置"
		}
	}
	s.mu.Lock()
	turn := s.turn
	s.mu.Unlock()
	r, err := s.dialogRequest(ctx, "approval.requested", turn, map[string]any{"title": title, "context": contextText, "choices": choices, "selected": 0, "shortcuts": map[string]any{}, "risk": assessment})
	if err != nil {
		return false, "", err
	}
	if r.Choice == nil || *r.Choice < 0 {
		return false, "declined", nil
	}
	switch *r.Choice {
	case 0:
		return true, "", nil
	case 1:
		s.grants[grant] = true
		return true, "", nil
	case 2:
		r, err := s.dialogRequest(ctx, "input.requested", turn, map[string]any{"prompt": "What should Aria do differently?", "secret": false})
		if err != nil {
			return false, "", err
		}
		return false, r.Text, nil
	default:
		return false, "invalid approval response", nil
	}
}
func (s *server) runTurn(ctx context.Context, turn, text string) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	if err := s.emit("turn.started", turn, nil); err != nil {
		cancel()
	}
	var answer string
	var err error
	var evidence *turnEvidence
	if strings.HasPrefix(strings.TrimSpace(text), "!") {
		answer, err = s.userCommand(ctx, turn, strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(text), "!")))
		s.contents = settlePending(s.contents)
		s.contents = append(s.contents, genai.NewContentFromText(text, genai.RoleUser))
		if answer != "" {
			s.contents = append(s.contents, genai.NewContentFromText(answer, genai.RoleModel))
		}
	} else if strings.HasPrefix(strings.TrimSpace(text), "/") {
		answer, err = s.command(ctx, turn, text)
	} else {
		hooks := agentHooks{emit: func(kind string, fields map[string]any) {
			if err := s.emit(kind, turn, fields); err != nil {
				cancel()
			}
		}, approve: s.approve, apply: func(ctx context.Context, tool string, change fileChange) toolResult {
			return applyRecordedChange(ctx, s.scope, s.opts.root, s.snap.ID, tool, change)
		}, stage: func(ctx context.Context, tool string, change fileChange) toolResult {
			return stageRecordedChange(ctx, s.scope, s.opts.root, s.snap.ID, tool, change)
		}, evidence: newTurnEvidence(s.scope.workspace)}
		s.contents, answer, err = nativeTurn(ctx, s.provider, s.scope, s.cfg, s.contents, text, hooks)
		evidence = hooks.evidence
	}
	if answer != "" {
		if s.historyReset {
			s.historyNotice = capText(answer, 1000)
		}
		if err := s.emit("answer.replace", turn, map[string]any{"text": capText(answer, 65536)}); err != nil {
			cancel()
		}
	}
	outcome := "ok"
	if err != nil {
		outcome = "failed"
		if ctx.Err() != nil || errors.Is(err, context.Canceled) {
			outcome = "cancelled"
		}
	}
	if !strings.HasPrefix(strings.TrimSpace(text), "/") {
		s.snap.Messages = append(s.snap.Messages, message{"role": "user", "content": text})
		if answer != "" {
			s.snap.Messages = append(s.snap.Messages, message{"role": "assistant", "content": answer})
		}
	}
	if saveErr := s.persist(); saveErr != nil {
		outcome = "failed"
		err = fmt.Errorf("session save failed: %w", saveErr)
	}
	fields := map[string]any{"status": outcome, "error": "", "session_id": s.snap.ID}
	if err != nil {
		fields["error"] = err.Error()
	}
	// A turn that changed files, ran checks or had calls refused ends with a
	// report written from that evidence (runtime/delivery.py). Headless
	// results always carry it; delivery_report=false hides it in the chat.
	if evidence != nil {
		stop := "completed"
		switch {
		case outcome == "cancelled":
			stop = "cancelled"
		case err != nil && strings.Contains(err.Error(), "tool round limit reached"):
			stop = "max_rounds"
		case err != nil:
			stop = "error"
		}
		// The checks' verdict, as Python's headless result carries it: the
		// training set leaves out a passing run whose own checks were red.
		if acceptance := evidence.acceptance(); acceptance != nil {
			fields["acceptance"] = acceptance
		}
		if report := evidence.report(stop); report.worthShowing() {
			delivery := report.asDict()
			delivery["text"] = report.Render("", s.scope.workspace)
			delivery["show"] = s.cfg.boolean("delivery_report", true)
			fields["delivery"] = delivery
		}
	}
	s.mu.Lock()
	// Release completion and the final snapshot as one state transition.
	s.emit("turn.completed", turn, fields)
	s.state("session.state")
	s.busy = false
	s.turn = ""
	s.cancel = nil
	close(s.done)
	s.mu.Unlock()
}
func (s *server) switchSession(id string) error {
	var next snapshot
	var err error
	if id != "" {
		next, err = loadSession(s.opts.root, id)
	} else {
		ident, e := newID()
		err = e
		next = snapshot{ID: ident, Messages: []message{}, Metadata: map[string]any{}}
	}
	if err != nil {
		return err
	}
	contents, err := sessionContents(next)
	if err != nil {
		return err
	}
	if err := s.persist(); err != nil {
		return err
	}
	s.snap = next
	s.contents = contents
	s.grants = map[string]bool{}
	if err := s.persist(); err != nil {
		return err
	}
	if err := s.saveSetting("last_session_id", s.snap.ID); err != nil {
		return err
	}
	return s.state("session.ready")
}
func (s *server) run(ctx context.Context, in io.Reader) error {
	ctx, stop := context.WithCancel(ctx)
	defer stop()
	s.stop = stop
	// stdin is an owned worker pipe. Closing it on a signal or broken output
	// wakes the scanner so cancellation also works while the worker is idle.
	wake := context.AfterFunc(ctx, func() {
		if closer, ok := in.(io.Closer); ok {
			closer.Close()
		}
	})
	defer wake()
	scanner := bufio.NewScanner(in)
	scanner.Buffer(make([]byte, 4096), maxMessage)
	if !s.opts.hostContained && (!scanner.Scan() || strings.TrimSpace(scanner.Text()) != "") {
		return errors.New("application start requires the host containment barrier newline")
	}
	if err := s.state("session.ready"); err != nil {
		return err
	}
	for scanner.Scan() {
		if strings.TrimSpace(scanner.Text()) == "" {
			continue
		}
		var r request
		if err := json.Unmarshal(scanner.Bytes(), &r); err != nil || r.Protocol != 1 {
			s.emit("protocol.error", "", map[string]any{"error": "invalid protocol request"})
			continue
		}
		s.mu.Lock()
		busy := s.busy
		turn := s.turn
		switch r.Type {
		case "shutdown":
			cancel, done := s.cancel, s.done
			s.mu.Unlock()
			if cancel != nil {
				cancel()
			}
			if done != nil {
				select {
				case <-done:
				case <-time.After(5 * time.Second):
					return errors.New("runtime did not stop after cancellation")
				}
			}
			if err := s.persist(); err != nil {
				return err
			}
			s.emit("session.closed", "", nil)
			return nil
		case "turn.cancel":
			cancel := s.cancel
			s.mu.Unlock()
			if busy && r.TurnID == turn && cancel != nil {
				cancel()
			} else {
				s.emit("protocol.error", "", map[string]any{"error": "unknown turn"})
			}
			continue
		case "dialog.respond":
			d := s.dialog
			s.mu.Unlock()
			if d == nil || r.RequestID != d.id || r.TurnID != d.turn {
				s.emit("protocol.error", "", map[string]any{"error": "stale dialog response"})
				continue
			}
			select {
			case d.replies <- r:
			default:
				s.emit("protocol.error", "", map[string]any{"error": "dialog already answered"})
			}
			continue
		}
		if busy {
			s.mu.Unlock()
			s.emit("protocol.error", "", map[string]any{"error": "a turn is already running"})
			continue
		}
		switch r.Type {
		case "completion.request":
			s.mu.Unlock()
			if !busy && r.SessionID == s.snap.ID && len(r.Text) <= 65536 {
				s.emit("completion.result", "", map[string]any{"session_id": s.snap.ID, "text": r.Text, "candidates": s.referenceCompletions(r.Text)})
			}
			continue
		case "turn.submit":
			if !validID.MatchString(r.TurnID) || strings.TrimSpace(r.Text) == "" || len(r.Text) > 65536 {
				s.mu.Unlock()
				s.emit("protocol.error", "", map[string]any{"error": "invalid turn ID or text"})
				continue
			}
			s.busy = true
			s.turn = r.TurnID
			s.done = make(chan struct{})
			turnCtx, cancel := context.WithTimeout(ctx, 300*time.Second)
			s.cancel = cancel
			s.mu.Unlock()
			go func() { defer cancel(); s.runTurn(turnCtx, r.TurnID, r.Text) }()
		case "session.new", "session.resume":
			s.mu.Unlock()
			if err := s.switchSession(r.SessionID); err != nil {
				s.emit("protocol.error", "", map[string]any{"error": err.Error()})
			}
		default:
			s.mu.Unlock()
			s.emit("protocol.error", "", map[string]any{"error": "unknown application request"})
		}
	}
	s.mu.Lock()
	cancel, done := s.cancel, s.done
	s.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	if done != nil {
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			return errors.New("runtime cancellation timeout")
		}
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	if err := s.persist(); err != nil {
		return err
	}
	return s.emit("session.closed", "", nil)
}
