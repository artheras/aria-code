package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestProvidersAndApikeyNeverShowSecrets(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("MYCORP_TOKEN", "secret-value-123")
	os.MkdirAll(filepath.Join(home, ".aria"), 0o755)
	os.WriteFile(filepath.Join(home, ".aria", "providers.yaml"), []byte("model_providers:\n  mycorp:\n    name: Corp gateway\n    base_url: https://llm.example.com/v1\n    env_key: MYCORP_TOKEN\n  other:\n    base_url: https://other.example.com/v1\n    env_key: OTHER_TOKEN\n"), 0o644)
	s := approvalServer(t, config{}, appOptions{})
	out, err := s.command(context.Background(), "t", "/providers")
	if err != nil || !strings.Contains(out, "mycorp") || !strings.Contains(out, "MYCORP_TOKEN is set") || !strings.Contains(out, "needs OTHER_TOKEN") || !strings.Contains(out, "● google") {
		t.Fatal(out, err)
	}
	keys, _ := s.command(context.Background(), "t", "/apikey")
	if strings.Contains(out+keys, "secret-value-123") || !strings.Contains(keys, "never from its own files") {
		t.Fatal("a key was shown", keys)
	}
}

func TestWhoamiAndLogout(t *testing.T) {
	s := approvalServer(t, config{"auth_token": "tok", "user_id": "u-1", "email": "a@example.invalid", "model": "m"}, appOptions{})
	if out, _ := s.command(context.Background(), "t", "/whoami"); !strings.Contains(out, "u-1") || !strings.Contains(out, "a@example.invalid") || strings.Contains(out, "tok") {
		t.Fatal(out)
	}
	if out, err := s.command(context.Background(), "t", "/logout"); err != nil || !strings.Contains(out, "Signed out") {
		t.Fatal(out, err)
	}
	disk, _ := loadConfig(s.opts.root)
	if _, ok := disk["auth_token"]; ok || disk["model"] != "m" {
		t.Fatal("logout left the token or lost other settings", disk)
	}
	if out, _ := s.command(context.Background(), "t", "/whoami"); !strings.Contains(out, "Not signed in") {
		t.Fatal(out)
	}
}

func TestDoctorReportsProblems(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	out, err := s.command(context.Background(), "t", "/doctor")
	if err != nil || !strings.Contains(out, "✓ State directory") || !strings.Contains(out, "✓ config.json") || !strings.Contains(out, "Project instructions") {
		t.Fatal(out, err)
	}
	os.WriteFile(filepath.Join(s.opts.root, "mcp_servers.json"), []byte("{not json"), 0o644)
	if out, _ := s.command(context.Background(), "t", "/doctor"); !strings.Contains(out, "✗ MCP") || !strings.Contains(out, "problem(s)") {
		t.Fatal(out)
	}
}

func TestFeedbackStaysLocalAndPrivacyToggles(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	s.snap.Messages = []message{{"role": "user", "content": "q"}, {"role": "assistant", "content": "private answer"}}
	if out, err := s.command(context.Background(), "t", "/feedback good great job"); err != nil || !strings.Contains(out, "on this machine") {
		t.Fatal(out, err)
	}
	if _, err := s.command(context.Background(), "t", "/feedback bad wrong file --with-text"); err != nil {
		t.Fatal(err)
	}
	body, _ := os.ReadFile(filepath.Join(s.opts.root, "feedback.jsonl"))
	lines := strings.Split(strings.TrimSpace(string(body)), "\n")
	var first, second map[string]any
	json.Unmarshal([]byte(lines[0]), &first)
	json.Unmarshal([]byte(lines[1]), &second)
	if len(lines) != 2 || first["comment"] != "great job" || first["answer"] != nil || second["answer"] != "private answer" || second["comment"] != "wrong file" {
		t.Fatal(lines)
	}
	if _, err := s.command(context.Background(), "t", "/feedback meh"); err == nil {
		t.Fatal("bad rating accepted")
	}
	if out, _ := s.command(context.Background(), "t", "/privacy share on"); !strings.Contains(out, "marked for sharing") {
		t.Fatal(out)
	}
	if out, _ := s.command(context.Background(), "t", "/privacy"); !strings.Contains(out, "Telemetry") {
		t.Fatal(out)
	}
	if out, _ := s.command(context.Background(), "t", "/update"); !strings.Contains(out, "aria update") {
		t.Fatal(out)
	}
}
