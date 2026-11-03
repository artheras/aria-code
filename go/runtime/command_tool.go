package main

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"regexp"
	"strings"
	"sync"
	"time"
)

var deviceRedirect = regexp.MustCompile(`>\s*/dev/([^\s;&|]+)`)

type commandSpec struct {
	command, cwd string
	timeout      time.Duration
}

func commandPlan(s *scope, c config, params map[string]any) (commandSpec, error) {
	if err := noHostFlags(params); err != nil {
		return commandSpec{}, err
	}
	for key := range params {
		if key != "command" && key != "cwd" && key != "timeout" {
			return commandSpec{}, fmt.Errorf("unsupported command field: %s", key)
		}
	}
	if remoteScope() || s.readOnly || c.text("permission_mode", "workspace-write") == "read-only" || !c.boolean("network_enabled", true) {
		return commandSpec{}, errors.New("native shell sandbox adapter is required for remote, read-only or network-disabled execution")
	}
	command := str(params, "command", "")
	if strings.TrimSpace(command) == "" || len(command) > 4096 || strings.ContainsRune(command, 0) {
		return commandSpec{}, errors.New("command must contain between 1 and 4096 bytes without NUL")
	}
	for _, pattern := range []string{"rm -rf /", "mkfs", "dd if=", ":(){ :", "fork bomb"} {
		if strings.Contains(command, pattern) {
			return commandSpec{}, errors.New("command is blocked by the existing destructive-operation policy")
		}
	}
	for _, match := range deviceRedirect.FindAllStringSubmatch(command, -1) {
		target := match[1]
		if target != "null" && target != "stdout" && target != "stderr" && target != "tty" && !strings.HasPrefix(target, "fd/") {
			return commandSpec{}, errors.New("device writes are blocked by command policy")
		}
	}
	p, err := s.open(str(params, "cwd", "."), true)
	if err != nil {
		return commandSpec{}, err
	}
	defer p.Close()
	info, err := p.root.Stat(p.relative)
	if err != nil {
		return commandSpec{}, err
	}
	if !info.IsDir() {
		return commandSpec{}, errors.New("command cwd must be a directory")
	}
	seconds := integer(params, "timeout", 120)
	if seconds < 1 {
		return commandSpec{}, errors.New("command timeout must be positive")
	}
	if seconds > 300 {
		seconds = 300
	}
	return commandSpec{command: command, cwd: p.absolute, timeout: time.Duration(seconds) * time.Second}, nil
}

// Drain all process output while keeping bounded tails. A verbose build cannot
// fill memory or block on a full pipe; the model receives explicit truncation.
type outputTail struct {
	mu    sync.Mutex
	bytes []byte
	total int
	limit int
}

func (b *outputTail) Write(data []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.total += len(data)
	if len(data) >= b.limit {
		b.bytes = append(b.bytes[:0], data[len(data)-b.limit:]...)
	} else {
		excess := len(b.bytes) + len(data) - b.limit
		if excess > 0 {
			copy(b.bytes, b.bytes[excess:])
			b.bytes = b.bytes[:len(b.bytes)-excess]
		}
		b.bytes = append(b.bytes, data...)
	}
	return len(data), nil
}
func (b *outputTail) text() (string, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return strings.ToValidUTF8(string(b.bytes), "�"), b.total > b.limit
}
func commandTool(ctx context.Context, spec commandSpec) toolResult {
	if ctx.Err() != nil {
		return failed(ctx.Err())
	}
	bounded, cancel := context.WithTimeout(ctx, spec.timeout)
	defer cancel()
	cmd := shellCommand(bounded, spec.command)
	cmd.Dir = spec.cwd
	cmd.Stdin = nil
	cmd.WaitDelay = time.Second
	stdout, stderr := &outputTail{limit: 65536}, &outputTail{limit: 32768}
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	configureCommandTree(cmd)
	err := cmd.Run()
	stopCommandTree(cmd)
	out, outCut := stdout.text()
	errorText, errCut := stderr.text()
	code := -1
	if cmd.ProcessState != nil {
		code = cmd.ProcessState.ExitCode()
	}
	data := map[string]any{"command": spec.command, "cwd": spec.cwd, "exit_code": code, "stdout": out, "stderr": errorText, "stdout_truncated": outCut, "stderr_truncated": errCut}
	if bounded.Err() != nil {
		return toolResult{Data: data, Error: fmt.Sprintf("command stopped: %v", bounded.Err())}
	}
	if err != nil {
		var exit *exec.ExitError
		if !errors.As(err, &exit) {
			return toolResult{Data: data, Error: "command execution failed: " + err.Error()}
		}
	}
	return toolResult{Success: true, Data: data}
}

func (s *server) userCommand(ctx context.Context, turn, command string) (string, error) {
	return s.userCommandTimeout(ctx, turn, command, 0)
}

// userCommandTimeout is userCommand with a timeout in seconds (0: the
// default).
func (s *server) userCommandTimeout(ctx context.Context, turn, command string, timeout int) (string, error) {
	params := map[string]any{"command": command, "cwd": s.scope.workspace}
	if timeout > 0 {
		params["timeout"] = float64(timeout)
	}
	spec, err := commandPlan(s.scope, s.cfg, params)
	if err != nil {
		return "", err
	}
	if err := s.emit("tool.started", turn, map[string]any{"tool": "run_command", "params": eventParams(params)}); err != nil {
		return "", err
	}
	allowed, reason, err := s.approve(ctx, "run_command", params, fileChange{})
	if err != nil {
		return "", err
	}
	if !allowed {
		s.emit("tool.completed", turn, map[string]any{"tool": "run_command", "success": false, "error": "User declined"})
		return "Command was not executed: " + reason, nil
	}
	started := time.Now()
	result := commandTool(ctx, spec)
	code, _ := result.Data["exit_code"].(int)
	if err := s.emit("tool.completed", turn, map[string]any{"tool": "run_command", "success": result.Success && code == 0, "exit_code": code, "elapsed_s": time.Since(started).Seconds(), "error": result.Error, "output": commandOutput(result.Data)}); err != nil {
		return "", err
	}
	var answer strings.Builder
	fmt.Fprintf(&answer, "Command exit: %d\n", code)
	for _, key := range []string{"stdout", "stderr"} {
		if text, ok := result.Data[key].(string); ok && text != "" {
			fmt.Fprintf(&answer, "\n%s:\n%s", key, text)
			if cut, _ := result.Data[key+"_truncated"].(bool); cut {
				answer.WriteString("\n[output tail; earlier output was truncated]\n")
			}
		}
	}
	if !result.Success {
		return answer.String(), errors.New(result.Error)
	}
	if code != 0 {
		return answer.String(), fmt.Errorf("command exited with code %d", code)
	}
	return answer.String(), nil
}
