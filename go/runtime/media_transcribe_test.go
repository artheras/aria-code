package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestMain lets this test binary stand in for whisper-cli (ARIA_FAKE_WHISPER=1).
func TestMain(m *testing.M) {
	if os.Getenv("ARIA_FAKE_WHISPER") == "1" {
		os.Exit(fakeWhisper(os.Args[1:]))
	}
	os.Exit(m.Run())
}

// fakeWhisper writes <-of>.json in whisper.cpp's shape, echoing its arguments.
func fakeWhisper(args []string) int {
	flags := map[string]string{}
	for i := 0; i+1 < len(args); i++ {
		if strings.HasPrefix(args[i], "-") {
			flags[args[i]] = args[i+1]
		}
	}
	if _, err := os.Stat(flags["-f"]); err != nil {
		os.Stderr.WriteString("no input")
		return 1
	}
	if os.Getenv("ARIA_FAKE_WHISPER_FAIL") == "1" {
		os.Stderr.WriteString("failed to load model")
		return 3
	}
	language := flags["-l"]
	if language == "auto" {
		language = "zh"
	}
	out, _ := json.Marshal(map[string]any{
		"result": map[string]any{"language": language},
		"transcription": []any{
			map[string]any{"offsets": map[string]any{"from": 0, "to": 2500}, "text": " 大家好"},
			map[string]any{"offsets": map[string]any{"from": 2500, "to": 4100}, "text": " "},
			map[string]any{"offsets": map[string]any{"from": 4100, "to": 6000}, "text": " model " + filepath.Base(flags["-m"])},
		},
	})
	if err := os.WriteFile(flags["-of"]+".json", out, 0o644); err != nil {
		return 2
	}
	return 0
}

func TestVideoTranscribe(t *testing.T) {
	home := chartHome(t)
	ctx := context.Background()
	saved := extractSpeech
	t.Cleanup(func() { extractSpeech = saved })
	extractSpeech = func(_ context.Context, input, wav string) error { return os.WriteFile(wav, []byte("RIFF"), 0o644) }
	os.WriteFile(filepath.Join(home, "talk.mp4"), []byte("video"), 0o644)
	models := filepath.Join(home, ".aria", "models", "whisper")
	os.MkdirAll(models, 0o755)

	t.Setenv("ARIA_WHISPER_BIN", filepath.Join(home, "missing"))
	if r := mcpVideoTranscribe(ctx, map[string]any{"input_path": "talk.mp4"}); r["success"] != false || !strings.Contains(r["error"].(string), "ARIA_WHISPER_BIN") {
		t.Fatal(r)
	}
	self, _ := os.Executable()
	t.Setenv("ARIA_WHISPER_BIN", self)
	t.Setenv("ARIA_FAKE_WHISPER", "1")
	if r := mcpVideoTranscribe(ctx, map[string]any{"input_path": "talk.mp4"}); r["success"] != false || !strings.Contains(r["error"].(string), "ggml-base.bin") {
		t.Fatal("a missing model must say which file and where", r)
	}
	os.WriteFile(filepath.Join(models, "ggml-base.bin"), []byte("model"), 0o644)
	r := mcpVideoTranscribe(ctx, map[string]any{"input_path": "talk.mp4"})
	if r["success"] != true || r["language"] != "zh" || r["text"] != "大家好 model ggml-base.bin" {
		t.Fatal(r)
	}
	segments := r["segments"].([]any)
	if len(segments) != 2 || segments[1].(map[string]any)["start"] != 4.1 || segments[0].(map[string]any)["end"] != 2.5 {
		t.Fatal("blank segments are dropped; times are seconds", segments)
	}
	os.WriteFile(filepath.Join(models, "ggml-small.en.bin"), []byte("model"), 0o644)
	if r := mcpVideoTranscribe(ctx, map[string]any{"input_path": "talk.mp4", "model_size": "small.en", "language": "EN"}); r["language"] != "en" || r["model"] != "ggml-small.en.bin" {
		t.Fatal(r)
	}
	t.Setenv("ARIA_FAKE_WHISPER_FAIL", "1")
	if r := mcpVideoTranscribe(ctx, map[string]any{"input_path": "talk.mp4"}); r["success"] != false || !strings.Contains(r["error"].(string), "failed to load model") {
		t.Fatal(r)
	}
	for _, bad := range []map[string]any{
		{"input_path": "/etc/hosts"},
		{"input_path": "talk.mp4", "model_size": "../x"},
		{"input_path": "talk.mp4", "language": "english; rm"},
	} {
		if r := mcpVideoTranscribe(ctx, bad); r["success"] != false {
			t.Fatal("accepted", bad)
		}
	}
}

func TestArtifactsList(t *testing.T) {
	home := chartHome(t)
	ctx := context.Background()
	docs := filepath.Join(home, "docs-root")
	t.Setenv("ARIA_ARTIFACT_ROOT", docs)
	t.Setenv("ARIA_USER_OUTPUT_ROOT", filepath.Join(home, "out"))
	write := func(path, text string, age time.Duration) {
		os.MkdirAll(filepath.Dir(path), 0o755)
		os.WriteFile(path, []byte(text), 0o644)
		when := time.Now().Add(-age)
		os.Chtimes(path, when, when)
	}
	state := filepath.Join(home, ".aria", "artifacts")
	write(filepath.Join(state, "chart", "AAPL_chart.svg"), "<svg/>", time.Hour)
	write(filepath.Join(state, "report", "q3.pdf"), "%PDF", time.Minute)
	write(filepath.Join(state, "report", ".hidden", "x"), "x", 0)
	report := filepath.Join(docs, "reports", "msft.html")
	write(report, "<html>", 2*time.Hour)
	meta, _ := json.Marshal(map[string]any{"kind": "report", "status": "complete", "created_at": "2026-10-01",
		"artifact": map[string]any{"path": report, "topic": "MSFT"}})
	write(filepath.Join(docs, "reports", "msft.metadata.json"), string(meta), 2*time.Hour)
	write(filepath.Join(docs, "reports", "broken.metadata.json"), "{not json", time.Second)
	write(filepath.Join(home, "out", "generated", "x", "y.metadata.json"), `{"artifact":{"category":"code"}}`, 3*time.Hour)

	r := mcpArtifactsList(ctx, map[string]any{})
	items := r["artifacts"].([]any)
	if r["success"] != true || len(items) != 4 {
		t.Fatal(r)
	}
	first, second := items[0].(artifactItem), items[1].(artifactItem)
	if first.Kind != "report" || filepath.Base(first.Path) != "q3.pdf" || second.Kind != "chart" {
		t.Fatal("newest first, kind from the folder", first, second)
	}
	python := items[2].(artifactItem)
	if python.Topic != "MSFT" || python.Path != report || python.SizeBytes == 0 || python.Status != "complete" {
		t.Fatal(python)
	}
	if code := items[3].(artifactItem); code.Kind != "code" || code.Status != "unknown" {
		t.Fatal(code)
	}
	if r := mcpArtifactsList(ctx, map[string]any{"limit": 1}); len(r["artifacts"].([]any)) != 1 {
		t.Fatal(r)
	}
	if r := mcpArtifactsList(ctx, map[string]any{"limit": 0}); r["success"] != false {
		t.Fatal(r)
	}
}
