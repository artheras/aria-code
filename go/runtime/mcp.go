package main

// A Model Context Protocol client for stdio servers, ported from
// src/aria_code/mcp_client.py. Servers come from <state>/mcp_servers.json,
// the file the Python client reads:
//
//	{"servers": [{"name": "fs", "command": "npx", "args": ["-y", "server"],
//	              "env": {"TOKEN": "${TOKEN}"}, "enabled": true, "timeout": 30}]}
//
// Each server's tools reach the model as mcp__<server>__<tool>. A call is
// approved like a file write unless the server marks the tool readOnlyHint,
// and it is refused in a read-only turn.

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"google.golang.org/genai"
)

const mcpProtocolVersion = "2024-11-05"

type mcpServerConfig struct {
	Name        string            `json:"name"`
	Command     string            `json:"command"`
	Args        []string          `json:"args"`
	Env         map[string]string `json:"env"`
	Enabled     *bool             `json:"enabled"`
	Description string            `json:"description"`
	Timeout     float64           `json:"timeout"`
}

type mcpTool struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
	Annotations map[string]any `json:"annotations"`
}

func (t mcpTool) readOnly() bool {
	hint, _ := t.Annotations["readOnlyHint"].(bool)
	return hint
}

var unsafeToolChars = regexp.MustCompile(`[^A-Za-z0-9_-]+`)

// mcpToolName is model_safe_tool_name: providers reject slashes in names.
func mcpToolName(server, tool string) string {
	s := strings.Trim(unsafeToolChars.ReplaceAllString(server, "_"), "_")
	t := strings.Trim(unsafeToolChars.ReplaceAllString(tool, "_"), "_")
	if s == "" {
		s = "server"
	}
	if t == "" {
		t = "tool"
	}
	return "mcp__" + s + "__" + t
}

func loadMCPConfig(root string) ([]mcpServerConfig, error) {
	var file struct {
		Servers []mcpServerConfig `json:"servers"`
	}
	if err := readJSON(filepath.Join(root, "mcp_servers.json"), &file); err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("mcp_servers.json: %w", err)
	}
	return file.Servers, nil
}

type rpcMessage struct {
	ID     *int64          `json:"id,omitempty"`
	Method string          `json:"method,omitempty"`
	Result json.RawMessage `json:"result,omitempty"`
	Error  *struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	} `json:"error,omitempty"`
}

type mcpClient struct {
	cfg     mcpServerConfig
	cmd     *exec.Cmd
	stdin   io.WriteCloser
	mu      sync.Mutex
	pending map[int64]chan rpcMessage
	next    int64
	done    chan struct{}
	stderr  *outputTail
	tools   []mcpTool
}

func (c *mcpClient) timeout() time.Duration {
	if c.cfg.Timeout > 0 {
		return time.Duration(c.cfg.Timeout * float64(time.Second))
	}
	return 30 * time.Second
}

func startMCP(ctx context.Context, cfg mcpServerConfig) (*mcpClient, error) {
	if strings.TrimSpace(cfg.Command) == "" {
		return nil, errors.New("no command")
	}
	expand := func(value string) string { return os.Expand(value, os.Getenv) }
	args := make([]string, len(cfg.Args))
	for i, arg := range cfg.Args {
		args[i] = expand(arg)
	}
	cmd := exec.Command(expand(cfg.Command), args...)
	cmd.Env = os.Environ()
	for key, value := range cfg.Env {
		cmd.Env = append(cmd.Env, key+"="+expand(value))
	}
	c := &mcpClient{cfg: cfg, cmd: cmd, pending: map[int64]chan rpcMessage{}, done: make(chan struct{}), stderr: &outputTail{limit: 4000}}
	cmd.Stderr = c.stderr
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	c.stdin = stdin
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	go c.read(stdout)
	ctx, cancel := context.WithTimeout(ctx, c.timeout())
	defer cancel()
	if _, err := c.request(ctx, "initialize", map[string]any{
		"protocolVersion": mcpProtocolVersion, "capabilities": map[string]any{},
		"clientInfo": map[string]any{"name": "aria", "version": version},
	}); err != nil {
		c.close()
		return nil, c.explain(err)
	}
	if err := c.send(map[string]any{"jsonrpc": "2.0", "method": "notifications/initialized"}); err != nil {
		c.close()
		return nil, err
	}
	raw, err := c.request(ctx, "tools/list", map[string]any{})
	if err != nil {
		c.close()
		return nil, c.explain(err)
	}
	var listed struct {
		Tools []mcpTool `json:"tools"`
	}
	if err := json.Unmarshal(raw, &listed); err != nil {
		c.close()
		return nil, fmt.Errorf("tools/list: %w", err)
	}
	c.tools = listed.Tools
	return c, nil
}

// explain adds what the server printed on stderr to a failure.
func (c *mcpClient) explain(err error) error {
	if text, _ := c.stderr.text(); strings.TrimSpace(text) != "" {
		return fmt.Errorf("%w: %s", err, capText(strings.TrimSpace(text), 500))
	}
	return err
}

func (c *mcpClient) read(stdout io.Reader) {
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 64<<10), 24<<20)
	for scanner.Scan() {
		var m rpcMessage
		if json.Unmarshal(scanner.Bytes(), &m) != nil || m.ID == nil {
			continue
		}
		if m.Method != "" {
			c.send(map[string]any{"jsonrpc": "2.0", "id": *m.ID, "result": map[string]any{}})
			continue
		}
		c.mu.Lock()
		ch := c.pending[*m.ID]
		delete(c.pending, *m.ID)
		c.mu.Unlock()
		if ch != nil {
			ch <- m
		}
	}
	if scanner.Err() != nil && c.cmd.Process != nil {
		_ = c.cmd.Process.Kill()
	}
	c.cmd.Wait()
	c.closeOnce()
}

func (c *mcpClient) send(message map[string]any) error {
	body, err := json.Marshal(message)
	if err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	_, err = c.stdin.Write(append(body, '\n'))
	return err
}

func (c *mcpClient) request(ctx context.Context, method string, params any) (json.RawMessage, error) {
	c.mu.Lock()
	c.next++
	id := c.next
	ch := make(chan rpcMessage, 1)
	c.pending[id] = ch
	c.mu.Unlock()
	if err := c.send(map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params}); err != nil {
		return nil, fmt.Errorf("server not reachable: %w", err)
	}
	select {
	case m := <-ch:
		if m.Error != nil {
			return nil, fmt.Errorf("%s: %s (code %d)", method, m.Error.Message, m.Error.Code)
		}
		return m.Result, nil
	case <-c.done:
		return nil, errors.New("server exited")
	case <-ctx.Done():
		c.mu.Lock()
		delete(c.pending, id)
		c.mu.Unlock()
		return nil, fmt.Errorf("%s: %w", method, ctx.Err())
	}
}

func (c *mcpClient) closeOnce() {
	c.mu.Lock()
	defer c.mu.Unlock()
	select {
	case <-c.done:
	default:
		close(c.done)
	}
}

func (c *mcpClient) close() {
	c.stdin.Close()
	select {
	case <-c.done:
	case <-time.After(2 * time.Second):
		if c.cmd.Process != nil {
			c.cmd.Process.Kill()
		}
	}
	c.closeOnce()
}

func (c *mcpClient) alive() bool {
	select {
	case <-c.done:
		return false
	default:
		return true
	}
}

// mcpManager starts the configured servers on first use and keeps them.
type mcpManager struct {
	root    string
	mu      sync.Mutex
	loaded  bool
	clients map[string]*mcpClient
	configs []mcpServerConfig
	failed  map[string]string
}

func newMCPManager(root string) *mcpManager {
	return &mcpManager{root: root, clients: map[string]*mcpClient{}, failed: map[string]string{}}
}

func (m *mcpManager) ensure(ctx context.Context) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.loaded {
		return
	}
	m.loaded = true
	configs, err := loadMCPConfig(m.root)
	if err != nil {
		m.failed["mcp_servers.json"] = err.Error()
		return
	}
	m.configs = configs
	if remoteScope() {
		for _, cfg := range configs {
			m.failed[cfg.Name] = "local MCP servers do not run in a remote session"
		}
		return
	}
	for _, cfg := range configs {
		if cfg.Enabled != nil && !*cfg.Enabled {
			continue
		}
		if cfg.Name == "" {
			continue
		}
		client, err := startMCP(ctx, cfg)
		if err != nil {
			m.failed[cfg.Name] = err.Error()
			continue
		}
		m.clients[cfg.Name] = client
	}
}

func (m *mcpManager) closeAll() {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, c := range m.clients {
		c.close()
	}
	m.clients, m.failed, m.loaded = map[string]*mcpClient{}, map[string]string{}, false
}

// lookup is the server and tool behind a model-facing name.
func (m *mcpManager) lookup(name string) (*mcpClient, mcpTool, bool) {
	if !strings.HasPrefix(name, "mcp__") {
		return nil, mcpTool{}, false
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	for server, c := range m.clients {
		for _, t := range c.tools {
			if mcpToolName(server, t.Name) == name {
				return c, t, true
			}
		}
	}
	return nil, mcpTool{}, false
}

func (m *mcpManager) definition(name string) (definition, bool) {
	_, tool, ok := m.lookup(name)
	if !ok {
		return definition{}, false
	}
	return definition{name: name, description: tool.Description, mutate: !tool.readOnly()}, true
}

func (m *mcpManager) declarations() []*genai.FunctionDeclaration {
	m.mu.Lock()
	defer m.mu.Unlock()
	names := make([]string, 0, len(m.clients))
	for name := range m.clients {
		names = append(names, name)
	}
	sort.Strings(names)
	out := []*genai.FunctionDeclaration{}
	for _, server := range names {
		for _, t := range m.clients[server].tools {
			schema := map[string]any{"type": "object", "properties": map[string]any{}}
			if t.InputSchema != nil {
				schema = map[string]any{}
				for key, value := range t.InputSchema {
					if key != "$schema" {
						schema[key] = value
					}
				}
			}
			out = append(out, &genai.FunctionDeclaration{
				Name:                 mcpToolName(server, t.Name),
				Description:          capText("[MCP "+server+"] "+t.Description, 1024),
				ParametersJsonSchema: schema,
			})
		}
	}
	return out
}

func (m *mcpManager) call(ctx context.Context, name string, args map[string]any) toolResult {
	c, tool, ok := m.lookup(name)
	if !ok {
		return failed(fmt.Errorf("MCP tool %s is not available", name))
	}
	if !c.alive() {
		return failed(fmt.Errorf("MCP server %s has exited; /mcp reload restarts it", c.cfg.Name))
	}
	if args == nil {
		args = map[string]any{}
	}
	ctx, cancel := context.WithTimeout(ctx, c.timeout())
	defer cancel()
	raw, err := c.request(ctx, "tools/call", map[string]any{"name": tool.Name, "arguments": args})
	if err != nil {
		return failed(c.explain(err))
	}
	return decodeMCPToolResult(raw, c.cfg.Name, tool.Name)
}

// Image bytes are separate from JSON tool results and diagnostic events.
// Only bounded, decodable raster images are forwarded to the model.
func decodeMCPToolResult(raw []byte, server, tool string) toolResult {
	var result struct {
		Content []struct {
			Type     string `json:"type"`
			Text     string `json:"text"`
			Data     string `json:"data"`
			MIMEType string `json:"mimeType"`
		} `json:"content"`
		IsError bool `json:"isError"`
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		return failed(fmt.Errorf("tools/call: %w", err))
	}
	var text strings.Builder
	var images []toolImage
	total := 0
	for _, part := range result.Content {
		switch part.Type {
		case "text":
			text.WriteString(part.Text)
		case "image":
			if result.IsError {
				continue
			}
			if len(images) >= 4 || len(part.Data) > 20_000_000 {
				return failed(errors.New("MCP image limit exceeded"))
			}
			data, err := base64.StdEncoding.DecodeString(part.Data)
			total += len(data)
			if err != nil || total > 15_000_000 {
				return failed(errors.New("Invalid or oversized MCP image"))
			}
			cfg, format, err := image.DecodeConfig(bytes.NewReader(data))
			mime := map[string]string{"png": "image/png", "jpeg": "image/jpeg"}[format]
			if err != nil || mime == "" || mime != part.MIMEType || cfg.Width <= 0 || cfg.Height <= 0 || cfg.Width > 8192 || cfg.Height > 8192 || int64(cfg.Width)*int64(cfg.Height) > 16_777_216 {
				return failed(errors.New("Unsupported or invalid MCP image"))
			}
			images = append(images, toolImage{MIMEType: mime, Data: data})
			text.WriteString("[image attached]")
		default:
			fmt.Fprintf(&text, "[%s content]", part.Type)
		}
	}
	if result.IsError {
		return toolResult{Error: capText(text.String(), 30000), Data: map[string]any{"server": server, "tool": tool}}
	}
	return toolResult{Success: true, Images: images, Data: map[string]any{"server": server, "tool": tool, "content": capText(text.String(), 30000)}}
}

func (m *mcpManager) status() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	if len(m.configs) == 0 && len(m.failed) == 0 {
		return "No MCP servers. Add them to " + filepath.Join(m.root, "mcp_servers.json") + ":\n" +
			`{"servers": [{"name": "fs", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}]}`
	}
	var b strings.Builder
	b.WriteString("MCP servers")
	for _, cfg := range m.configs {
		switch c, ok := m.clients[cfg.Name]; {
		case ok && c.alive():
			fmt.Fprintf(&b, "\n  ● %-16s %d tools", cfg.Name, len(c.tools))
		case ok:
			fmt.Fprintf(&b, "\n  ✗ %-16s exited; /mcp reload", cfg.Name)
		case cfg.Enabled != nil && !*cfg.Enabled:
			fmt.Fprintf(&b, "\n  ○ %-16s disabled", cfg.Name)
		default:
			fmt.Fprintf(&b, "\n  ✗ %-16s %s", cfg.Name, capText(m.failed[cfg.Name], 300))
		}
	}
	if reason, ok := m.failed["mcp_servers.json"]; ok {
		b.WriteString("\n  ✗ " + reason)
	}
	return b.String()
}

func (m *mcpManager) toolList() string {
	declarations := m.declarations()
	if len(declarations) == 0 {
		return "No MCP tools. /mcp shows the servers."
	}
	var b strings.Builder
	b.WriteString("MCP tools")
	for _, d := range declarations {
		access := "write"
		if def, ok := m.definition(d.Name); ok && !def.mutate {
			access = "read"
		}
		fmt.Fprintf(&b, "\n  %-40s %-5s  %s", d.Name, access, capText(oneLine(d.Description), 90))
	}
	return b.String()
}

func (s *server) mcpCommand(ctx context.Context, args []string) (string, error) {
	sub := "status"
	if len(args) > 0 {
		sub = args[0]
	}
	switch sub {
	case "status":
		s.mcp.ensure(ctx)
		return s.mcp.status(), nil
	case "tools":
		s.mcp.ensure(ctx)
		return s.mcp.toolList(), nil
	case "reload":
		s.mcp.closeAll()
		s.mcp.ensure(ctx)
		return "Reloaded.\n" + s.mcp.status(), nil
	}
	return "", errors.New("usage: /mcp [status | tools | reload]")
}
