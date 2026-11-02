package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// fakeBrowser writes a stand-in for Chrome's --screenshot=PATH, so the
// command line, quoting and file check run the same on every OS.
func fakeBrowser(t *testing.T) string {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "fake browser")
	os.MkdirAll(dir, 0o755)
	if runtime.GOOS == "windows" {
		path := filepath.Join(dir, "browser.cmd")
		script := "@echo off\r\n:loop\r\nif \"%~1\"==\"\" goto :eof\r\nset \"a=%~1\"\r\nif \"%a:~0,13%\"==\"--screenshot=\" echo png> \"%a:~13%\"\r\nshift\r\ngoto loop\r\n"
		os.WriteFile(path, []byte(script), 0o755)
		return path
	}
	path := filepath.Join(dir, "browser")
	os.WriteFile(path, []byte("#!/bin/sh\nfor a; do case \"$a\" in --screenshot=*) printf png > \"${a#--screenshot=}\";; esac; done\n"), 0o755)
	return path
}

func TestBrowserSavesTheScreenshot(t *testing.T) {
	t.Setenv("ARIA_BROWSER", fakeBrowser(t))
	s := approvalServer(t, config{}, appOptions{skipPermissions: true})
	out, err := s.command(context.Background(), "t", "/browser https://example.com/a?b=c")
	if err != nil || !strings.Contains(out, "Saved ") || !strings.Contains(out, "/vision") {
		t.Fatal(out, err)
	}
	refused := approvalServer(t, config{}, appOptions{})
	if out, _ := refused.command(context.Background(), "t", "/browser https://example.com"); !strings.Contains(out, "not executed") {
		t.Fatal("opened a page without approval", out)
	}
}

// TestRealBrowser runs a real Chrome-family browser:
// ARIA_REAL_BROWSER=/path/to/chrome (headless Chrome needs a working
// sandbox, which some CI runners lack).
func TestRealBrowser(t *testing.T) {
	browser := os.Getenv("ARIA_REAL_BROWSER")
	if browser == "" {
		t.Skip("set ARIA_REAL_BROWSER to a Chrome, Chromium or Edge")
	}
	t.Setenv("ARIA_BROWSER", browser)
	page := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "<html><body><h1>Hello</h1></body></html>")
	}))
	defer page.Close()
	s := approvalServer(t, config{}, appOptions{skipPermissions: true})
	out, err := s.command(context.Background(), "t", "/browser "+page.URL)
	if err != nil || !strings.Contains(out, "Saved ") {
		t.Fatal(out, err)
	}
}

func TestBrowserAndScreenshotRefusals(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	for _, text := range []string{"/browser", "/browser file:///etc/passwd", "/browser javascript:alert(1)"} {
		if _, err := s.command(context.Background(), "t", text); err == nil {
			t.Fatal(text, "accepted")
		}
	}
	off := approvalServer(t, config{"network_enabled": false}, appOptions{})
	if _, err := off.command(context.Background(), "t", "/browser https://example.com"); err == nil || !strings.Contains(err.Error(), "network") {
		t.Fatal(err)
	}
	t.Setenv("ARIA_RUNTIME_SCOPE", "remote")
	if _, err := s.command(context.Background(), "t", "/screenshot"); err == nil {
		t.Fatal("captured a screen in a remote session")
	}
}

func TestScreenCaptureCommandQuotesThePath(t *testing.T) {
	command, err := screenCaptureCommand("/tmp/a b.png")
	if err != nil {
		t.Skip(err)
	}
	if !strings.Contains(command, "a b.png") {
		t.Fatal(command)
	}
}
