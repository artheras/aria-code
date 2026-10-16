package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

var platformKeys = []string{"darwin-arm64", "darwin-x64", "linux-x64", "linux-arm64", "win32-x64"}

type archivePairs map[string]string

func (p *archivePairs) String() string { return "platform=archive" }
func (p *archivePairs) Set(value string) error {
	key, file, ok := strings.Cut(value, "=")
	known := false
	for _, platform := range platformKeys {
		if key == platform {
			known = true
		}
	}
	if !ok || !known || file == "" {
		return fmt.Errorf("expected a known platform=archive, got %q", value)
	}
	if *p == nil {
		*p = archivePairs{}
	}
	if _, exists := (*p)[key]; exists {
		return fmt.Errorf("duplicate platform %s", key)
	}
	if err := regular(file); err != nil {
		return err
	}
	(*p)[key] = file
	return nil
}

func buildPlatform(key, version, archive, output string, mcp bool) (string, error) {
	platform, arch, _ := strings.Cut(key, "-")
	suffix, name, kind := key, "aria-code-bin", "CLI"
	if mcp {
		suffix = "mcp-" + key
		name = "aria-code-mcp-bin"
		kind = "MCP server"
	}
	pkg := filepath.Join(output, "aria-code-"+suffix)
	if err := os.MkdirAll(filepath.Join(pkg, "bin"), 0o755); err != nil {
		return "", err
	}
	app, err := unpackArchive(archive, filepath.Join(pkg, "bin"), true)
	if err != nil {
		return "", err
	}
	if filepath.Base(app) != name {
		return "", fmt.Errorf("archive root %s; expected %s", filepath.Base(app), name)
	}
	exe := name
	if platform == "win32" {
		exe += ".exe"
	}
	target := filepath.Join(app, exe)
	if err := regular(target); err != nil {
		return "", err
	}
	info, err := os.Stat(target)
	if err != nil {
		return "", err
	}
	if err := os.Chmod(target, info.Mode().Perm()|0o111); err != nil {
		return "", err
	}
	meta := map[string]any{"name": "@artheras/aria-code-" + suffix, "version": version,
		"description": "aria-code " + kind + " binary for " + key, "license": "Apache-2.0",
		"os": []string{platform}, "cpu": []string{arch}, "files": []string{"bin/"}}
	data, err := json.MarshalIndent(meta, "", "  ")
	if err != nil {
		return "", err
	}
	if err := os.WriteFile(filepath.Join(pkg, "package.json"), append(data, '\n'), 0o644); err != nil {
		return "", err
	}
	return pkg, nil
}

func buildPlatforms(version string, cli, mcp archivePairs, output string, out io.Writer) error {
	if version == "" || len(cli) == 0 || len(cli) != len(mcp) {
		return fmt.Errorf("version and matching nonempty CLI/MCP platform sets are required")
	}
	for key := range cli {
		if _, ok := mcp[key]; !ok {
			return fmt.Errorf("CLI and MCP platform sets must match")
		}
	}
	if err := os.MkdirAll(output, 0o755); err != nil {
		return err
	}
	staging, err := os.MkdirTemp(output, ".aria-packages-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(staging)
	var prepared []string
	for _, key := range sortedKeys(cli) {
		for _, server := range []bool{false, true} {
			archive := cli[key]
			if server {
				archive = mcp[key]
			}
			pkg, err := buildPlatform(key, version, archive, staging, server)
			if err != nil {
				return err
			}
			prepared = append(prepared, pkg)
		}
	}
	// Every input validates before any published package directory is replaced.
	// Individual directory promotions are atomic; this is not a set transaction.
	for _, pkg := range prepared {
		if err := promoteDirectory(pkg, filepath.Join(output, filepath.Base(pkg))); err != nil {
			return err
		}
	}
	_, err = fmt.Fprintf(out, "Built %d platform package(s) in %s\n", len(prepared), output)
	return err
}

func packagingCommand(command string, args []string, output io.Writer) error {
	flags := flag.NewFlagSet(command, flag.ContinueOnError)
	switch command {
	case "pack":
		useZip := flags.Bool("zip", runtime.GOOS == "windows", "Create ZIP (default on Windows)")
		if err := flags.Parse(args); err != nil {
			return err
		}
		if flags.NArg() != 2 {
			return fmt.Errorf("usage: aria-bundle pack [--zip] SOURCE OUT")
		}
		result, err := packDirectory(flags.Arg(0), flags.Arg(1), *useZip)
		if err != nil {
			return err
		}
		_, err = fmt.Fprintln(output, result)
		return err
	case "unpack":
		dereference := flags.Bool("dereference", false, "Replace internal links with copies")
		if err := flags.Parse(args); err != nil {
			return err
		}
		if flags.NArg() != 2 {
			return fmt.Errorf("usage: aria-bundle unpack [--dereference] ARCHIVE DEST")
		}
		result, err := unpackArchive(flags.Arg(0), flags.Arg(1), *dereference)
		if err != nil {
			return err
		}
		_, err = fmt.Fprintln(output, result)
		return err
	case "packages":
		version := flags.String("version", "", "Release version")
		dest := flags.String("out", "npm/platforms", "Output directory")
		var cli, mcp archivePairs
		flags.Var(&cli, "binary", "platform=CLI archive (repeatable)")
		flags.Var(&mcp, "mcp", "platform=MCP archive (repeatable)")
		if err := flags.Parse(args); err != nil {
			return err
		}
		if flags.NArg() != 0 {
			return fmt.Errorf("unexpected arguments: %v", flags.Args())
		}
		return buildPlatforms(*version, cli, mcp, *dest, output)
	}
	return fmt.Errorf("unknown packaging command %s", command)
}
