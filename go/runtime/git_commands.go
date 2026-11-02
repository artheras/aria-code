package main

// Git, review and check commands ported from the Python worker
// (engineering_cmds.py, git_cmds.py, review_service.py): /git, /commit, /pr,
// /gh, /review, /test, /verify, and the /fix, /bug, /trace and /quality
// turns. Anything that changes the repository or reaches the network runs as
// a user command, under the same approval as run_command.

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"google.golang.org/genai"
)

// Git subcommands that only read, run without a prompt. Options that write
// files or run programs are refused even for these.
var readOnlyGit = map[string]bool{
	"status": true, "log": true, "diff": true, "show": true, "blame": true, "shortlog": true,
	"describe": true, "ls-files": true, "grep": true, "reflog": true, "rev-parse": true,
}

func unsafeGitArg(arg string) bool {
	for _, prefix := range []string{"--output", "--ext-diff", "--textconv", "--exec", "--open-files-in-pager", "-O"} {
		if strings.HasPrefix(arg, prefix) {
			return true
		}
	}
	return false
}

// argQuote quotes one argument for the shell userCommand runs: sh, or
// cmd.exe on Windows.
func argQuote(arg string) string {
	if runtime.GOOS != "windows" {
		return shellQuote(arg)
	}
	// = , ; also split arguments for batch files (a .cmd wrapper as
	// ARIA_BROWSER), so they are quoted too.
	if arg != "" && !strings.ContainsAny(arg, " \t\"&|<>^%()=,;") {
		return arg
	}
	return `"` + strings.ReplaceAll(arg, `"`, `\"`) + `"`
}

func shellJoin(args []string) string {
	quoted := make([]string, len(args))
	for i, arg := range args {
		quoted[i] = argQuote(arg)
	}
	return strings.Join(quoted, " ")
}

func (s *server) requireGit(ctx context.Context) error {
	if _, err := exec.LookPath("git"); err != nil {
		return errors.New("git is not installed")
	}
	if _, err := git(ctx, s.scope.workspace, nil, 10*time.Second, "rev-parse", "--is-inside-work-tree"); err != nil {
		return errors.New("not a git repository")
	}
	return nil
}

func (s *server) gitCommand(ctx context.Context, turn string, args []string) (string, error) {
	if len(args) == 0 {
		args = []string{"status", "--short", "--branch"}
	}
	if err := s.requireGit(ctx); err != nil {
		return "", err
	}
	readOnly := readOnlyGit[args[0]] || (args[0] == "branch" && len(args) == 1) ||
		(args[0] == "remote" && len(args) == 2 && args[1] == "-v") || (args[0] == "stash" && len(args) == 2 && args[1] == "list")
	if readOnly {
		for _, arg := range args[1:] {
			if unsafeGitArg(arg) {
				return "", fmt.Errorf("git option %s is not allowed here", arg)
			}
		}
		out, err := git(ctx, s.scope.workspace, nil, 30*time.Second, append([]string{"--no-pager"}, args...)...)
		if err != nil {
			return "", err
		}
		if out == "" {
			out = "(no output)"
		}
		return capText(out, 60000), nil
	}
	return s.userCommand(ctx, turn, "git "+shellJoin(args))
}

func (s *server) stagedDiff(ctx context.Context) (string, error) {
	diff, err := git(ctx, s.scope.workspace, nil, 30*time.Second, "diff", "HEAD", "--no-ext-diff")
	if err != nil {
		diff, err = git(ctx, s.scope.workspace, nil, 30*time.Second, "diff", "--no-ext-diff")
	}
	if err != nil {
		return "", err
	}
	untracked, _ := git(ctx, s.scope.workspace, nil, 10*time.Second, "ls-files", "--others", "--exclude-standard")
	if untracked != "" {
		diff += "\n\nNew files:\n" + untracked
	}
	return strings.TrimSpace(diff), nil
}

// commitMessage asks the model for a message from the diff alone.
func (s *server) commitMessage(ctx context.Context, diff string) (string, error) {
	if runes := []rune(diff); len(runes) > 40000 {
		diff = string(runes[:40000]) + "\n… [diff truncated]"
	}
	prompt := "Write a git commit message for this change: a summary line under 72 characters in the imperative mood, then a blank line and a short body saying what changed and why. Answer with the message only, no code fences.\n\n" + diff
	reply, err := s.provider.Generate(ctx, []*genai.Content{genai.NewContentFromText(prompt, genai.RoleUser)}, "You write concise, accurate git commit messages.", nil, func(string) {})
	if err != nil {
		return "", err
	}
	message := strings.TrimSpace(strings.Trim(strings.TrimSpace(contentText(reply)), "`"))
	if message == "" {
		return "", errors.New("the model returned no commit message; use /commit <message>")
	}
	return message, nil
}

func (s *server) commitCommand(ctx context.Context, turn, message string) (string, error) {
	if err := s.requireGit(ctx); err != nil {
		return "", err
	}
	diff, err := s.stagedDiff(ctx)
	if err != nil {
		return "", err
	}
	if diff == "" {
		return "Nothing to commit.", nil
	}
	if strings.TrimSpace(message) == "" {
		if message, err = s.commitMessage(ctx, diff); err != nil {
			return "", err
		}
	}
	if out, err := s.userCommand(ctx, turn, "git add -A"); err != nil || strings.HasPrefix(out, "Command was not executed") {
		return out, err
	}
	// A file carries a multi-line message through any shell, cmd.exe included.
	file := filepath.Join(s.opts.root, "COMMIT_MSG")
	if err := atomicWrite(file, []byte(message+"\n")); err != nil {
		return "", err
	}
	defer os.Remove(file)
	return s.userCommand(ctx, turn, "git commit -F "+argQuote(file))
}

func (s *server) ghCommand(ctx context.Context, turn string, args []string) (string, error) {
	if _, err := exec.LookPath("gh"); err != nil {
		return "", errors.New("the GitHub CLI (gh) is not installed: https://cli.github.com")
	}
	if len(args) == 0 {
		return "", errors.New("usage: /gh <arguments>")
	}
	return s.userCommand(ctx, turn, "gh "+shellJoin(args))
}

func (s *server) prCommand(ctx context.Context, turn string, args []string) (string, error) {
	sub := "status"
	if len(args) > 0 {
		sub = args[0]
	}
	switch sub {
	case "status", "view", "checks", "diff":
		return s.ghCommand(ctx, turn, append([]string{"pr", sub}, args[min(1, len(args)):]...))
	case "create":
		title := strings.Join(args[1:], " ")
		if title == "" {
			return s.ghCommand(ctx, turn, []string{"pr", "create", "--fill"})
		}
		return s.ghCommand(ctx, turn, []string{"pr", "create", "--fill", "--title", title})
	}
	return "", errors.New("usage: /pr [status | view | checks | diff | create [title]]")
}

// reviewCommandText reviews the working tree, the staged change or the
// branch against a base, with the review gate's isolated reviewer.
func (s *server) reviewChange(ctx context.Context, args []string) (string, error) {
	if err := s.requireGit(ctx); err != nil {
		return "", err
	}
	var diff, what string
	var err error
	switch {
	case len(args) == 0:
		diff, err = s.stagedDiff(ctx)
		what = "the uncommitted change"
	case args[0] == "--staged" && len(args) == 1:
		diff, err = git(ctx, s.scope.workspace, nil, 30*time.Second, "diff", "--cached", "--no-ext-diff")
		what = "the staged change"
	case args[0] == "--base" && len(args) == 2 && !strings.HasPrefix(args[1], "-"):
		diff, err = git(ctx, s.scope.workspace, nil, 30*time.Second, "diff", "--no-ext-diff", args[1]+"...HEAD")
		what = "this branch against " + args[1]
	default:
		return "", errors.New("usage: /review [--staged | --base BRANCH]")
	}
	if err != nil {
		return "", err
	}
	if strings.TrimSpace(diff) == "" {
		return "Nothing to review.", nil
	}
	goal := "Review " + what + " for correctness and safety."
	if plan, _ := s.snap.Metadata["plan"].(string); plan != "" {
		goal += "\n\nThe change was meant to carry out this plan:\n" + capText(plan, 4000)
	}
	prompt := buildReviewPrompt(goal, diff, nil, "", 60000)
	reply, err := s.provider.Generate(ctx, []*genai.Content{genai.NewContentFromText(prompt, genai.RoleUser)}, reviewerSystem, nil, func(string) {})
	if err != nil {
		return "", err
	}
	report := parseReview(contentText(reply), 1)
	return strings.Join(report.lines(30), "\n"), nil
}

// checkCommands are the project's declared acceptance commands, or the ones
// inferred from what changed.
func (s *server) checkCommands(paths []string) ([]string, string) {
	if declared := declaredAcceptanceCommands(s.cfg, s.scope.workspace); len(declared) > 0 {
		return declared, "configured acceptance commands"
	}
	plan := planVerification(s.scope.workspace, paths)
	return plan.Commands, plan.Reason
}

func (s *server) testCommand(ctx context.Context, turn string, paths []string) (string, error) {
	commands, reason := s.checkCommands(paths)
	if len(commands) == 0 {
		return "No checks found (" + reason + "). Set acceptance_commands with /config or in ARIA.md.", nil
	}
	var b strings.Builder
	fmt.Fprintf(&b, "Checks (%s)\n", reason)
	for _, command := range commands {
		out, err := s.userCommand(ctx, turn, command)
		if strings.HasPrefix(out, "Command was not executed") {
			fmt.Fprintf(&b, "\n  – %s: %s", command, out)
			return b.String(), nil
		}
		if err != nil {
			fmt.Fprintf(&b, "\n  ✗ %s\n\n%s", command, capText(out, 20000))
			return b.String(), fmt.Errorf("check failed: %s", command)
		}
		fmt.Fprintf(&b, "\n  ✓ %s", command)
	}
	return b.String(), nil
}

func (s *server) verifyCommand(ctx context.Context, turn string) (string, error) {
	checks, err := s.testCommand(ctx, turn, nil)
	if err != nil {
		return checks, err
	}
	review, err := s.reviewChange(ctx, nil)
	if err != nil {
		return checks + "\n\nReview: " + err.Error(), nil
	}
	return checks + "\n\n" + review, nil
}

// engineeringPrompt turns /fix, /bug, /trace and /quality into the model
// turn they stand for. /trace and /quality only read.
func engineeringPrompt(name, extra string) (prompt string, readOnly bool, err error) {
	switch name {
	case "/fix":
		prompt = "Fix the failing checks in this project. Run the relevant tests or build first to see the actual failure, find the root cause, make the smallest correct change, and run the checks again to confirm."
		if extra != "" {
			prompt = "Fix this: " + extra + "\n\nReproduce it first (run the failing command or test), find the root cause, make the smallest correct change, and verify it with the same command."
		}
		return prompt, false, nil
	case "/bug":
		if extra == "" {
			return "", false, errors.New("usage: /bug <what goes wrong>")
		}
		return "Investigate this bug: " + extra + "\n\nFind where it comes from and reproduce it, ideally with a failing test. Then fix it and show the test passing. If you cannot reproduce it, say what you tried.", false, nil
	case "/trace":
		if extra == "" {
			return "", false, errors.New("usage: /trace <error message or stack trace>")
		}
		return "Trace this error to its root cause. This turn is read-only: read and search the code, do not change files or run commands. Explain the chain from the code to the error with file and line references, and propose the fix.\n\n" + extra, true, nil
	case "/quality":
		prompt = "Assess the quality of the current change (git diff) in this project. This turn is read-only. Point out bugs, missing tests, error handling, duplication and unclear code, each with file and line references, most important first."
		if extra != "" {
			prompt += "\n\nFocus on: " + extra
		}
		return prompt, true, nil
	}
	return "", false, nil
}
