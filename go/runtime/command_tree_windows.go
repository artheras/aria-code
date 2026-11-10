package main

import (
	"context"
	"os"
	"os/exec"
	"strconv"
	"syscall"
	"time"
)

func configureCommandTree(cmd *exec.Cmd) {
	cmd.Cancel = func() error {
		stopCommandTree(cmd)
		if cmd.Process == nil {
			return os.ErrProcessDone
		}
		return cmd.Process.Kill()
	}
}
func stopCommandTree(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	if cmd.ProcessState != nil && cmd.ProcessState.Exited() {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_ = exec.CommandContext(ctx, "taskkill.exe", "/PID", strconv.Itoa(cmd.Process.Pid), "/T", "/F").Run()
}

// shellCommand runs command through cmd.exe with the command line passed
// as written: with /s, cmd.exe drops the outer quotes and keeps the rest.
// Go's own argument escaping would turn inner quotes into \", which
// cmd.exe does not understand, so a quoted program path would break.
func shellCommand(ctx context.Context, command string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, "cmd.exe")
	cmd.SysProcAttr = &syscall.SysProcAttr{CmdLine: `cmd.exe /d /s /c "` + command + `"`}
	return cmd
}
