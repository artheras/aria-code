package main

import (
	"bytes"
	"context"
	"errors"
	"google.golang.org/genai"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

type documentOptions struct {
	Source     string
	Images     *[]*genai.Part
	OCR        bool
	Language   string
	Restricted bool
}

func documentSandboxAvailable() bool {
	if runtime.GOOS == "darwin" {
		return true
	}
	if runtime.GOOS == "linux" {
		_, err := exec.LookPath("bwrap")
		return err == nil
	}
	return false
}

func linuxBwrapArgs(dir, executable, data string, args []string) ([]string, error) {
	bwrapPath, err := exec.LookPath("bwrap")
	if err != nil {
		return nil, err
	}
	cmdArgs := []string{
		bwrapPath,
		"--unshare-net",
		"--new-session",
		"--die-with-parent",
		"--proc", "/proc",
		"--dev", "/dev",
		"--bind", dir, dir,
		"--chdir", dir,
	}
	for _, ro := range []string{"/usr", "/lib", "/lib64", "/bin", "/etc/ld.so.cache", "/etc/fonts", "/etc/poppler"} {
		if _, statErr := os.Stat(ro); statErr == nil {
			cmdArgs = append(cmdArgs, "--ro-bind", ro, ro)
		}
	}
	if data != "" {
		cmdArgs = append(cmdArgs, "--ro-bind", data, data)
	}
	cmdArgs = append(cmdArgs, "--ro-bind", executable, executable, executable)
	cmdArgs = append(cmdArgs, args...)
	return cmdArgs, nil
}

// Seatbelt denies network, project/home reads and all writes outside scratch.
// Dependency roots contain installed executables/libraries, never the workspace.
func documentProfile(dir, executable, data string) string {
	var b strings.Builder
	b.WriteString("(version 1)(deny default)(allow process-fork)(allow sysctl-read)(allow mach-lookup)(allow file-read-metadata)\n")
	b.WriteString("(allow process-exec (literal " + strconv.Quote(executable) + "))\n")
	for _, p := range []string{"/System", "/usr/lib", "/usr/share", "/opt/homebrew/Cellar", "/opt/homebrew/share", "/private/var/db/dyld"} {
		b.WriteString("(allow file-read* (subpath " + strconv.Quote(p) + "))\n")
	}
	for _, p := range []string{executable, "/", "/dev/null", "/dev/urandom", "/dev/random"} {
		b.WriteString("(allow file-read* (literal " + strconv.Quote(p) + "))\n")
	}
	b.WriteString("(allow file-read* file-write* (subpath " + strconv.Quote(dir) + "))\n")
	if data != "" {
		b.WriteString("(allow file-read* (subpath " + strconv.Quote(data) + "))\n")
	}
	return b.String()
}

func runDocumentTool(ctx context.Context, name string, args []string, input []byte, limit int, restricted bool) ([]byte, error) {
	executable, err := exec.LookPath(name)
	if err != nil || !filepath.IsAbs(executable) {
		return nil, errors.New("document conversion requires installed " + name)
	}
	executable, err = filepath.EvalSymlinks(executable)
	if err != nil {
		return nil, err
	}
	if restricted && !documentSandboxAvailable() {
		return nil, errors.New("document sandbox unavailable on this platform; refusing restricted conversion")
	}
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	dir, err := os.MkdirTemp("", "aria-document-")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(dir)
	dir, err = filepath.EvalSymlinks(dir)
	if err != nil {
		return nil, err
	}
	data := os.Getenv("ARIA_OCR_DATA")
	if data != "" {
		data, err = filepath.EvalSymlinks(data)
		if err != nil || !filepath.IsAbs(data) {
			return nil, errors.New("invalid ARIA_OCR_DATA directory")
		}
	}
	if name == "tesseract" && data != "" {
		args = append(args, "--tessdata-dir", data)
	}
	cmd := exec.CommandContext(ctx, executable, args...)
	if runtime.GOOS == "darwin" {
		cmd = exec.CommandContext(ctx, "/usr/bin/sandbox-exec", append([]string{"-p", documentProfile(dir, executable, data), executable}, args...)...)
	} else if runtime.GOOS == "linux" {
		if bwrapCmd, bErr := linuxBwrapArgs(dir, executable, data, args); bErr == nil {
			cmd = exec.CommandContext(ctx, bwrapCmd[0], bwrapCmd[1:]...)
		}
	}
	cmd.Dir = dir
	cmd.Env = []string{"HOME=" + dir, "TMPDIR=" + dir, "LANG=C", "LC_ALL=C", "OMP_THREAD_LIMIT=2"}
	if runtime.GOOS == "windows" {
		// A Windows process started with no SystemRoot cannot load system DLLs,
		// and Poppler resolves its own DLLs from its directory. Give it exactly
		// those two locations and nothing else from the caller's environment.
		root := os.Getenv("SystemRoot")
		cmd.Env = append(cmd.Env, "SystemRoot="+root, "PATH="+filepath.Dir(executable)+";"+filepath.Join(root, "System32"))
	}
	cmd.WaitDelay = time.Second
	configureCommandTree(cmd)
	cmd.Stdin = bytes.NewReader(input)
	out, stderr := &documentCapture{limit: limit}, &documentCapture{limit: 8192}
	cmd.Stdout, cmd.Stderr = out, stderr
	err = cmd.Run()
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if out.exceeded || stderr.exceeded {
		return nil, errors.New("document conversion exceeds output limit")
	}
	if err != nil {
		detail := strings.TrimSpace(string(stderr.Bytes()))
		if len(detail) > 300 {
			detail = detail[:300]
		}
		return nil, errors.New(name + " failed (" + err.Error() + "; " + detail + "); check document, language data and sandbox availability")
	}
	return out.Bytes(), nil
}
