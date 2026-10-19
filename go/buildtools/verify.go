package main

// The release smoke test runs the shipped executables, with isolated state and
// no model request. This replaces scripts/verify_native_frontend.py.
import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

type boundedOutput struct {
	buffer   bytes.Buffer
	limit    int
	overflow bool
}

func (b *boundedOutput) Write(p []byte) (int, error) {
	n := len(p)
	remaining := b.limit - b.buffer.Len()
	if n > remaining {
		b.overflow = true
		p = p[:remaining]
	}
	_, _ = b.buffer.Write(p)
	return n, nil
}

func (b *boundedOutput) String() string { return b.buffer.String() }

func verificationEnv(temp string) []string {
	var env []string
	for _, item := range os.Environ() {
		key, _, _ := strings.Cut(item, "=")
		key = strings.ToUpper(key)
		if strings.HasPrefix(key, "ARIA_") || key == "PYTHONPATH" || key == "PYTHONHOME" || key == "HOME" || key == "USERPROFILE" || key == "XDG_CONFIG_HOME" || key == "XDG_DATA_HOME" || key == "APPDATA" || key == "LOCALAPPDATA" {
			continue
		}
		env = append(env, item)
	}
	return append(env, "ARIA_HOME="+filepath.Join(temp, "state"), "HOME="+temp,
		"USERPROFILE="+temp, "XDG_CONFIG_HOME="+temp, "XDG_DATA_HOME="+temp,
		"APPDATA="+temp, "LOCALAPPDATA="+temp, "ARIA_OFFLINE=1", "ARIA_THEME=dark")
}

func verificationCommand(ctx context.Context, cwd string, env []string, binary string, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, binary, args...)
	cmd.Dir, cmd.Env = cwd, env
	cmd.WaitDelay = 2 * time.Second
	return cmd
}

func verificationRun(ctx context.Context, cwd string, env []string, binary string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, time.Minute)
	defer cancel()
	cmd := verificationCommand(ctx, cwd, env, binary, args...)
	out, diagnostic := &boundedOutput{limit: 1 << 20}, &boundedOutput{limit: 4096}
	cmd.Stdout, cmd.Stderr = out, diagnostic
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("%s %v: %w: %s", filepath.Base(binary), args, err, diagnostic.String())
	}
	if out.overflow {
		return "", fmt.Errorf("%s exceeded smoke-test output limit", binary)
	}
	return strings.TrimSpace(out.String()), nil
}

func verificationSession(ctx context.Context, cwd string, env []string, helper string) (map[string]json.RawMessage, error) {
	ctx, cancel := context.WithTimeout(ctx, time.Minute)
	defer cancel()
	cmd := verificationCommand(ctx, cwd, env, helper, "chat", "--jsonl")
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		_ = stdin.Close()
		return nil, err
	}
	diagnostic := &boundedOutput{limit: 4096}
	cmd.Stderr = diagnostic
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	defer stdin.Close()
	// A single reader preserves event order and bounds each JSONL frame.
	events := make(chan map[string]json.RawMessage, 1)
	readDone := make(chan error, 1)
	go func() {
		scanner := bufio.NewScanner(stdout)
		scanner.Buffer(make([]byte, 4096), 1<<20)
		for scanner.Scan() {
			var event map[string]json.RawMessage
			if err := json.Unmarshal(scanner.Bytes(), &event); err != nil {
				readDone <- fmt.Errorf("invalid application JSON: %w", err)
				close(events)
				return
			}
			select {
			case events <- event:
			case <-ctx.Done():
				readDone <- ctx.Err()
				close(events)
				return
			}
		}
		readDone <- scanner.Err()
		close(events)
	}()
	var ready map[string]json.RawMessage
	closed := false
	var failure error
loop:
	for {
		select {
		case <-ctx.Done():
			failure = ctx.Err()
			break loop
		case event, ok := <-events:
			if !ok {
				break loop
			}
			var kind string
			_ = json.Unmarshal(event["type"], &kind)
			if string(event["protocol"]) != "1" {
				failure = fmt.Errorf("invalid application protocol")
				break loop
			}
			switch kind {
			case "session.ready":
				if ready != nil {
					failure = fmt.Errorf("duplicate ready event")
					break loop
				}
				ready = event
				_, failure = io.WriteString(stdin, "{\"type\":\"shutdown\",\"protocol\":1}\n")
				if failure != nil {
					break loop
				}
			case "session.closed":
				if ready == nil || closed {
					failure = fmt.Errorf("unexpected closed event")
					break loop
				}
				closed = true
			case "session.failed", "protocol.error":
				failure = fmt.Errorf("application reported %s", kind)
				break loop
			}
		}
	}
	if failure != nil {
		cancel()
	}
	// Finish reading before Wait closes the pipe; cancellation unblocks the reader.
	readErr := <-readDone
	if readErr != nil {
		cancel()
	}
	waitErr := cmd.Wait()
	if failure != nil {
		return nil, failure
	}
	if readErr != nil {
		return nil, readErr
	}
	if waitErr != nil {
		return nil, fmt.Errorf("worker shutdown: %w: %s", waitErr, diagnostic.String())
	}
	if ready == nil || !closed {
		return nil, fmt.Errorf("worker omitted ready/closed events: %s", diagnostic.String())
	}
	return ready, nil
}

func verifyFrontend(ctx context.Context, directory string, output io.Writer) error {
	root, err := filepath.Abs(directory)
	if err != nil {
		return err
	}
	frontend := filepath.Join(root, executable("aria-code-bin"))
	worker := filepath.Join(root, executable("aria-code-worker"))
	helper := filepath.Join(contentsDir(root), executable("aria-native"))
	for _, path := range []string{frontend, worker, helper} {
		if err := regular(path); err != nil {
			return err
		}
	}
	temp, err := os.MkdirTemp("", "aria-frozen-smoke-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(temp)
	state := filepath.Join(temp, "state")
	if err := os.Mkdir(state, 0o700); err != nil {
		return err
	}
	config := []byte(`{"model":"gemini-3.8-flash","local_provider":"google","check_for_update_on_startup":false,"auto_save_sessions":true,"task_isolation":"off"}`)
	if err := os.WriteFile(filepath.Join(state, "config.json"), config, 0o600); err != nil {
		return err
	}
	env := verificationEnv(temp)
	run := func(binary string, args ...string) (string, error) {
		return verificationRun(ctx, temp, env, binary, args...)
	}
	version, err := run(frontend, "--version")
	if err != nil {
		return err
	}
	if !strings.HasPrefix(version, "aria-code ") || len(version) == len("aria-code ") {
		return fmt.Errorf("invalid frontend version: %q", version)
	}
	workerVersion, err := run(worker, "--aria-worker", "cli", "--version")
	if err != nil {
		return err
	}
	if workerVersion != version {
		return fmt.Errorf("frontend/worker version mismatch: %q != %q", version, workerVersion)
	}
	if runtime.GOOS != "windows" {
		aliases := filepath.Join(temp, "bin")
		if err := os.Mkdir(aliases, 0o700); err != nil {
			return err
		}
		for _, name := range []string{"aria", "aria-code"} {
			alias := filepath.Join(aliases, name)
			if err := os.Symlink(frontend, alias); err != nil {
				return err
			}
			got, err := run(alias, "--version")
			if err != nil {
				return err
			}
			if got != version {
				return fmt.Errorf("%s version mismatch", name)
			}
			help, err := run(alias, "--help")
			if err != nil {
				return err
			}
			if help == "" || strings.Contains(help, "aria-native —") {
				return fmt.Errorf("%s did not route to product help", name)
			}
		}
		alias := filepath.Join(aliases, "aria-native")
		if err := os.Symlink(helper, alias); err != nil {
			return err
		}
		helper = alias
	}
	ready, err := verificationSession(ctx, temp, env, helper)
	if err != nil {
		return err
	}
	var readyVersion string
	var robot []json.RawMessage
	_ = json.Unmarshal(ready["version"], &readyVersion)
	_ = json.Unmarshal(ready["robot"], &robot)
	if "aria-code "+readyVersion != version || len(robot) != 4 {
		return fmt.Errorf("invalid ready version or robot")
	}
	const marker = "bundled runtime file access"
	if err := os.WriteFile(filepath.Join(temp, "note.txt"), []byte(marker), 0o600); err != nil {
		return err
	}
	result, err := run(helper, "tool", "read_file", `{"path":"note.txt"}`)
	if err != nil {
		return err
	}
	var response struct {
		Result struct {
			Success bool            `json:"success"`
			Data    json.RawMessage `json:"data"`
		} `json:"result"`
	}
	if err := json.Unmarshal([]byte(result), &response); err != nil {
		return err
	}
	if !response.Result.Success || !bytes.Contains(response.Result.Data, []byte(marker)) {
		return fmt.Errorf("bundled file tool did not return the fixture contents")
	}
	_, err = fmt.Fprintf(output, "%s: Rust frontend, frozen worker, canonical robot, file tool and shutdown passed\n", version)
	return err
}
