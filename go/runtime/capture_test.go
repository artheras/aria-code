package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func TestBrowserScreenshotsAPage(t *testing.T) {
	if os.Getenv("ARIA_BROWSER") == "" && browserBinary() == "" {
		t.Skip("no Chrome-family browser")
	}
	if os.Geteuid() == 0 && os.Getenv("ARIA_BROWSER") == "" {
		t.Skip("Chrome refuses to run as root without --no-sandbox")
	}
	page := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "<html><body><h1>Hello</h1></body></html>")
	}))
	defer page.Close()
	s := approvalServer(t, config{}, appOptions{skipPermissions: true})
	out, err := s.command(context.Background(), "t", "/browser "+page.URL)
	if err != nil || !strings.Contains(out, "Saved ") || !strings.Contains(out, "/vision") {
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
