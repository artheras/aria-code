package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writePolicySkill(t *testing.T, dir, name, policy string) {
	t.Helper()
	folder := filepath.Join(dir, name)
	os.MkdirAll(folder, 0o755)
	os.WriteFile(filepath.Join(folder, "SKILL.md"), []byte("---\nname: "+name+"\ndescription: test skill "+name+"\n---\nUse get_market_data, then calculate_factors.\n"), 0o644)
	if policy != "" {
		os.WriteFile(filepath.Join(folder, "skill-policy.json"), []byte(policy), 0o644)
	}
}

func TestSkillPoliciesMapToGoTools(t *testing.T) {
	root, workspace := t.TempDir(), t.TempDir()
	t.Setenv("ARIA_SKILLS_PATH", "")
	t.Setenv("HOME", t.TempDir())
	skills := filepath.Join(root, "skills")
	writePolicySkill(t, skills, "equity-check", `{"allowed_tools": ["read_file", "analyze_file", "get_market_data", "calculate_factors", "web_fetch"]}`)
	writePolicySkill(t, skills, "poster", `{"allowed_tools": ["generate_image", "aria.report.estimate_image_cost", "mcp__x__y"]}`)
	writePolicySkill(t, skills, "plain", "")
	writePolicySkill(t, skills, "broken", `{not json`)

	sk, ok := findSkill(root, workspace, "equity-check")
	if !ok || len(sk.allowedTools) != 5 {
		t.Fatal(sk)
	}
	if plain, _ := findSkill(root, workspace, "plain"); plain.allowedTools != nil {
		t.Fatal("no policy file, no tools")
	}
	if broken, ok := findSkill(root, workspace, "broken"); !ok || broken.allowedTools != nil {
		t.Fatal("a broken policy leaves the skill usable without extra tools")
	}

	for name, want := range map[string]string{
		"read_file":                       "read_file",
		"analyze_file":                    "read_file",
		"web_fetch":                       "web_fetch",
		"get_market_data":                 "mcp__arthera-finance__financial_quote",
		"calculate_factors":               "mcp__arthera-finance__financial_technical_factors",
		"generate_image":                  "mcp__aria__aria_report_generate_image",
		"aria.report.estimate_image_cost": "mcp__aria__aria_report_estimate_image_cost",
		"mcp__x__y":                       "mcp__x__y",
	} {
		if got := goToolName(name); got != want {
			t.Errorf("%s -> %s, want %s", name, got, want)
		}
	}

	// $name activates a skill; so does the block /skills use puts in the prompt.
	active := activeSkills(root, workspace, "make one with $poster and $missing please")
	if len(active) != 1 || active[0].name != "poster" {
		t.Fatal(active)
	}
	prompt, err := (&server{opts: appOptions{root: root}, scope: &scope{workspace: workspace}}).skillPrompt("equity-check check AAPL")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(prompt, "get_market_data → mcp__arthera-finance__financial_quote") || strings.Contains(prompt, "read_file →") {
		t.Fatal("the skill block names what its older tool names mean", prompt)
	}
	active = activeSkills(root, workspace, prompt+" and $poster")
	tools := skillTools(active)
	for _, want := range []string{"mcp__arthera-finance__financial_quote", "mcp__aria__aria_report_generate_image", "read_file", "web_fetch"} {
		if !tools[want] {
			t.Errorf("missing %s in %v", want, tools)
		}
	}
	if len(skillTools(nil)) != 0 {
		t.Fatal("no skill, no extra tools")
	}
}

func TestAriaToolsAreOfferedOnlyToSkillsThatAskForThem(t *testing.T) {
	m := newMCPManager(t.TempDir())
	m.loaded = true
	m.clients[selfMCPName] = &mcpClient{tools: []mcpTool{{Name: "aria.report.chart"}, {Name: "aria.report.generate_image"}}}
	m.clients[financeServer] = &mcpClient{tools: []mcpTool{{Name: "financial_quote"}}}
	names := func(offered map[string]bool) string {
		var out []string
		for _, d := range m.declarationsFor(offered) {
			out = append(out, d.Name)
		}
		return strings.Join(out, ",")
	}
	if got := names(nil); got != "mcp__aria__aria_report_chart,mcp__aria__aria_report_generate_image,mcp__arthera-finance__financial_quote" {
		t.Fatal("nil offers everything (status, /mcp)", got)
	}
	if got := names(map[string]bool{}); got != "mcp__arthera-finance__financial_quote" {
		t.Fatal("an ordinary turn carries no Aria media tools", got)
	}
	if got := names(map[string]bool{"mcp__aria__aria_report_chart": true}); got != "mcp__aria__aria_report_chart,mcp__arthera-finance__financial_quote" {
		t.Fatal(got)
	}
}

func TestSelfMCPServerIsStartedBeside(t *testing.T) {
	root := t.TempDir()
	saved := selfMCPCommand
	defer func() { selfMCPCommand = saved }()
	t.Setenv("ARIA_SELF_MCP", "0")
	if _, _, ok := saved(); ok {
		t.Fatal("ARIA_SELF_MCP=0 turns it off")
	}
	t.Setenv("ARIA_SELF_MCP", "")
	if _, _, ok := saved(); ok {
		t.Fatal("a binary that is not aria-runtime does not start itself")
	}
	selfMCPCommand = func() (string, []string, bool) {
		return filepath.Join(root, "missing-aria-runtime"), []string{"mcp"}, true
	}
	m := newMCPManager(root)
	m.ensure(context.Background())
	found := false
	for _, cfg := range m.configs {
		found = found || (cfg.Name == selfMCPName && len(cfg.Args) == 1 && cfg.Args[0] == "mcp")
	}
	if !found || m.failed[selfMCPName] == "" {
		t.Fatal("the built-in server is configured (and here fails to start)", m.configs, m.failed)
	}
	// A user's own "aria" entry wins.
	os.WriteFile(filepath.Join(root, "mcp_servers.json"), []byte(`{"servers":[{"name":"aria","command":"/bin/false","enabled":false}]}`), 0o644)
	m = newMCPManager(root)
	m.ensure(context.Background())
	if len(m.configs) != 1 || m.configs[0].Command != "/bin/false" {
		t.Fatal(m.configs)
	}
}
