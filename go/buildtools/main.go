// Release assembly: build and validate Rust before replacing the launcher.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"time"
)

func main() {
	if len(os.Args) > 1 && (os.Args[1] == "pack" || os.Args[1] == "unpack" || os.Args[1] == "packages") {
		if err := packagingCommand(os.Args[1], os.Args[2:], os.Stdout); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "verify" {
		if len(os.Args) != 3 {
			fmt.Fprintln(os.Stderr, "usage: aria-bundle verify ONEDIR")
			os.Exit(2)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
		defer cancel()
		if err := verifyFrontend(ctx, os.Args[2], os.Stdout); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	flags := flag.NewFlagSet("aria-bundle", flag.ContinueOnError)
	root := flags.String("root", "", "Repository root (required)")
	frontend := flags.Bool("frontend", false, "Install Rust as launcher; retain the application worker")
	if err := flags.Parse(os.Args[1:]); err != nil {
		os.Exit(2)
	}
	if *root == "" || flags.NArg() != 1 {
		fmt.Fprintln(os.Stderr, "usage: aria-bundle --root REPOSITORY [--frontend] ONEDIR")
		os.Exit(2)
	}
	if err := bundle(*root, flags.Arg(0), *frontend, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func executable(name string) string {
	if runtime.GOOS == "windows" {
		return name + ".exe"
	}
	return name
}

func regular(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("expected a regular file: %s", path)
	}
	return nil
}

func contentsDir(onedir string) string {
	internal := filepath.Join(onedir, "_internal")
	if info, err := os.Stat(internal); err == nil && info.IsDir() {
		return internal
	}
	return onedir
}

func bundle(root, onedir string, frontend bool, output io.Writer) error {
	root, err := filepath.Abs(root)
	if err != nil {
		return err
	}
	onedir, err = filepath.Abs(onedir)
	if err != nil {
		return err
	}
	if info, err := os.Stat(onedir); err != nil || !info.IsDir() {
		return fmt.Errorf("not a directory: %s", onedir)
	}
	launcher := filepath.Join(onedir, executable("aria-code-bin"))
	worker := filepath.Join(onedir, executable("aria-code-worker"))
	if frontend {
		if err := regular(launcher); err != nil {
			return fmt.Errorf("expected a fresh PyInstaller launcher: %w", err)
		}
		if _, err := os.Lstat(worker); !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("worker already exists or cannot be inspected: %s", worker)
		}
	}
	build := exec.Command("cargo", "build", "--release", "--locked", "-p", "aria-native")
	build.Dir = filepath.Join(root, "rust")
	build.Stdout, build.Stderr = output, output
	if err := build.Run(); err != nil {
		return fmt.Errorf("Rust build failed: %w", err)
	}
	targetDir := os.Getenv("CARGO_TARGET_DIR")
	if targetDir == "" {
		targetDir = "target"
	}
	if !filepath.IsAbs(targetDir) {
		targetDir = filepath.Join(root, "rust", targetDir)
	}
	built := filepath.Join(targetDir, "release", executable("aria-native"))
	return installNative(built, onedir, frontend, output)
}

// Copy to a same-filesystem temporary file, execute it there, then commit the
// names. A failed probe must leave the Python launcher and worker untouched.
func installNative(built, onedir string, frontend bool, output io.Writer) error {
	if err := regular(built); err != nil {
		return err
	}
	target := filepath.Join(contentsDir(onedir), executable("aria-native"))
	temp, err := copyTemporary(built, filepath.Dir(target))
	if err != nil {
		return err
	}
	defer os.Remove(temp)
	version, err := probe(temp, onedir)
	if err != nil {
		return err
	}
	launcher := filepath.Join(onedir, executable("aria-code-bin"))
	worker := filepath.Join(onedir, executable("aria-code-worker"))
	var prepared string
	if frontend {
		if err := regular(launcher); err != nil {
			return err
		}
		if _, err := os.Lstat(worker); !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("expected a fresh distribution; worker exists: %s", worker)
		}
		prepared, err = copyTemporary(built, onedir)
		if err != nil {
			return err
		}
		defer os.Remove(prepared)
		if _, err := probe(prepared, onedir); err != nil {
			return err
		}
	}
	// Native indexes are replaceable, but never overwrite a redirected target.
	if info, err := os.Lstat(target); err == nil && !info.Mode().IsRegular() {
		return fmt.Errorf("native index target is not a regular file: %s", target)
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.Rename(temp, target); err != nil {
		return err
	}
	if frontend {
		if err := os.Rename(launcher, worker); err != nil {
			return err
		}
		if err := os.Rename(prepared, launcher); err != nil {
			rollback := os.Rename(worker, launcher)
			return errors.Join(fmt.Errorf("install frontend: %w", err), rollback)
		}
		fmt.Fprintf(output, "native frontend installed; application worker at %s\n", worker)
	}
	fmt.Fprintf(output, "bundled %s at %s\n", bytes.TrimSpace(version), target)
	return nil
}

func copyTemporary(source, directory string) (name string, err error) {
	in, err := os.Open(source)
	if err != nil {
		return "", err
	}
	defer in.Close()
	out, err := os.CreateTemp(directory, ".aria-bundle-*"+filepath.Ext(executable("aria-native")))
	if err != nil {
		return "", err
	}
	name = out.Name()
	defer func() {
		if err != nil {
			out.Close()
			os.Remove(name)
		}
	}()
	if _, err = io.Copy(out, in); err != nil {
		return name, err
	}
	if err = out.Chmod(0o755); err != nil {
		return name, err
	}
	if err = out.Close(); err != nil {
		return name, err
	}
	return name, nil
}

func probe(binary, onedir string) ([]byte, error) {
	run := func(input []byte, args ...string) ([]byte, error) {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, binary, args...)
		cmd.Stdin = bytes.NewReader(input)
		var stderr bytes.Buffer
		cmd.Stderr = &stderr
		out, err := cmd.Output()
		if err != nil {
			return nil, fmt.Errorf("native smoke test %v failed: %w (%s)", args, err, stderr.String())
		}
		return out, nil
	}
	version, err := run(nil, "--version")
	if err != nil {
		return nil, err
	}
	input, _ := json.Marshal(map[string]any{"root": onedir, "files": []string{}, "parse": []string{}})
	out, err := run(input, "index", "imports")
	if err != nil {
		return nil, err
	}
	var answer map[string]json.RawMessage
	if err := json.Unmarshal(out, &answer); err != nil || len(answer) != 2 {
		return nil, fmt.Errorf("unexpected native index response: %s", out)
	}
	var imports map[string]json.RawMessage
	var fallback []string
	if json.Unmarshal(answer["imports"], &imports) != nil || imports == nil || len(imports) != 0 || json.Unmarshal(answer["fallback"], &fallback) != nil || fallback == nil || len(fallback) != 0 {
		return nil, fmt.Errorf("unexpected native index response: %s", out)
	}
	return version, nil
}
