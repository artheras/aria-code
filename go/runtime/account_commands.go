package main

// Account and maintenance commands ported from the Python worker
// (auth_cmds.py, model_cmds.py, core_cmds.py, diagnostic_ops_cmds.py):
// /providers, /apikey, /whoami, /logout, /doctor, /update, /feedback and
// /privacy.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"
)

// providerRows are the built-in and declared providers with whether each
// can be used now. Credentials are never shown, only whether they are set.
func providerRows() ([][3]string, string, map[string]string) {
	cfg, source := loadProviderConfig()
	specs, problems := parseDeclaredProviders(cfg)
	google := "needs Google credentials (gcloud auth application-default login) or GEMINI_API_KEY"
	if os.Getenv("GEMINI_API_KEY") != "" || os.Getenv("GOOGLE_API_KEY") != "" || os.Getenv("GOOGLE_APPLICATION_CREDENTIALS") != "" {
		google = "credentials found"
	}
	rows := [][3]string{{"google", "Gemini (built in)", google}, {"ollama", "Local Ollama (built in)", "no key needed"}}
	gateway := "needs /login"
	if root, err := stateRoot(); err == nil {
		if cfg, err := loadConfig(root); err == nil && tokenClaims(cfg.text("auth_token", "")) != nil {
			gateway = "signed in"
		}
	}
	rows = append(rows, [3]string{gatewayProviderID, "Arthera gateway (Google models, built in)", gateway})
	for _, p := range providerPresets {
		if _, declared := specs[p.id]; declared {
			continue
		}
		state := "no key needed"
		if p.envKey != "" {
			state = "needs " + p.envKey
			if strings.TrimSpace(os.Getenv(p.envKey)) != "" {
				state = p.envKey + " is set"
			}
		}
		rows = append(rows, [3]string{p.id, p.name + " (built in)", state})
	}
	ids := make([]string, 0, len(specs))
	for id := range specs {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		spec := specs[id]
		state := "no key needed"
		switch {
		case spec.Auth != nil:
			state = "token from `" + spec.Auth.Command + "`"
		case spec.EnvKey != "" && strings.TrimSpace(os.Getenv(spec.EnvKey)) != "":
			state = spec.EnvKey + " is set"
		case spec.EnvKey != "":
			state = "needs " + spec.EnvKey
		}
		name := spec.Name
		if name == "" {
			name = spec.BaseURL
		}
		rows = append(rows, [3]string{id, name, state})
	}
	return rows, source, problems
}

func (s *server) providersCommand() (string, error) {
	rows, source, problems := providerRows()
	current := providerName(s.cfg)
	var b strings.Builder
	b.WriteString("Providers")
	for _, row := range rows {
		mark := " "
		if row[0] == current {
			mark = "●"
		}
		fmt.Fprintf(&b, "\n  %s %-16s %-36s %s", mark, row[0], capText(row[1], 36), row[2])
	}
	for id, reason := range problems {
		fmt.Fprintf(&b, "\n  ✗ %-16s skipped: %s", id, reason)
	}
	if source != "" {
		b.WriteString("\n\nDeclared in " + source)
	} else {
		b.WriteString("\n\nDeclare more in ~/.aria/providers.yaml (model_providers: …)")
	}
	return b.String() + "\n/model <provider> <model> switches", nil
}

// apikeyCommand says which variable each provider reads its key from. Keys
// are not stored by Aria: a file that holds a key is refused when it loads
// (provider_declared.go), so the place for one is the environment or a
// token command.
func (s *server) apikeyCommand() (string, error) {
	rows, _, _ := providerRows()
	var b strings.Builder
	b.WriteString("API keys\n\nAria reads keys from environment variables or a token command, never from its own files, so a key cannot leak through a shared config or a session export.\n")
	for _, row := range rows {
		fmt.Fprintf(&b, "\n  %-16s %s", row[0], row[2])
	}
	b.WriteString("\n\nSet one in your shell profile (e.g. export MYCORP_LLM_TOKEN=…) and restart Aria.")
	return b.String(), nil
}

func (s *server) whoamiCommand() (string, error) {
	user := s.cfg.text("user_id", "")
	if user == "" || s.cfg.text("auth_token", "") == "" {
		return "Not signed in to Arthera. Models still work with your own provider (/providers).", nil
	}
	email := s.cfg.text("email", "")
	text := "Signed in to Arthera as " + user
	if email != "" {
		text += " (" + email + ")"
	}
	if expires := s.cfg.text("token_expires_at", ""); expires != "" {
		text += "\nToken expires " + expires
	}
	return text, nil
}

func (s *server) logoutCommand() (string, error) {
	if s.cfg.text("auth_token", "") == "" && s.cfg.text("user_id", "") == "" {
		return "Not signed in.", nil
	}
	disk, err := loadConfig(s.opts.root)
	if err != nil {
		return "", err
	}
	for _, key := range []string{"auth_token", "refresh_token", "user_id", "email", "token_expires_at"} {
		delete(disk, key)
		delete(s.cfg, key)
	}
	if err := atomicJSON(filepath.Join(s.opts.root, "config.json"), disk); err != nil {
		return "", err
	}
	return "Signed out of Arthera on this machine.", nil
}

type doctorCheck struct {
	ok     bool
	name   string
	detail string
}

func (s *server) doctorCommand(ctx context.Context) (string, error) {
	checks := []doctorCheck{{true, "Aria", fmt.Sprintf("%s · %s/%s", version, runtime.GOOS, runtime.GOARCH)}}
	add := func(ok bool, name, detail string) { checks = append(checks, doctorCheck{ok, name, detail}) }

	probe := filepath.Join(s.opts.root, ".doctor-probe")
	if err := os.WriteFile(probe, []byte("ok"), 0o600); err != nil {
		add(false, "State directory", s.opts.root+" is not writable: "+err.Error())
	} else {
		os.Remove(probe)
		add(true, "State directory", s.opts.root)
	}
	if _, err := loadConfig(s.opts.root); err != nil {
		add(false, "config.json", err.Error())
	} else {
		add(true, "config.json", "valid")
	}
	if _, err := modelProviderFor(s.cfg); err != nil {
		add(false, "Model", err.Error())
	} else {
		add(true, "Model", providerName(s.cfg)+" / "+s.cfg.text("model", "gemini-3.8-flash")+" (/health tests the connection)")
	}
	if _, _, problems := providerRows(); len(problems) > 0 {
		names := []string{}
		for id := range problems {
			names = append(names, id)
		}
		sort.Strings(names)
		add(false, "Declared providers", "skipped: "+strings.Join(names, ", ")+" (/providers says why)")
	}
	if _, err := persistentToolPolicy(s.opts.root, "read_file"); err != nil {
		add(false, "tool_policy.json", err.Error())
	}
	for _, tool := range []struct{ name, why string }{{"git", "/diff, /git, /commit, /task"}, {"gh", "/pr, /gh"}, {"rg", "faster search (optional)"}} {
		if path, err := exec.LookPath(tool.name); err == nil {
			add(true, tool.name, path)
		} else {
			add(tool.name == "rg" || tool.name == "gh", tool.name, "not found — needed for "+tool.why)
		}
	}
	if configs, err := loadMCPConfig(s.opts.root); err != nil {
		add(false, "MCP", err.Error())
	} else if len(configs) > 0 {
		add(true, "MCP", fmt.Sprintf("%d server(s) configured (/mcp starts and checks them)", len(configs)))
	}
	if user, project, untrusted := s.hookCounts(); user+project > 0 {
		detail := fmt.Sprintf("%d of yours, %d from the project", user, project)
		if untrusted {
			detail += " (not trusted: /hooks trust)"
		}
		add(true, "Hooks", detail)
	}
	if found := workspaceFiles(s.scope.workspace); len(found) == 0 {
		add(true, "Project instructions", "none (/init writes ARIA.md)")
	} else {
		add(true, "Project instructions", strings.Join(found, ", "))
	}
	if s.scope.readOnly {
		add(true, "Permissions", "read-only session")
	}
	var b strings.Builder
	b.WriteString("Doctor")
	problems := 0
	for _, c := range checks {
		mark := "✓"
		if !c.ok {
			mark = "✗"
			problems++
		}
		fmt.Fprintf(&b, "\n  %s %-22s %s", mark, c.name, c.detail)
	}
	if problems == 0 {
		b.WriteString("\n\nNo problems found.")
	} else {
		fmt.Fprintf(&b, "\n\n%d problem(s).", problems)
	}
	return b.String(), nil
}

func (s *server) updateCommand() (string, error) {
	return fmt.Sprintf("Aria %s. To update, quit and run `aria update` in your shell (or reinstall with the installer you used); the terminal app replaces both the interface and the runtime.", version), nil
}

// feedbackCommand keeps a rating of the last answer in <state>/feedback.jsonl.
// It stays on this machine unless /privacy share is on, and even then the
// answer text is only included when the user says so with --with-text.
func (s *server) feedbackCommand(args string) (string, error) {
	rating, comment, _ := strings.Cut(strings.TrimSpace(args), " ")
	rating = strings.ToLower(rating)
	if rating != "good" && rating != "bad" {
		return "", errors.New("usage: /feedback good|bad [comment] [--with-text]")
	}
	withText := strings.Contains(comment, "--with-text")
	comment = strings.TrimSpace(strings.ReplaceAll(comment, "--with-text", ""))
	entry := map[string]any{"at": time.Now().UTC().Format(time.RFC3339), "rating": rating, "comment": comment,
		"session_id": s.snap.ID, "model": s.cfg.text("model", ""), "provider": providerName(s.cfg)}
	if withText {
		for i := len(s.snap.Messages) - 1; i >= 0; i-- {
			if s.snap.Messages[i]["role"] == "assistant" {
				entry["answer"], _ = s.snap.Messages[i]["content"].(string)
				break
			}
		}
	}
	body, err := json.Marshal(entry)
	if err != nil {
		return "", err
	}
	f, err := os.OpenFile(filepath.Join(s.opts.root, "feedback.jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return "", err
	}
	defer f.Close()
	if _, err := f.Write(append(body, '\n')); err != nil {
		return "", err
	}
	where := "kept on this machine"
	if s.cfg.boolean("feedback_sharing", false) {
		where = "kept locally and marked for sharing"
	}
	return "Thanks — feedback " + where + ".", nil
}

func (s *server) privacyCommand(args []string) (string, error) {
	if len(args) == 2 && args[0] == "share" && (args[1] == "on" || args[1] == "off") {
		value := args[1] == "on"
		if err := s.saveSetting("feedback_sharing", value); err != nil {
			return "", err
		}
		s.cfg["feedback_sharing"] = value
	} else if len(args) != 0 {
		return "", errors.New("usage: /privacy [share on|off]")
	}
	share := "off — /feedback stays in feedback.jsonl on this machine"
	if s.cfg.boolean("feedback_sharing", false) {
		share = "on — /feedback entries are marked for sharing; answer text only with --with-text"
	}
	return table("Privacy", [][2]string{
		{"Conversations", "sent only to the model provider you chose (" + providerName(s.cfg) + ")"},
		{"Sessions", filepath.Join(s.opts.root, "sessions") + " (local)"},
		{"Telemetry", "none"},
		{"Feedback", share},
		{"Diagnostics", "only when you run /export bundle; no conversation or config"},
	}), nil
}
