package main

// `aria-runtime mcp`: Aria's own capabilities as an MCP server over stdio,
// for other agents (Claude Code, Codex, Cursor, ...). A port of the core of
// src/aria_code/packages/aria_mcp/server.py, which also served finance,
// trading, reports, images, video and Figma; those belong elsewhere:
// finance to financial-service (an MCP server itself), trading to its own
// gated service, media to a separate decision. Every tool here only reads.
//
// Register with: claude mcp add aria -- aria-runtime mcp

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"go.yaml.in/yaml/v3"
)

const mcpServerProtocol = "2024-11-05"

type mcpServedTool struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
	Annotations map[string]any `json:"annotations"`
}

var rootProperty = map[string]any{"type": "string", "description": "Project directory (default: the server's working directory)."}

func readOnlyTool(name, description string, properties map[string]any, required ...string) mcpServedTool {
	schema := map[string]any{"type": "object", "properties": properties}
	if len(required) > 0 {
		schema["required"] = required
	}
	return mcpServedTool{Name: name, Description: description, InputSchema: schema,
		Annotations: map[string]any{"readOnlyHint": true, "destructiveHint": false, "idempotentHint": true, "openWorldHint": false}}
}

var mcpServerTools = []mcpServedTool{
	readOnlyTool("aria.risk.assess", "Assess what a shell command or tool call would do before running it: capabilities, reversibility, hosts, files, a risk level, and whether Aria would ask for approval.",
		map[string]any{
			"command": map[string]any{"type": "string", "description": "A shell command to assess, e.g. 'npm install jose'."},
			"tool":    map[string]any{"type": "string", "description": "Or a tool name (write_file, edit_file, run_command, ...)."},
			"params":  map[string]any{"type": "object", "description": "The tool's parameters."},
			"root":    rootProperty,
		}),
	readOnlyTool("aria.contract.check", "Check a tool call against the project's change contract (.aria/policy.yaml): allowed or not, the rule and the reason.",
		map[string]any{
			"tool":   map[string]any{"type": "string", "description": "The tool call to check, e.g. write_file or run_command."},
			"params": map[string]any{"type": "object", "description": "The tool's parameters, e.g. {path: 'src/a.py'}."},
			"root":   rootProperty,
		}, "tool"),
	readOnlyTool("aria.project.impact", "What a change to these files or symbols reaches: direct and indirect importers, tests and services, from the project's import graph.",
		map[string]any{
			"targets": map[string]any{"type": "array", "items": map[string]any{"type": "string"}, "description": "Paths or symbols, e.g. ['src/auth/session.py', 'Session.refresh']."},
			"root":    rootProperty,
		}, "targets"),
	readOnlyTool("aria.project.commands", "The project's own test, build and lint commands: what .aria/project.yaml declares, otherwise what its manifests (package.json, Cargo.toml, go.mod, pyproject.toml, Makefile, ROS 2) define.",
		map[string]any{"root": rootProperty}),
	readOnlyTool("aria.skill.list", "Installed skills (instruction documents for a kind of task), optionally ranked against a task description.",
		map[string]any{"query": map[string]any{"type": "string", "description": "Task description to rank skills against. Omit to list all installed skills."}}),
	readOnlyTool("aria.skill.get", "One skill's full instructions, or one of its reference documents, so the calling model can follow it.",
		map[string]any{
			"name":      map[string]any{"type": "string", "description": "Skill name from aria.skill.list."},
			"reference": map[string]any{"type": "string", "description": "Optional reference file name from the skill's references/ directory."},
		}, "name"),
	readOnlyTool("aria.world.simulate", "Pre-flight world model simulation for candidate file changes: computes dependency blast radius, affected tests, verification commands, checkpoint status, and empirical success rate.",
		map[string]any{
			"paths": map[string]any{"type": "array", "items": map[string]any{"type": "string"}, "description": "Candidate file paths to evaluate before editing."},
			"root":  rootProperty,
		}, "paths"),
	readOnlyTool("aria.quant.price_option", "Deterministic Black-Scholes / American CRR option pricing, full Greeks (Delta, Gamma, Vega, Theta, Rho, Vanna, Volga), and implied volatility.",
		map[string]any{
			"s":            map[string]any{"type": "number", "description": "Spot price (> 0)."},
			"k":            map[string]any{"type": "number", "description": "Strike price (> 0)."},
			"t":            map[string]any{"type": "number", "description": "Time to maturity in years (> 0)."},
			"r":            map[string]any{"type": "number", "description": "Risk-free rate (decimal, e.g. 0.05)."},
			"q":            map[string]any{"type": "number", "description": "Dividend yield (decimal, default 0)."},
			"sigma":        map[string]any{"type": "number", "description": "Annualized volatility (decimal, default 0.20)."},
			"is_call":      map[string]any{"type": "boolean", "description": "True for Call, False for Put (default true)."},
			"american":     map[string]any{"type": "boolean", "description": "Also compute American option price via 500-step CRR tree."},
			"market_price": map[string]any{"type": "number", "description": "Optional observed option price to solve implied volatility."},
		}, "s", "k", "t"),
}

// mcpProjectRoot is an existing directory under the server's working
// directory or the user's home, never an arbitrary path.
func mcpProjectRoot(value any) (string, error) {
	text, _ := value.(string)
	cwd, err := os.Getwd()
	if err != nil {
		return "", err
	}
	root := cwd
	if strings.TrimSpace(text) != "" {
		// Abs, not Join: on Windows "/" or "\\x" is rooted at the current
		// drive, not relative to the working directory.
		abs, err := filepath.Abs(expandUser(text))
		if err != nil {
			return "", err
		}
		root = abs
	}
	if resolved, err := filepath.EvalSymlinks(root); err == nil {
		root = resolved
	}
	if info, err := os.Stat(root); err != nil || !info.IsDir() {
		return "", fmt.Errorf("root is not a directory: %s", root)
	}
	allowed := []string{cwd}
	if home, err := os.UserHomeDir(); err == nil {
		allowed = append(allowed, home)
	}
	for _, base := range allowed {
		if resolved, err := filepath.EvalSymlinks(base); err == nil {
			base = resolved
		}
		if insideRoot(root, base) {
			return root, nil
		}
	}
	return "", fmt.Errorf("root must be inside the server's working directory or your home directory")
}

func mcpRiskView(a RiskAssessment) map[string]any {
	return map[string]any{"success": true, "assessment": a,
		"approval": map[string]any{"manual": approvalRequirement(a, "manual", nil), "risk_mode": approvalRequirement(a, "risk", nil)},
		"card":     riskCard(a)}
}

func mcpFail(message string) map[string]any {
	return map[string]any{"success": false, "error": message}
}

func mcpRiskAssess(_ context.Context, args map[string]any) map[string]any {
	if command, _ := args["command"].(string); strings.TrimSpace(command) != "" {
		return mcpRiskView(AssessCommand(command))
	}
	tool, _ := args["tool"].(string)
	if strings.TrimSpace(tool) == "" {
		return mcpFail("give a `command`, or a `tool` and its `params`")
	}
	params, _ := args["params"].(map[string]any)
	root, err := mcpProjectRoot(args["root"])
	if err != nil {
		return mcpFail(err.Error())
	}
	return mcpRiskView(AssessTool(tool, params, root))
}

func mcpContractCheck(_ context.Context, args map[string]any) map[string]any {
	tool, _ := args["tool"].(string)
	if strings.TrimSpace(tool) == "" {
		return mcpFail("`tool` is required")
	}
	params, _ := args["params"].(map[string]any)
	root, err := mcpProjectRoot(args["root"])
	if err != nil {
		return mcpFail(err.Error())
	}
	contract, err := LoadContract(root)
	if err != nil {
		// A policy that cannot be read bounds the project to reads, as it does for Aria itself.
		contract = failClosedContract(root, err.Error())
	}
	if contract == nil {
		return map[string]any{"success": true, "contract": nil, "allowed": true,
			"note": "this project declares no change contract (.aria/policy.yaml)"}
	}
	verdict := contract.Check(tool, params)
	return map[string]any{"success": true, "allowed": verdict.Allowed, "rule": verdict.Rule, "reason": verdict.Reason,
		"level": verdict.Assessment.Level, "contract": contract.Render()}
}

func mcpProjectImpact(ctx context.Context, args map[string]any) map[string]any {
	raw, _ := args["targets"].([]any)
	var targets []string
	for _, item := range raw {
		if text, ok := item.(string); ok && text != "" {
			targets = append(targets, text)
		}
	}
	if len(targets) == 0 || len(targets) != len(raw) {
		return mcpFail("`targets` must be a non-empty list of paths or symbols")
	}
	if len(targets) > 20 {
		targets = targets[:20]
	}
	root, err := mcpProjectRoot(args["root"])
	if err != nil {
		return mcpFail(err.Error())
	}
	graph, err := buildProjectGraph(ctx, root)
	if err != nil {
		return mcpFail(err.Error())
	}
	impact := graph.impact(targets)
	return map[string]any{"success": true, "breadth": impact.breadth(), "text": impact.render(40),
		"impact": map[string]any{"targets": nonNil(impact.targets), "unresolved": nonNil(impact.unresolved),
			"direct": nonNil(impact.direct), "indirect": nonNil(impact.indirect), "tests": nonNil(impact.tests),
			"services": nonNil(impact.services)}}
}

func mcpProjectCommands(_ context.Context, args map[string]any) map[string]any {
	root, err := mcpProjectRoot(args["root"])
	if err != nil {
		return mcpFail(err.Error())
	}
	return map[string]any{"success": true, "root": root, "commands": detectProjectCommands(root)}
}

func mcpSkills() ([]skill, error) {
	state, err := stateRoot()
	if err != nil {
		return nil, err
	}
	cwd, err := os.Getwd()
	if err != nil {
		return nil, err
	}
	return discoverSkills(state, cwd), nil
}

func mcpSkillSummary(s skill) map[string]any {
	return map[string]any{"name": s.name, "description": s.description, "source": filepath.Dir(filepath.Dir(s.path))}
}

// rankSkills keeps skills whose name or description shares a word of three
// or more letters with the query, most shared words first.
func rankSkills(query string, skills []skill) []skill {
	words := map[string]bool{}
	for _, w := range strings.FieldsFunc(strings.ToLower(query), func(r rune) bool { return !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9') }) {
		if len(w) >= 3 {
			words[w] = true
		}
	}
	type scored struct {
		s     skill
		score int
	}
	var hits []scored
	for _, s := range skills {
		text := strings.ToLower(s.name + " " + strings.ReplaceAll(s.name, "-", " ") + " " + s.description)
		score := 0
		for w := range words {
			if strings.Contains(text, w) {
				score++
			}
		}
		if score > 0 {
			hits = append(hits, scored{s, score})
		}
	}
	sort.SliceStable(hits, func(i, j int) bool { return hits[i].score > hits[j].score })
	out := make([]skill, len(hits))
	for i, h := range hits {
		out[i] = h.s
	}
	return out
}

func mcpSkillList(_ context.Context, args map[string]any) map[string]any {
	skills, err := mcpSkills()
	if err != nil {
		return mcpFail(err.Error())
	}
	query, _ := args["query"].(string)
	query = strings.TrimSpace(query)
	chosen, matched := skills, false
	if query != "" {
		// An off-topic query lists everything rather than claiming no skills exist.
		if ranked := rankSkills(query, skills); len(ranked) > 0 {
			chosen, matched = ranked, true
		}
	}
	rows := make([]map[string]any, 0, len(chosen))
	for _, s := range chosen {
		rows = append(rows, mcpSkillSummary(s))
	}
	return map[string]any{"success": true, "query": query, "matched_by_relevance": matched, "skills": rows}
}

func skillReferences(s skill) []string {
	paths, _ := filepath.Glob(filepath.Join(filepath.Dir(s.path), "references", "*.md"))
	names := make([]string, 0, len(paths))
	for _, p := range paths {
		names = append(names, filepath.Base(p))
	}
	sort.Strings(names)
	return names
}

func mcpSkillGet(_ context.Context, args map[string]any) map[string]any {
	name, _ := args["name"].(string)
	name = strings.TrimSpace(name)
	if name == "" {
		return mcpFail("name is required")
	}
	skills, err := mcpSkills()
	if err != nil {
		return mcpFail(err.Error())
	}
	var match *skill
	available := []string{}
	for i := range skills {
		available = append(available, skills[i].name)
		if skills[i].name == name || strings.HasSuffix(name, ":"+skills[i].name) {
			match = &skills[i]
		}
	}
	if match == nil {
		return map[string]any{"success": false, "error": fmt.Sprintf("No skill named %q. Call aria.skill.list to see installed skills.", name), "available": available}
	}
	references := skillReferences(*match)
	result := mcpSkillSummary(*match)
	result["success"] = true
	requested, _ := args["reference"].(string)
	if requested = strings.TrimSpace(requested); requested != "" {
		// The name is caller-controlled and becomes a path: it must resolve,
		// symlinks included, to a file inside this skill's references/.
		dir, err := filepath.EvalSymlinks(filepath.Join(filepath.Dir(match.path), "references"))
		target, err2 := filepath.EvalSymlinks(filepath.Join(filepath.Dir(match.path), "references", requested))
		info, err3 := os.Stat(target)
		if err != nil || err2 != nil || err3 != nil || !info.Mode().IsRegular() || filepath.Dir(target) != dir {
			return map[string]any{"success": false, "error": fmt.Sprintf("No reference %q in skill %q.", requested, match.name), "references": references}
		}
		data, err := os.ReadFile(target)
		if err != nil || len(data) > maxSkillChars {
			return mcpFail("the reference could not be read")
		}
		result["reference"], result["content"] = requested, string(data)
		return result
	}
	result["instructions"], result["references"] = match.instructions, references
	return result
}

// writeTool makes a new file and changes nothing that exists.
func writeTool(name, description string, properties map[string]any, required ...string) mcpServedTool {
	t := readOnlyTool(name, description, properties, required...)
	t.Annotations = map[string]any{"readOnlyHint": false, "destructiveHint": false, "idempotentHint": false, "openWorldHint": false}
	return t
}

var (
	inputPathProp  = map[string]any{"type": "string", "description": "Video file under the working directory or your home directory."}
	outputPathProp = map[string]any{"type": "string", "description": "Optional new file to write (never overwritten); default: a new file under Aria's artifacts/video."}
)

func init() {
	mcpServerTools = append(mcpServerTools,
		readOnlyTool("aria.video.probe", "Duration, size and streams (codec, resolution, frame rate) of a video, via ffprobe.",
			map[string]any{"input_path": inputPathProp}, "input_path"),
		readOnlyTool("aria.video.detect_scenes", "Scene-cut timestamps in seconds, from ffmpeg's scene-change score.",
			map[string]any{"input_path": inputPathProp, "threshold": map[string]any{"type": "number", "description": "0..100, default 30; higher finds fewer, surer cuts."}}, "input_path"),
		writeTool("aria.video.trim", "Cut start..end seconds out of a video into a new file (frame-accurate re-encode).",
			map[string]any{"input_path": inputPathProp, "start": map[string]any{"type": "number"}, "end": map[string]any{"type": "number"}, "output_path": outputPathProp},
			"input_path", "start", "end"),
		writeTool("aria.video.concat", "Join 2..50 videos in order into a new file, re-encoding so different codecs and sizes still join.",
			map[string]any{"input_paths": map[string]any{"type": "array", "items": map[string]any{"type": "string"}}, "output_path": outputPathProp}, "input_paths"),
		writeTool("aria.video.overlay_text", "Burn a caption (CJK included) onto a video at the top, centre or bottom, into a new file.",
			map[string]any{"input_path": inputPathProp, "text": map[string]any{"type": "string"},
				"position":  map[string]any{"type": "string", "enum": []string{"top", "center", "bottom"}},
				"font_size": map[string]any{"type": "number"}, "font_color": map[string]any{"type": "string", "description": "white, black, yellow, red, green, blue or #RRGGBB"},
				"output_path": outputPathProp}, "input_path", "text"),
		writeTool("aria.video.overlay_audio", "Mix an audio track under a video's sound, or replace it, into a new file.",
			map[string]any{"input_path": inputPathProp, "audio_path": map[string]any{"type": "string"}, "replace": map[string]any{"type": "boolean"}, "output_path": outputPathProp},
			"input_path", "audio_path"),
		writeTool("aria.video.convert", "Change a video's container (mp4, mov, mkv, webm, avi) and/or crop it to fill an aspect such as 9:16, into a new file.",
			map[string]any{"input_path": inputPathProp, "output_format": map[string]any{"type": "string"}, "aspect": map[string]any{"type": "string"}, "output_path": outputPathProp},
			"input_path"),
		writeTool("aria.video.change_speed", "Speed a video up (factor > 1) or slow it down (factor < 1), pitch-corrected, into a new file.",
			map[string]any{"input_path": inputPathProp, "factor": map[string]any{"type": "number"}, "output_path": outputPathProp}, "input_path", "factor"),
	)
	for name, handler := range map[string]func(context.Context, map[string]any) map[string]any{
		"aria.video.probe": mcpVideoProbe, "aria.video.detect_scenes": mcpVideoDetectScenes, "aria.video.trim": mcpVideoTrim,
		"aria.video.concat": mcpVideoConcat, "aria.video.overlay_text": mcpVideoOverlayText, "aria.video.overlay_audio": mcpVideoOverlayAudio,
		"aria.video.convert": mcpVideoConvert, "aria.video.change_speed": mcpVideoChangeSpeed,
	} {
		mcpServerHandlers[name] = handler
	}
}

func mcpWorldSimulate(ctx context.Context, args map[string]any) map[string]any {
	root, err := mcpProjectRoot(args["root"])
	if err != nil {
		return mcpFail(err.Error())
	}
	res := simulateActionTool(ctx, &scope{workspace: root}, args)
	if !res.Success {
		return mcpFail(res.Error)
	}
	return map[string]any{"success": true, "simulation": res.Data}
}

func mcpQuantPriceOption(ctx context.Context, args map[string]any) map[string]any {
	res := priceOptionTool(ctx, args)
	if !res.Success {
		return mcpFail(res.Error)
	}
	return map[string]any{"success": true, "pricing": res.Data}
}

var mcpServerHandlers = map[string]func(context.Context, map[string]any) map[string]any{
	"aria.risk.assess":        mcpRiskAssess,
	"aria.contract.check":     mcpContractCheck,
	"aria.project.impact":     mcpProjectImpact,
	"aria.project.commands":   mcpProjectCommands,
	"aria.skill.list":         mcpSkillList,
	"aria.skill.get":          mcpSkillGet,
	"aria.world.simulate":     mcpWorldSimulate,
	"aria.quant.price_option": mcpQuantPriceOption,
}

func mcpReply(out io.Writer, id any, result any, rpcErr map[string]any) error {
	message := map[string]any{"jsonrpc": "2.0", "id": id}
	if rpcErr != nil {
		message["error"] = rpcErr
	} else {
		message["result"] = result
	}
	body, err := json.Marshal(message)
	if err != nil {
		return err
	}
	_, err = out.Write(append(body, '\n'))
	return err
}

// mcpServe answers JSON-RPC requests, one per line, until stdin closes.
func mcpServe(ctx context.Context, in io.Reader, out, logs io.Writer) error {
	logger := log.New(logs, "aria-runtime mcp: ", 0)
	scanner := bufio.NewScanner(in)
	scanner.Buffer(make([]byte, 64<<10), 4<<20)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var req struct {
			ID     any             `json:"id"`
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
		}
		if err := json.Unmarshal([]byte(line), &req); err != nil {
			if err := mcpReply(out, nil, nil, map[string]any{"code": -32700, "message": "Parse error"}); err != nil {
				return err
			}
			continue
		}
		notification := req.ID == nil
		var err error
		switch req.Method {
		case "initialize":
			err = mcpReply(out, req.ID, map[string]any{"protocolVersion": mcpServerProtocol,
				"capabilities": map[string]any{"tools": map[string]any{"listChanged": false}},
				"serverInfo":   map[string]any{"name": "aria-code", "version": version}}, nil)
		case "notifications/initialized", "notifications/cancelled":
		case "ping":
			err = mcpReply(out, req.ID, map[string]any{}, nil)
		case "tools/list":
			err = mcpReply(out, req.ID, map[string]any{"tools": mcpServerTools}, nil)
		case "tools/call":
			var call struct {
				Name      string         `json:"name"`
				Arguments map[string]any `json:"arguments"`
			}
			if json.Unmarshal(req.Params, &call) != nil {
				err = mcpReply(out, req.ID, nil, map[string]any{"code": -32602, "message": "Invalid params"})
				break
			}
			handler, ok := mcpServerHandlers[call.Name]
			if !ok {
				err = mcpReply(out, req.ID, nil, map[string]any{"code": -32601, "message": "Unknown or non-exposed tool: " + call.Name})
				break
			}
			if call.Arguments == nil {
				call.Arguments = map[string]any{}
			}
			result, failed := mcpCall(ctx, handler, call.Arguments, logger, call.Name)
			if failed {
				err = mcpReply(out, req.ID, nil, map[string]any{"code": -32603,
					"message": "Tool error: " + call.Name + " failed; details are in the server's log"})
				break
			}
			text, _ := json.Marshal(result)
			err = mcpReply(out, req.ID, map[string]any{"content": []any{map[string]any{"type": "text", "text": string(text)}},
				"isError": result["success"] == false}, nil)
		default:
			if !notification {
				err = mcpReply(out, req.ID, nil, map[string]any{"code": -32601, "message": "Method not found: " + req.Method})
			}
		}
		if err != nil {
			return err
		}
	}
	return scanner.Err()
}

// mcpCall keeps a panicking tool from taking the server down; its details go
// to the log, never to the client, which is another program's model.
func mcpCall(ctx context.Context, handler func(context.Context, map[string]any) map[string]any, args map[string]any, logger *log.Logger, name string) (result map[string]any, failed bool) {
	defer func() {
		if r := recover(); r != nil {
			logger.Printf("tool %s failed: %v", name, r)
			result, failed = nil, true
		}
	}()
	return handler(ctx, args), false
}

func mcpServerCommand(args []string, in io.Reader, out, logs io.Writer) int {
	if len(args) == 1 && (args[0] == "--version" || args[0] == "-V") {
		fmt.Fprintln(out, "aria-runtime mcp", version)
		return 0
	}
	if len(args) != 0 {
		fmt.Fprintln(logs, "usage: aria-runtime mcp   (speaks MCP over stdio)")
		return 2
	}
	if err := mcpServe(context.Background(), in, out, logs); err != nil {
		fmt.Fprintln(logs, err)
		return 1
	}
	return 0
}

// detectProjectCommands is engineering_cmds.detect_commands: the project's own
// test, build and lint commands from what it has, first match per kind; what
// .aria/project.yaml declares wins. Nothing is guessed for an undeclared kind.
func detectProjectCommands(root string) map[string]string {
	found := map[string]string{}
	set := func(kind, command string) {
		if _, ok := found[kind]; !ok {
			found[kind] = command
		}
	}
	isFile := func(name string) bool {
		info, err := os.Stat(filepath.Join(root, name))
		return err == nil && info.Mode().IsRegular()
	}
	isDir := func(name string) bool {
		info, err := os.Stat(filepath.Join(root, name))
		return err == nil && info.IsDir()
	}
	ros := isFile("package.xml")
	if !ros && isDir("src") {
		one, _ := filepath.Glob(filepath.Join(root, "src", "*", "package.xml"))
		two, _ := filepath.Glob(filepath.Join(root, "src", "*", "*", "package.xml"))
		ros = len(one)+len(two) > 0
	}
	if ros {
		set("build", "colcon build")
		set("test", "colcon test")
		found["test_report"] = "colcon test-result --verbose"
	}
	if isFile("package.json") {
		scripts := packageScripts(root)
		pm := "npm"
		if isFile("pnpm-lock.yaml") {
			pm = "pnpm"
		} else if isFile("yarn.lock") {
			pm = "yarn"
		}
		if _, ok := scripts["test"]; ok {
			set("test", pm+" test")
		}
		if _, ok := scripts["build"]; ok {
			set("build", pm+" run build")
		}
		if _, ok := scripts["lint"]; ok {
			set("lint", pm+" run lint")
		}
	}
	if isFile("Cargo.toml") {
		set("test", "cargo test")
		set("build", "cargo build")
		set("lint", "cargo clippy --all-targets")
	}
	if isFile("go.mod") {
		set("test", "go test ./...")
		set("build", "go build ./...")
		set("lint", "go vet ./...")
	}
	pyproject := ""
	if data, err := os.ReadFile(filepath.Join(root, "pyproject.toml")); err == nil {
		pyproject = string(data)
	}
	if pyproject != "" || isFile("setup.py") || isFile("pytest.ini") || isDir("tests") {
		if strings.Contains(pyproject, "pytest") || isFile("pytest.ini") || isDir("tests") {
			set("test", "python3 -m pytest -q")
		}
		if strings.Contains(pyproject, "[build-system]") {
			set("build", "python3 -m build")
		}
		if strings.Contains(pyproject, "ruff") || isFile("ruff.toml") {
			set("lint", "ruff check .")
		}
	}
	for _, name := range []string{"Makefile", "makefile", "GNUmakefile"} {
		data, err := os.ReadFile(filepath.Join(root, name))
		if err != nil {
			continue
		}
		targets := map[string]bool{}
		for _, m := range makeTarget.FindAllStringSubmatch(string(data), -1) {
			targets[m[1]] = true
		}
		for _, kind := range []string{"test", "build", "lint"} {
			if targets[kind] {
				set(kind, "make "+kind)
			}
		}
		break
	}
	declared := declaredProjectCommands(root)
	for kind, command := range declared {
		found[kind] = command
	}
	if _, ok := declared["test"]; ok {
		delete(found, "test_report") // the project's own test command reports for itself
	}
	return found
}

var makeTarget = regexp.MustCompile(`(?m)^([A-Za-z0-9_.-]+):`)

// declaredProjectCommands reads `commands:` from .aria/project.yaml (test,
// build, lint and sim; sim is never detected).
func declaredProjectCommands(root string) map[string]string {
	data, err := os.ReadFile(filepath.Join(root, ".aria", "project.yaml"))
	if err != nil {
		return nil
	}
	var doc struct {
		Commands map[string]any `yaml:"commands"`
	}
	if yaml.Unmarshal(data, &doc) != nil {
		return nil
	}
	out := map[string]string{}
	for _, kind := range []string{"test", "build", "lint", "sim"} {
		if text, ok := doc.Commands[kind].(string); ok && strings.TrimSpace(text) != "" {
			out[kind] = strings.TrimSpace(text)
		}
	}
	return out
}
