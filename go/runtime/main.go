package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"time"
)

// Release packaging supplies -X main.version=<product version>.
var version = "development"

type stringsFlag []string

func (v *stringsFlag) String() string         { return strings.Join(*v, ",") }
func (v *stringsFlag) Set(value string) error { *v = append(*v, value); return nil }
func main()                                   { os.Exit(mainCode()) }
func mainCode() int {
	if len(os.Args) > 1 && os.Args[1] == "training" {
		return trainingCommand(os.Args[2:])
	}
	if len(os.Args) > 1 && os.Args[1] == "backtest" {
		return backtestCommand(os.Args[2:])
	}
	if len(os.Args) == 2 && os.Args[1] == "risk" {
		return riskCommand(os.Stdin, os.Stdout, os.Stderr)
	}
	if len(os.Args) == 2 && os.Args[1] == "providers" {
		return providersCommand(os.Stdout)
	}
	if len(os.Args) == 2 && os.Args[1] == "acceptance" {
		return acceptanceCommand(os.Stdin, os.Stdout, os.Stderr)
	}
	if len(os.Args) > 1 && os.Args[1] == "mcp" {
		return mcpServerCommand(os.Args[2:], os.Stdin, os.Stdout, os.Stderr)
	}
	if len(os.Args) > 1 && os.Args[1] == "daemon" {
		return daemonCommand(os.Args[2:], os.Stdout, os.Stderr)
	}
	if len(os.Args) > 1 && os.Args[1] == "canva" {
		return canvaCommand(os.Args[2:], os.Stdout, os.Stderr)
	}
	if len(os.Args) > 1 && os.Args[1] == "code-review" {
		return codeReviewCommand(context.Background(), os.Args[2:], os.Stdout, os.Stderr)
	}
	if len(os.Args) == 2 && os.Args[1] == "review" {
		return reviewCommand(os.Stdin, os.Stdout, os.Stderr)
	}
	if len(os.Args) == 2 && os.Args[1] == "delivery" {
		return deliveryCommand(os.Stdin, os.Stdout, os.Stderr)
	}
	if len(os.Args) == 2 && os.Args[1] == "contract" {
		return contractCommand(os.Stdin, os.Stdout, os.Stderr)
	}
	if len(os.Args) == 2 && os.Args[1] == "auth-probe" {
		ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
		defer cancel()
		started := time.Now()
		identity, err := readCloudIdentity(cloudFolder())
		ok := false
		if err == nil {
			if creds, e := nativeCloudCredentials(ctx, identity); e == nil {
				if token, e := creds.Token(ctx); e == nil {
					ok = token.IsValid()
				} else {
					err = e
				}
			} else {
				err = e
			}
		}
		result := map[string]any{"success": ok, "elapsed_seconds": time.Since(started).Seconds()}
		if err != nil {
			result["error"] = "Google credentials could not provide a valid token"
		}
		json.NewEncoder(os.Stdout).Encode(result)
		if !ok {
			return 1
		}
		return 0
	}
	if len(os.Args) == 2 && (os.Args[1] == "--version" || os.Args[1] == "version") {
		fmt.Println("aria-runtime", version)
		return 0
	}
	if len(os.Args) > 1 && os.Args[1] == "bridge" {
		ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
		defer cancel()
		return bridgeCommand(ctx, os.Args[2:], os.Stdin, os.Stdout, os.Stderr)
	}
	if len(os.Args) < 2 || (os.Args[1] != "app" && os.Args[1] != "stream") {
		fmt.Fprintln(os.Stderr, "Usage: aria-runtime app|stream|bridge|backtest|training|risk|contract|delivery|review|acceptance|providers|code-review|mcp [options]")
		return 2
	}
	mode := os.Args[1]
	options := appOptions{}
	var reads, writes stringsFlag
	var allowed string
	var prompt, format string
	var quiet bool
	flags := flag.NewFlagSet(mode, flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	flags.StringVar(&options.model, "model", "", "model name")
	flags.StringVar(&options.provider, "provider", "", "model provider")
	flags.StringVar(&options.resume, "resume", "", "saved session ID")
	flags.BoolVar(&options.resumeLast, "resume-last", false, "resume most recent session")
	flags.StringVar(&options.banner, "banner", "", "banner preference")
	flags.BoolVar(&options.noBanner, "no-banner", false, "hide banner")
	flags.BoolVar(&options.skipPermissions, "dangerously-skip-permissions", false, "explicit host authorization to skip default confirmation prompts")
	flags.StringVar(&allowed, "allow-tools", "", "explicit host session tool grants")
	flags.Var(&reads, "read-dir", "additional read-only root")
	flags.Var(&writes, "add-dir", "additional writable root")
	flags.BoolVar(&options.local, "local", false, "use configured local provider")
	flags.BoolVar(&options.thinking, "thinking", false, "thinking mode")
	flags.StringVar(&options.url, "url", "", "provider endpoint override")
	if mode == "stream" {
		flags.StringVar(&prompt, "prompt", "", "one task to execute")
		flags.StringVar(&format, "format", "jsonl", "output protocol")
		flags.BoolVar(&quiet, "quiet", false, "suppress non-protocol messages")
	}
	if err := flags.Parse(os.Args[2:]); err != nil {
		return 2
	}
	if len(flags.Args()) > 0 {
		fmt.Fprintln(os.Stderr, "unexpected application argument")
		return 2
	}
	if mode == "stream" && (format != "jsonl" || strings.TrimSpace(prompt) == "" || len(prompt) > 65536) {
		fmt.Fprintln(os.Stderr, "stream requires --format=jsonl and a nonempty prompt up to 65536 bytes")
		return 2
	}
	options.readRoots, options.writeRoots = reads, writes
	for _, value := range strings.Split(allowed, ",") {
		if value = strings.TrimSpace(value); value != "" {
			options.allowedTools = append(options.allowedTools, value)
		}
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	// No user-state read/write or model setup precedes the host's process/job
	// containment acknowledgement. Read exactly one byte without buffering.
	if err := hostBarrier(ctx, os.Stdin); err != nil {
		return 2
	}
	options.hostContained = true
	options.noninteractive = mode == "stream"
	s, err := newServer(options, os.Stdout)
	if err != nil {
		if mode == "stream" {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		json.NewEncoder(os.Stdout).Encode(map[string]any{"protocol": 1, "type": "session.failed", "error": err.Error()})
		json.NewEncoder(os.Stdout).Encode(map[string]any{"protocol": 1, "type": "session.closed"})
		return 1
	}
	if mode == "stream" {
		if err := s.runStream(ctx, prompt); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		return 0
	}
	if err := s.run(ctx, os.Stdin); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}
