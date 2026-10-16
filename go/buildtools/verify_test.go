package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Real subprocesses exercise the verifier's pipes, timeout and exit contracts.
func verificationFixture(mode string) int {
	args := strings.Join(os.Args[1:], " ")
	if os.Getenv("ARIA_RUNTIME") != "" || os.Getenv("ARIA_FRONTEND") != "" || os.Getenv("PYTHONPATH") != "" || os.Getenv("ARIA_OFFLINE") != "1" {
		return 8
	}
	switch {
	case strings.Contains(args, "--version"):
		if mode == "version-flood" {
			fmt.Print(strings.Repeat("x", 2<<20))
			return 0
		}
		if mode == "mismatch" && strings.Contains(args, "--aria-worker") {
			fmt.Println("aria-code wrong")
		} else {
			fmt.Println("aria-code 0.152.0")
		}
	case args == "--help":
		fmt.Println("Aria Code product help")
	case args == "chat --jsonl":
		if mode == "malformed" {
			fmt.Println("not json")
			return 0
		}
		if mode == "oversized" {
			fmt.Println(strings.Repeat("x", 2<<20))
			return 0
		}
		if mode == "exit" {
			return 9
		}
		if mode == "timeout" {
			time.Sleep(10 * time.Second)
			return 0
		}
		fmt.Println(`{"protocol":1,"type":"session.ready","version":"0.152.0","robot":[[],[],[],[]]}`)
		line, _ := bufio.NewReader(os.Stdin).ReadString('\n')
		if !strings.Contains(line, `"shutdown"`) {
			return 10
		}
		if mode != "no-close" {
			fmt.Println(`{"protocol":1,"type":"session.closed"}`)
		}
	case strings.HasPrefix(args, "tool read_file "):
		data, err := os.ReadFile("note.txt")
		if err != nil {
			return 11
		}
		if mode == "false-read" {
			data = []byte("wrong file")
		}
		_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"result": map[string]any{"success": true, "data": map[string]any{"content": string(data)}}})
	default:
		return 12
	}
	return 0
}

func verifierDistribution(t *testing.T, internal bool) string {
	t.Helper()
	root := filepath.Join(t.TempDir(), "distribution with spaces")
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	if internal {
		if err := os.Mkdir(filepath.Join(root, "_internal"), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	binary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, target := range []string{filepath.Join(root, executable("aria-code-bin")), filepath.Join(root, executable("aria-code-worker")), filepath.Join(contentsDir(root), executable("aria-native"))} {
		temp, err := copyTemporary(binary, filepath.Dir(target))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.Rename(temp, target); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func TestVerifyDistribution(t *testing.T) {
	for _, internal := range []bool{true, false} {
		t.Run(fmt.Sprint(internal), func(t *testing.T) {
			t.Setenv("BUILDTOOLS_VERIFY_FIXTURE", "valid")
			t.Setenv("ARIA_RUNTIME", "must-not-run")
			t.Setenv("ARIA_FRONTEND", "python")
			t.Setenv("PYTHONPATH", "must-not-import")
			var output bytes.Buffer
			if err := verifyFrontend(context.Background(), verifierDistribution(t, internal), &output); err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(output.String(), "shutdown passed") {
				t.Fatal(output.String())
			}
		})
	}
}

func TestVerifyRejectsBrokenDistribution(t *testing.T) {
	root := verifierDistribution(t, true)
	for _, mode := range []string{"mismatch", "malformed", "oversized", "exit", "no-close", "false-read", "version-flood"} {
		t.Run(mode, func(t *testing.T) {
			t.Setenv("BUILDTOOLS_VERIFY_FIXTURE", mode)
			if err := verifyFrontend(context.Background(), root, io.Discard); err == nil {
				t.Fatal("broken distribution passed")
			}
		})
	}
}

func TestVerificationSessionTimeout(t *testing.T) {
	t.Setenv("BUILDTOOLS_VERIFY_FIXTURE", "timeout")
	root := verifierDistribution(t, true)
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, err := verificationSession(ctx, t.TempDir(), verificationEnv(t.TempDir()), filepath.Join(contentsDir(root), executable("aria-native")))
	if err == nil || time.Since(start) > 4*time.Second {
		t.Fatalf("timeout did not stop child: %v", err)
	}
}

func TestVerifyActualRelease(t *testing.T) {
	root := os.Getenv("ARIA_RELEASE_DIR")
	if root == "" {
		t.Skip("set ARIA_RELEASE_DIR to exercise a frozen distribution")
	}
	if err := verifyFrontend(context.Background(), root, io.Discard); err != nil {
		t.Fatal(err)
	}
}
