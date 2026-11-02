package main

// /browser and /screenshot, the native form of the Python worker's
// computer-use commands (ui_cmds.py). Both save a PNG under
// <state>/screenshots and leave the asking to /vision, so an image reaches
// the model only through the existing document_vision switch. Both run as
// user commands, under run_command's approval: a page load reaches the
// network and a screen capture sees everything on the screen.

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// browserBinary is ARIA_BROWSER, or the first Chrome-family browser found.
func browserBinary() string {
	if path := strings.TrimSpace(os.Getenv("ARIA_BROWSER")); path != "" {
		return path
	}
	for _, name := range []string{"chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome", "msedge"} {
		if path, err := exec.LookPath(name); err == nil {
			return path
		}
	}
	for _, path := range []string{
		"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
		"/Applications/Chromium.app/Contents/MacOS/Chromium",
		`C:\Program Files\Google\Chrome\Application\chrome.exe`,
		`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`,
	} {
		if _, err := os.Stat(path); err == nil {
			return path
		}
	}
	return ""
}

func (s *server) screenshotPath(prefix string) (string, error) {
	dir := filepath.Join(s.opts.root, "screenshots")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	return filepath.Join(dir, prefix+"-"+time.Now().Format("20060102-150405")+".png"), nil
}

func (s *server) browserCommand(ctx context.Context, turn string, args []string) (string, error) {
	if len(args) != 1 {
		return "", errors.New("usage: /browser <http(s) URL>")
	}
	u, err := url.Parse(args[0])
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return "", errors.New("only http and https URLs can be opened")
	}
	if !s.cfg.boolean("network_enabled", true) {
		return "", errors.New("network access is off (/config network_enabled)")
	}
	browser := browserBinary()
	if browser == "" {
		return "", errors.New("no Chrome, Chromium or Edge found; install one or set ARIA_BROWSER")
	}
	shot, err := s.screenshotPath("page")
	if err != nil {
		return "", err
	}
	command := strings.Join([]string{argQuote(browser), "--headless=new", "--disable-gpu", "--hide-scrollbars",
		"--window-size=1280,900", argQuote("--screenshot=" + shot), argQuote(u.String())}, " ")
	out, err := s.userCommandTimeout(ctx, turn, command, 60)
	if strings.HasPrefix(out, "Command was not executed") {
		return out, nil
	}
	if _, statErr := os.Stat(shot); statErr != nil {
		if err == nil {
			err = errors.New("the browser wrote no screenshot")
		}
		if strings.Contains(strings.ToLower(out), "sandbox") {
			err = fmt.Errorf("%w; the browser's sandbox could not start here (common in containers and on some Linux setups): set ARIA_BROWSER to a browser that runs headless on this machine", err)
		}
		return capText(out, 4000), err
	}
	return fmt.Sprintf("Saved %s\n/vision %s <question> asks about it.", shot, argQuote(shot)), nil
}

// screenCaptureCommand is the operating system's own capture tool.
func screenCaptureCommand(path string) (string, error) {
	quoted := argQuote(path)
	switch runtime.GOOS {
	case "darwin":
		return "screencapture -x " + quoted, nil
	case "windows":
		return `powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; $b=[System.Windows.Forms.SystemInformation]::VirtualScreen; $i=New-Object System.Drawing.Bitmap $b.Width,$b.Height; $g=[System.Drawing.Graphics]::FromImage($i); $g.CopyFromScreen($b.Left,$b.Top,0,0,$i.Size); $i.Save('` + strings.ReplaceAll(path, "'", "''") + `')"`, nil
	}
	for _, tool := range []struct{ binary, args string }{
		{"grim", ""}, {"gnome-screenshot", "-f"}, {"spectacle", "-b -n -o"}, {"scrot", ""}, {"import", "-window root"},
	} {
		if _, err := exec.LookPath(tool.binary); err == nil {
			return strings.TrimSpace(tool.binary+" "+tool.args) + " " + quoted, nil
		}
	}
	return "", errors.New("no screen capture tool found (grim, gnome-screenshot, spectacle, scrot or ImageMagick import)")
}

func (s *server) screenshotCommand(ctx context.Context, turn string) (string, error) {
	if remoteScope() {
		return "", errors.New("a remote session has no screen to capture")
	}
	shot, err := s.screenshotPath("screen")
	if err != nil {
		return "", err
	}
	command, err := screenCaptureCommand(shot)
	if err != nil {
		return "", err
	}
	out, err := s.userCommandTimeout(ctx, turn, command, 30)
	if strings.HasPrefix(out, "Command was not executed") {
		return out, nil
	}
	if _, statErr := os.Stat(shot); statErr != nil {
		if err == nil {
			err = errors.New("the capture tool wrote no image")
		}
		return capText(out, 4000), err
	}
	return fmt.Sprintf("Saved %s\n/vision %s <question> asks about it.", shot, argQuote(shot)), nil
}
