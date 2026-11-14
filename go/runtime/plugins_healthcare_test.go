package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestWorkspacePluginDiscoveryAndSkillsCatalog(t *testing.T) {
	root := t.TempDir()
	ws := t.TempDir()
	t.Setenv("ARIA_SKILLS_PATH", "")
	t.Setenv("ARIA_TOOLS_PATH", "")
	t.Setenv("HOME", t.TempDir())

	// 1. Skill in .claude/skills inside workspace
	claudeSkillDir := filepath.Join(ws, ".claude", "skills", "quant-audit")
	if err := os.MkdirAll(claudeSkillDir, 0o755); err != nil {
		t.Fatal(err)
	}
	skillBody := "---\nname: quant-audit\ndescription: Audit portfolio risk and Sharpe ratio\n---\nRun run_backtest and optimize_portfolio.\n"
	if err := os.WriteFile(filepath.Join(claudeSkillDir, "SKILL.md"), []byte(skillBody), 0o644); err != nil {
		t.Fatal(err)
	}

	// 2. aria_tools.py in workspace
	pluginBody := `def get_my_tools():
    return [{"name": "custom_alpha_factor", "description": "Compute custom factor"}]
`
	if err := os.WriteFile(filepath.Join(ws, "aria_tools.py"), []byte(pluginBody), 0o644); err != nil {
		t.Fatal(err)
	}

	instructions := projectInstructions(root, ws)
	if !strings.Contains(instructions, "$quant-audit: Audit portfolio risk") {
		t.Fatalf("expected available_skills catalog in instructions, got %q", instructions)
	}
	if !strings.Contains(instructions, `tools="custom_alpha_factor"`) {
		t.Fatalf("expected workspace_plugin catalog in instructions, got %q", instructions)
	}
}

func TestHealthcareEntitiesAndGuardrail(t *testing.T) {
	if got := resolveHealthcareEntities("Analyze market trends for 12345678"); len(got) != 0 {
		t.Fatalf("expected no healthcare entities on plain numbers, got %v", got)
	}
	got := resolveHealthcareEntities("Compare PMID: 12345, PMC7654321, and trial NCT01234567")
	if len(got) != 3 || got[0] != "PMID:12345" || got[1] != "PMC7654321" || got[2] != "NCT01234567" {
		t.Fatalf("unexpected healthcare entities: %v", got)
	}
	guardrail := healthcarePromptGuardrail("Review trial NCT01234567")
	if !strings.Contains(guardrail, "pending human review") || !strings.Contains(guardrail, "NCT01234567") {
		t.Fatalf("unexpected healthcare guardrail: %q", guardrail)
	}
}

func TestOfflineQuantFallbackWhenFinanceServiceMissing(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	prompt, err := s.financePrompt(context.Background(), "/backtest", "MA crossover")
	if err != nil {
		t.Fatalf("expected offline /backtest fallback to succeed, got %v", err)
	}
	if !strings.Contains(prompt, "run_backtest") || !strings.Contains(prompt, "price_option") {
		t.Fatalf("expected built-in quant tools in offline fallback prompt, got %q", prompt)
	}
	if _, err := s.financePrompt(context.Background(), "/quote", "AAPL"); err == nil {
		t.Fatal("expected /quote to still require external financial-service")
	}
}
