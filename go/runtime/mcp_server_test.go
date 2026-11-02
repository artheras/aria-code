package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// mcpSession sends each request on its own line and returns the replies by id.
func mcpSession(t *testing.T, requests ...map[string]any) (map[float64]map[string]any, []map[string]any) {
	t.Helper()
	var in bytes.Buffer
	for _, r := range requests {
		line, _ := json.Marshal(r)
		in.Write(append(line, '\n'))
	}
	in.WriteString("not json\n")
	var out, logs bytes.Buffer
	if err := mcpServe(context.Background(), &in, &out, &logs); err != nil {
		t.Fatal(err)
	}
	byID := map[float64]map[string]any{}
	var all []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		var reply map[string]any
		if err := json.Unmarshal([]byte(line), &reply); err != nil {
			t.Fatal(line, err)
		}
		all = append(all, reply)
		if id, ok := reply["id"].(float64); ok {
			byID[id] = reply
		}
	}
	return byID, all
}

func mcpCallRequest(id int, tool string, args map[string]any) map[string]any {
	return map[string]any{"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": map[string]any{"name": tool, "arguments": args}}
}

func toolPayload(t *testing.T, reply map[string]any) map[string]any {
	t.Helper()
	result, ok := reply["result"].(map[string]any)
	if !ok {
		t.Fatal("no result", reply)
	}
	text := result["content"].([]any)[0].(map[string]any)["text"].(string)
	var payload map[string]any
	if err := json.Unmarshal([]byte(text), &payload); err != nil {
		t.Fatal(text, err)
	}
	if payload["success"] == false && result["isError"] != true {
		t.Fatal("a failed call must set isError", reply)
	}
	return payload
}

func TestMCPServerProtocolAndTools(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("ARIA_HOME", filepath.Join(home, ".aria-state"))
	project := filepath.Join(home, "project")
	skills := filepath.Join(home, "skills")
	t.Setenv("ARIA_SKILLS_PATH", skills)
	for path, text := range map[string]string{
		"project/go.mod":                         "module example.com/p\n\ngo 1.22\n",
		"project/a/a.go":                         "package a\n\nfunc A() int { return 1 }\n",
		"project/b/b.go":                         "package b\n\nimport \"example.com/p/a\"\n\nfunc B() int { return a.A() }\n",
		"project/b/b_test.go":                    "package b_test\n\nimport (\n\t\"testing\"\n\n\t\"example.com/p/a\"\n)\n\nfunc TestA(t *testing.T) { a.A() }\n",
		"project/.aria/policy.yaml":              "contract:\n  allow: [a/**]\n",
		"skills/landing-page/SKILL.md":           "---\nname: landing-page\ndescription: Design a landing page for a product\n---\nUse a clear hero section.\n",
		"skills/landing-page/references/hero.md": "Hero rules.\n",
		"skills/sql-review/SKILL.md":             "---\nname: sql-review\ndescription: Review SQL migrations\n---\nCheck locks.\n",
		"secret.md":                              "do not leak\n",
	} {
		full := filepath.Join(home, filepath.FromSlash(path))
		os.MkdirAll(filepath.Dir(full), 0o755)
		if err := os.WriteFile(full, []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	t.Chdir(project)

	replies, all := mcpSession(t,
		map[string]any{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": map[string]any{"protocolVersion": "2024-11-05"}},
		map[string]any{"jsonrpc": "2.0", "method": "notifications/initialized"},
		map[string]any{"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
		mcpCallRequest(3, "aria.risk.assess", map[string]any{"command": "rm -rf /"}),
		mcpCallRequest(4, "aria.risk.assess", map[string]any{"tool": "write_file", "params": map[string]any{"path": "a/a.go"}}),
		mcpCallRequest(5, "aria.contract.check", map[string]any{"tool": "write_file", "params": map[string]any{"path": "b/b.go"}}),
		mcpCallRequest(6, "aria.contract.check", map[string]any{"tool": "write_file", "params": map[string]any{"path": "a/x.go"}}),
		mcpCallRequest(7, "aria.project.impact", map[string]any{"targets": []any{"a/a.go"}}),
		mcpCallRequest(8, "aria.project.commands", map[string]any{}),
		mcpCallRequest(9, "aria.skill.list", map[string]any{"query": "design a landing page"}),
		mcpCallRequest(10, "aria.skill.get", map[string]any{"name": "landing-page"}),
		mcpCallRequest(11, "aria.skill.get", map[string]any{"name": "landing-page", "reference": "hero.md"}),
		mcpCallRequest(12, "aria.skill.get", map[string]any{"name": "landing-page", "reference": "../../../secret.md"}),
		mcpCallRequest(13, "aria.risk.assess", map[string]any{"tool": "write_file", "params": map[string]any{}, "root": "/"}),
		mcpCallRequest(14, "aria.broker.confirm_order", map[string]any{}),
		map[string]any{"jsonrpc": "2.0", "id": 15, "method": "resources/list"},
		map[string]any{"jsonrpc": "2.0", "id": 16, "method": "ping"},
	)
	// Notifications get no reply; the bad line gets a parse error with a null id.
	if len(all) != 17 || all[len(all)-1]["error"].(map[string]any)["code"] != float64(-32700) {
		t.Fatal(len(all), all[len(all)-1])
	}
	init := replies[1]["result"].(map[string]any)
	if init["protocolVersion"] != "2024-11-05" || init["serverInfo"].(map[string]any)["name"] != "aria-code" {
		t.Fatal(init)
	}
	tools := replies[2]["result"].(map[string]any)["tools"].([]any)
	if len(tools) != 6 {
		t.Fatal(len(tools))
	}
	for _, raw := range tools {
		tool := raw.(map[string]any)
		if tool["annotations"].(map[string]any)["readOnlyHint"] != true || tool["inputSchema"].(map[string]any)["type"] != "object" {
			t.Fatal("every tool is read-only with a schema", tool)
		}
	}
	risk := toolPayload(t, replies[3])
	if risk["assessment"].(map[string]any)["level"].(float64) < 3 || len(risk["card"].([]any)) == 0 {
		t.Fatal(risk)
	}
	if toolPayload(t, replies[4])["assessment"].(map[string]any)["files"] == nil {
		t.Fatal(replies[4])
	}
	if blocked := toolPayload(t, replies[5]); blocked["allowed"] != false || blocked["rule"] != "allow" {
		t.Fatal(blocked)
	}
	if allowed := toolPayload(t, replies[6]); allowed["allowed"] != true {
		t.Fatal(allowed)
	}
	impact := toolPayload(t, replies[7])["impact"].(map[string]any)
	if !strings.Contains(strings.Join(anyStrings(impact["direct"]), ","), "b/b.go") || len(anyStrings(impact["tests"])) == 0 {
		t.Fatal(impact)
	}
	if commands := toolPayload(t, replies[8])["commands"].(map[string]any); commands["test"] != "go test ./..." || commands["lint"] != "go vet ./..." {
		t.Fatal(commands)
	}
	list := toolPayload(t, replies[9])
	first := list["skills"].([]any)[0].(map[string]any)
	if list["matched_by_relevance"] != true || first["name"] != "landing-page" || len(list["skills"].([]any)) != 1 {
		t.Fatal(list)
	}
	got := toolPayload(t, replies[10])
	if got["instructions"] != "Use a clear hero section." || anyStrings(got["references"])[0] != "hero.md" {
		t.Fatal(got)
	}
	if toolPayload(t, replies[11])["content"] != "Hero rules.\n" {
		t.Fatal(replies[11])
	}
	if escaped := toolPayload(t, replies[12]); escaped["success"] != false || strings.Contains(escaped["error"].(string)+jsonText(escaped), "do not leak") {
		t.Fatal(escaped)
	}
	if outside := toolPayload(t, replies[13]); outside["success"] != false || !strings.Contains(outside["error"].(string), "inside") {
		t.Fatal(outside)
	}
	if replies[14]["error"].(map[string]any)["code"] != float64(-32601) || replies[15]["error"].(map[string]any)["code"] != float64(-32601) {
		t.Fatal("trading tools and unknown methods are not served", replies[14], replies[15])
	}
	if _, ok := replies[16]["result"]; !ok {
		t.Fatal(replies[16])
	}
}

func TestMCPServerVersionAndUsage(t *testing.T) {
	var out, logs bytes.Buffer
	if code := mcpServerCommand([]string{"--version"}, strings.NewReader(""), &out, &logs); code != 0 || !strings.Contains(out.String(), "aria-runtime mcp") {
		t.Fatal(code, out.String())
	}
	if code := mcpServerCommand([]string{"extra"}, strings.NewReader(""), &out, &logs); code != 2 {
		t.Fatal(code)
	}
}

func anyStrings(value any) []string {
	items, _ := value.([]any)
	out := make([]string, 0, len(items))
	for _, item := range items {
		if text, ok := item.(string); ok {
			out = append(out, text)
		}
	}
	return out
}

func jsonText(value any) string {
	body, _ := json.Marshal(value)
	return string(body)
}

func TestDetectProjectCommands(t *testing.T) {
	write := func(root string, files map[string]string) string {
		for path, text := range files {
			full := filepath.Join(root, filepath.FromSlash(path))
			os.MkdirAll(filepath.Dir(full), 0o755)
			os.WriteFile(full, []byte(text), 0o644)
		}
		return root
	}
	pnpm := write(t.TempDir(), map[string]string{"package.json": `{"scripts":{"test":"vitest","lint":"eslint ."}}`, "pnpm-lock.yaml": "", "Makefile": "build:\n\tgo build\n"})
	if got := detectProjectCommands(pnpm); got["test"] != "pnpm test" || got["lint"] != "pnpm run lint" || got["build"] != "make build" {
		t.Fatal(got)
	}
	ros := write(t.TempDir(), map[string]string{"src/robot/package.xml": "<package/>", ".aria/project.yaml": "commands:\n  sim: ros2 launch r sim.launch.py\n  bogus: rm -rf /\n"})
	if got := detectProjectCommands(ros); got["test"] != "colcon test" || got["test_report"] == "" || got["sim"] != "ros2 launch r sim.launch.py" || got["bogus"] != "" {
		t.Fatal(got)
	}
	declared := write(t.TempDir(), map[string]string{"src/robot/package.xml": "<package/>", ".aria/project.yaml": "commands:\n  test: ./scripts/test.sh\n"})
	if got := detectProjectCommands(declared); got["test"] != "./scripts/test.sh" || got["test_report"] != "" {
		t.Fatal(got)
	}
	python := write(t.TempDir(), map[string]string{"pyproject.toml": "[build-system]\n[tool.ruff]\n", "tests/test_a.py": ""})
	if got := detectProjectCommands(python); got["test"] != "python3 -m pytest -q" || got["build"] != "python3 -m build" || got["lint"] != "ruff check ." {
		t.Fatal(got)
	}
	if got := detectProjectCommands(t.TempDir()); len(got) != 0 {
		t.Fatal("nothing is guessed", got)
	}
}
