package main

// Finance commands go to the Arthera financial-service, the separate
// Python MCP server (github.com/artheras/financial-service), through the
// native MCP client: the core keeps no finance code and needs no Python.
//
// /finance shows whether the service is connected and /finance setup
// <path> registers it in mcp_servers.json. /broker reads accounts,
// positions and orders by calling the service's read-only broker tools
// directly. Every other finance command (/quote, /backtest, /ashare …)
// becomes a read-only turn: the model answers it with the service's tools,
// which are all marked readOnlyHint, and cannot write files or run
// commands while doing so.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
)

const financeServer = "arthera-finance"

// financeCommands are the Python worker's finance commands
// (docs/command-migration-matrix.md, "Finance service").
var financeCommands = graphSetOf(strings.Fields(`/accuracy /alert /analyze /ashare /asset-diag /auto-strategy /backtest
/broker /carriers /chart /commodity /compare /compliance /contract-draft /corr /crypto /dashboard /data /datasource
/earnings /edgar /execution /exit-calc /factor-lab /factors /feargreed /football /forex /funding /hot /ichimoku
/indices /inventory /journal /limitup /longterm /macro /market /markets /news /north /ops-report /optimize
/optimize-port /options /orders /paper /peer /plan-report /portfolio /positions /predict /quote /realty /report
/research /revenue-calc /risk /risk-scan /screen /screen-cn /shortterm /signal /sim /stat-arb /strategy /stress
/ta /team /deep /ui /canvas /artifacts /account /cloud /deploy /trade /tv /warehouse /watch`))

// directFinance are answered without the model.
var directFinance = map[string]bool{"/broker": true, "/positions": true, "/orders": true, "/account": true}

// financeTools are the service's tools as the model sees them.
func (s *server) financeTools(ctx context.Context) []string {
	s.mcp.ensure(ctx)
	prefix := mcpToolName(financeServer, "x")
	prefix = strings.TrimSuffix(prefix, "x")
	var names []string
	for _, d := range s.mcp.declarations() {
		if strings.HasPrefix(d.Name, prefix) {
			names = append(names, d.Name)
		}
	}
	sort.Strings(names)
	return names
}

const financeMissing = "Finance commands run on the Arthera financial-service, which is not connected.\n" +
	"Install it (https://github.com/artheras/financial-service), then: /finance setup /absolute/path/to/.venv/bin/arthera-financial-service"

func (s *server) requireFinance(ctx context.Context) ([]string, error) {
	tools := s.financeTools(ctx)
	if len(tools) == 0 {
		return nil, errors.New(financeMissing)
	}
	return tools, nil
}

// financePrompt is the read-only turn for a finance command.
func (s *server) financePrompt(ctx context.Context, name, extra string) (string, error) {
	tools, err := s.requireFinance(ctx)
	if err != nil {
		return "", err
	}
	request := strings.TrimSpace(name + " " + extra)
	return "The user ran the finance command " + request + ".\n\n" +
		"Answer it with the Arthera financial-service tools (" + strings.Join(tools, ", ") + "). " +
		"Use only figures those tools return or the user supplied, cite each tool result's source and as-of date, " +
		"and keep missing values unknown. If the tools cannot answer this request (live quotes, news, order placement " +
		"and A-share screens are not among them), say so plainly and say what would be needed; never invent data. " +
		"This is analysis, not investment advice.", nil
}

// financeConfigured is the command registered for the service, if any.
func (s *server) financeConfigured() (string, bool) {
	configs, err := loadMCPConfig(s.opts.root)
	if err != nil {
		return "", false
	}
	for _, cfg := range configs {
		if cfg.Name == financeServer {
			return cfg.Command, cfg.Enabled == nil || *cfg.Enabled
		}
	}
	return "", false
}

func (s *server) financeCommand(ctx context.Context, args []string) (string, error) {
	if len(args) > 0 && args[0] == "setup" {
		return s.financeSetup(ctx, args[1:])
	}
	if len(args) > 0 {
		return "", errors.New("usage: /finance [setup <path to arthera-financial-service>]")
	}
	command, enabled := s.financeConfigured()
	if command == "" {
		hint := ""
		if path, err := exec.LookPath("arthera-financial-service"); err == nil {
			hint = "\nFound " + path + " on PATH: /finance setup " + path
		}
		return financeMissing + hint, nil
	}
	if !enabled {
		return "The financial-service is registered (" + command + ") but disabled in mcp_servers.json.", nil
	}
	tools := s.financeTools(ctx)
	if len(tools) == 0 {
		return "The financial-service is registered (" + command + ") but did not start.\n" + s.mcp.status(), nil
	}
	return fmt.Sprintf("Finance: connected (%s), %d tools.\n\n/broker [connections|account|positions|orders] [connection] reads broker data directly.\n"+
		"Other finance commands (/quote, /backtest, /analyze, /risk, /edgar, …) are answered by the model with these tools, read-only.\n"+
		"Network data and broker access are off unless the service's own environment enables them (FINANCIAL_SERVICE_NETWORK=1).", command, len(tools)), nil
}

// financeSetup registers the service's console script (or a Python with
// it installed) in mcp_servers.json, keeping every other entry as it is.
func (s *server) financeSetup(ctx context.Context, args []string) (string, error) {
	if len(args) != 1 {
		return "", errors.New("usage: /finance setup <absolute path to arthera-financial-service or the venv's python>")
	}
	path := expandUser(args[0])
	if !filepath.IsAbs(path) {
		return "", errors.New("use the absolute path: a bare name starts whichever copy is first on PATH")
	}
	info, err := os.Stat(path)
	if err != nil || info.IsDir() {
		return "", fmt.Errorf("%s is not a file", path)
	}
	entry := map[string]any{"name": financeServer, "command": path, "args": []string{},
		"description": "Arthera financial-service: statements, SEC facts, risk, backtests, market history, read-only broker data"}
	base := strings.ToLower(strings.TrimSuffix(filepath.Base(path), ".exe"))
	if strings.HasPrefix(base, "python") {
		// -I keeps the workspace off the import path (the service README).
		entry["args"] = []string{"-I", "-m", "arthera_finance.server"}
	}
	file := filepath.Join(s.opts.root, "mcp_servers.json")
	var doc map[string]any
	if err := readJSON(file, &doc); err != nil && !os.IsNotExist(err) {
		return "", fmt.Errorf("mcp_servers.json: %w", err)
	}
	if doc == nil {
		doc = map[string]any{}
	}
	servers, _ := doc["servers"].([]any)
	kept := []any{}
	for _, item := range servers {
		if m, ok := item.(map[string]any); ok && m["name"] == financeServer {
			continue
		}
		kept = append(kept, item)
	}
	doc["servers"] = append(kept, entry)
	if err := atomicJSON(file, doc); err != nil {
		return "", err
	}
	s.mcp.closeAll()
	if tools := s.financeTools(ctx); len(tools) > 0 {
		return fmt.Sprintf("Connected the financial-service: %d tools.", len(tools)), nil
	}
	return "Registered, but the service did not start:\n" + s.mcp.status(), nil
}

// brokerCommand calls the service's read-only broker tools directly.
func (s *server) brokerCommand(ctx context.Context, name string, args []string) (string, error) {
	if _, err := s.requireFinance(ctx); err != nil {
		return "", err
	}
	sub := "connections"
	switch name {
	case "/positions", "/orders", "/account":
		sub = strings.TrimPrefix(name, "/")
	default:
		if len(args) > 0 {
			sub, args = args[0], args[1:]
		}
	}
	tool, params := "", map[string]any{}
	switch sub {
	case "connections":
		tool = "financial_broker_connections"
	case "account", "positions", "orders":
		if len(args) != 1 {
			return "", fmt.Errorf("usage: /broker %s <connection> (/broker connections lists them)", sub)
		}
		tool, params["connection_id"] = "financial_broker_"+sub, args[0]
	default:
		return "", errors.New("usage: /broker [connections | account|positions|orders <connection>]")
	}
	result := s.mcp.call(ctx, mcpToolName(financeServer, tool), params)
	if !result.Success {
		return "", errors.New(result.Error)
	}
	content, _ := result.Data["content"].(string)
	var pretty any
	if json.Unmarshal([]byte(content), &pretty) == nil {
		body, _ := json.MarshalIndent(pretty, "", "  ")
		content = string(body)
	}
	return capText(content, 20000), nil
}
