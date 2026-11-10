package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/genai"
)

type shortStreamWriter struct{}

func (shortStreamWriter) Write(body []byte) (int, error) { return len(body) - 1, nil }

func decodeStream(t *testing.T, body []byte) []map[string]any {
	t.Helper()
	var events []map[string]any
	scanner := bufio.NewScanner(bytes.NewReader(body))
	for scanner.Scan() {
		var e map[string]any
		if err := json.Unmarshal(scanner.Bytes(), &e); err != nil {
			t.Fatal(err)
		}
		events = append(events, e)
	}
	if err := scanner.Err(); err != nil {
		t.Fatal(err)
	}
	return events
}
func TestNativeHeadlessPreservesEventContractAndNeverInventsApproval(t *testing.T) {
	for _, mode := range []string{"default", "explicit", "persistent-deny", "ask-always"} {
		t.Run(mode, func(t *testing.T) {
			root, workspace := t.TempDir(), t.TempDir()
			var out bytes.Buffer
			options := appOptions{root: root, workspace: workspace, noninteractive: true}
			if mode != "default" {
				options.allowedTools = []string{"write_file"}
			}
			if mode == "persistent-deny" {
				atomicJSON(filepath.Join(root, "tool_policy.json"), map[string]any{"denied": []string{"write_file"}})
			}
			if mode == "ask-always" {
				atomicJSON(filepath.Join(root, "tool_policy.json"), map[string]any{"ask_always": []string{"write_file"}})
				options.skipPermissions = true
			}
			s, err := newServer(options, &out)
			if err != nil {
				t.Fatal(err)
			}
			s.provider = simpleProvider()
			if err := s.runStream(context.Background(), "write note.txt"); err != nil {
				t.Fatal(err)
			}
			events := decodeStream(t, out.Bytes())
			if events[0]["type"] != "turn.started" || events[0]["prompt"] != "write note.txt" || events[0]["model"] != "google/gemini-3.8-flash" {
				t.Fatal(events[0])
			}
			last := events[len(events)-1]
			if last["type"] != "turn.completed" || last["success"] != true || last["response"] != "Operation completed." {
				t.Fatal(last)
			}
			for _, e := range events {
				if strings.HasPrefix(e["type"].(string), "approval.") || strings.HasPrefix(e["type"].(string), "session.") {
					t.Fatal(e)
				}
			}
			body, err := os.ReadFile(filepath.Join(workspace, "note.txt"))
			if mode == "explicit" {
				if err != nil || string(body) != "Actual native file body." {
					t.Fatal(string(body), err)
				}
			} else if !os.IsNotExist(err) {
				t.Fatal("headless task wrote without a usable grant", mode, err)
			}
			loaded, err := loadSession(root, s.snap.ID)
			if err != nil || len(loaded.Messages) != 2 {
				t.Fatal(loaded, err)
			}
		})
	}
}
func TestNativeHeadlessFlushesBeforeGenerationFinishes(t *testing.T) {
	reader, writer := io.Pipe()
	defer reader.Close()
	defer writer.Close()
	s, err := newServer(appOptions{root: t.TempDir(), workspace: t.TempDir()}, writer)
	if err != nil {
		t.Fatal(err)
	}
	finish := make(chan struct{})
	s.provider = providerFunc(func(ctx context.Context, h []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
		emit("实时输出")
		select {
		case <-finish:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return genai.NewContentFromText("实时输出", genai.RoleModel), nil
	})
	done := make(chan error, 1)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go func() { done <- s.runStream(ctx, "你好"); writer.Close() }()
	scanner := bufio.NewScanner(reader)
	for i := 0; i < 2; i++ {
		if !scanner.Scan() {
			t.Fatal("stream ended before delta", scanner.Err())
		}
	}
	var e map[string]any
	json.Unmarshal(scanner.Bytes(), &e)
	if e["type"] != "answer.delta" || e["text"] != "实时输出" {
		t.Fatal(e)
	}
	select {
	case err := <-done:
		t.Fatal("generation finished before release", err)
	default:
	}
	close(finish)
	var last map[string]any
	for scanner.Scan() {
		json.Unmarshal(scanner.Bytes(), &last)
	}
	if last["type"] != "turn.completed" {
		t.Fatal(last)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}
func TestNativeHeadlessFailureAndBrokenOutputCannotReportSuccess(t *testing.T) {
	for _, out := range []io.Writer{&bytes.Buffer{}, shortStreamWriter{}} {
		s, err := newServer(appOptions{root: t.TempDir(), workspace: t.TempDir()}, out)
		if err != nil {
			t.Fatal(err)
		}
		s.provider = providerFunc(func(ctx context.Context, h []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
			return nil, errors.New("configured model unavailable")
		})
		if err := s.runStream(context.Background(), "hello"); err == nil {
			t.Fatal("failed stream reported success")
		}
		if buffer, ok := out.(*bytes.Buffer); ok {
			events := decodeStream(t, buffer.Bytes())
			last := events[len(events)-1]
			if last["success"] != false || last["response"] != "" || last["error"] != "configured model unavailable" {
				t.Fatal(last)
			}
		}
	}
}

func TestHostBarrierAcceptsOnlyANewline(t *testing.T) {
	for input, ok := range map[string]bool{"\n": true, "\r\n": true, "\r": false, "\rx": false, "x\n": false, "": false} {
		if err := hostBarrier(context.Background(), strings.NewReader(input)); (err == nil) != ok {
			t.Errorf("%q: %v", input, err)
		}
	}
}
