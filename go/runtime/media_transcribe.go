package main

// aria.video.transcribe for aria-runtime mcp: the Go port of
// video_analysis.transcribe_video. The Python tool loaded faster-whisper
// in-process; this one runs a local whisper.cpp command (whisper-cli) on
// the speech track, so nothing leaves the machine and no Python is needed.
//
// The command is ARIA_WHISPER_BIN, else whisper-cli or whisper-cpp on PATH.
// The model is ARIA_WHISPER_MODEL (a ggml .bin file), else
// ggml-<model_size>.bin in Aria's models/whisper folder or whisper.cpp's
// usual places. Aria never downloads a model.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const transcribeTimeout = 30 * time.Minute

var (
	whisperModelSize = regexp.MustCompile(`^(tiny|base|small|medium|large(-v[1-3])?(-turbo)?)(\.en)?$`)
	whisperLanguage  = regexp.MustCompile(`^[a-z]{2,3}$`)
)

func whisperBinary() (string, error) {
	if configured := envKey("ARIA_WHISPER_BIN"); configured != "" {
		if info, err := os.Stat(configured); err != nil || info.IsDir() {
			return "", fmt.Errorf("ARIA_WHISPER_BIN is not a file: %s", configured)
		}
		return configured, nil
	}
	for _, name := range []string{"whisper-cli", "whisper-cpp"} {
		if path, err := exec.LookPath(name); err == nil {
			return path, nil
		}
	}
	return "", errors.New("no whisper.cpp command found: install whisper.cpp (it provides whisper-cli; e.g. `brew install whisper-cpp`) or set ARIA_WHISPER_BIN")
}

// whisperModel is the ggml model file for a size.
func whisperModel(size string) (string, error) {
	if configured := envKey("ARIA_WHISPER_MODEL"); configured != "" {
		if info, err := os.Stat(configured); err != nil || info.IsDir() {
			return "", fmt.Errorf("ARIA_WHISPER_MODEL is not a file: %s", configured)
		}
		return configured, nil
	}
	name := "ggml-" + size + ".bin"
	var dirs []string
	if state, err := stateRoot(); err == nil {
		dirs = append(dirs, filepath.Join(state, "models", "whisper"))
	}
	if home, err := os.UserHomeDir(); err == nil {
		dirs = append(dirs, filepath.Join(home, ".cache", "whisper.cpp"), filepath.Join(home, ".local", "share", "whisper.cpp"))
	}
	dirs = append(dirs, "/opt/homebrew/share/whisper-cpp", "/usr/local/share/whisper-cpp")
	for _, dir := range dirs {
		path := filepath.Join(dir, name)
		if info, err := os.Stat(path); err == nil && !info.IsDir() {
			return path, nil
		}
	}
	return "", fmt.Errorf("no whisper.cpp model %s: download it from https://huggingface.co/ggerganov/whisper.cpp into %s, or set ARIA_WHISPER_MODEL", name, dirs[0])
}

// extractSpeech writes the 16 kHz mono WAV whisper.cpp reads. A variable so
// tests need no ffmpeg.
var extractSpeech = func(ctx context.Context, input, wav string) error {
	_, err := runFFmpeg(ctx, "-i", input, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav)
	return err
}

type whisperOutput struct {
	Result struct {
		Language string `json:"language"`
	} `json:"result"`
	Transcription []struct {
		Offsets struct {
			From int64 `json:"from"`
			To   int64 `json:"to"`
		} `json:"offsets"`
		Text string `json:"text"`
	} `json:"transcription"`
}

func mcpVideoTranscribe(ctx context.Context, args map[string]any) map[string]any {
	input, err := mcpPathIn(args["input_path"], true)
	if err != nil {
		return mcpFail(err.Error())
	}
	size, _ := args["model_size"].(string)
	size = strings.TrimSpace(size)
	if size == "" {
		size = "base"
	}
	if !whisperModelSize.MatchString(size) {
		return mcpFail("model_size must be tiny, base, small, medium or large (optionally -v3, -turbo or .en)")
	}
	language, _ := args["language"].(string)
	language = strings.ToLower(strings.TrimSpace(language))
	if language == "" {
		language = "auto"
	} else if !whisperLanguage.MatchString(language) {
		return mcpFail("language must be an ISO code such as en or zh; omit it to detect")
	}
	binary, err := whisperBinary()
	if err != nil {
		return mcpFail(err.Error())
	}
	model, err := whisperModel(size)
	if err != nil {
		return mcpFail(err.Error())
	}
	dir, err := os.MkdirTemp("", "aria-transcribe-")
	if err != nil {
		return mcpFail(err.Error())
	}
	defer os.RemoveAll(dir)
	ctx, cancel := context.WithTimeout(ctx, transcribeTimeout)
	defer cancel()
	wav := filepath.Join(dir, "speech.wav")
	if err := extractSpeech(ctx, input, wav); err != nil {
		return mcpFail("could not read the speech track: " + err.Error())
	}
	stem := filepath.Join(dir, "transcript")
	cmd := exec.CommandContext(ctx, binary, "-m", model, "-f", wav, "-l", language, "-oj", "-of", stem, "-np")
	output, err := cmd.CombinedOutput()
	if err != nil {
		return mcpFail(fmt.Sprintf("whisper.cpp failed: %v: %s", err, capRunes(strings.TrimSpace(string(output)), 400)))
	}
	data, err := os.ReadFile(stem + ".json")
	if err != nil {
		return mcpFail("whisper.cpp wrote no transcript")
	}
	var parsed whisperOutput
	if err := json.Unmarshal(data, &parsed); err != nil {
		return mcpFail("whisper.cpp wrote an unreadable transcript")
	}
	segments := make([]any, 0, len(parsed.Transcription))
	var text []string
	for _, s := range parsed.Transcription {
		line := strings.TrimSpace(s.Text)
		if line == "" {
			continue
		}
		segments = append(segments, map[string]any{"start": float64(s.Offsets.From) / 1000, "end": float64(s.Offsets.To) / 1000, "text": line})
		text = append(text, line)
	}
	detected := parsed.Result.Language
	if detected == "" && language != "auto" {
		detected = language
	}
	return map[string]any{"success": true, "language": detected, "segments": segments, "text": strings.Join(text, " "),
		"model": filepath.Base(model)}
}

func init() {
	mcpServerTools = append(mcpServerTools,
		readOnlyTool("aria.video.transcribe", "Transcribe a video's or audio file's speech locally with whisper.cpp: timed segments and the full text. Nothing leaves the machine.",
			map[string]any{
				"input_path": inputPathProp,
				"model_size": map[string]any{"type": "string", "description": "whisper.cpp model: tiny, base (default), small, medium, large-v3 …; its ggml file must be installed."},
				"language":   map[string]any{"type": "string", "description": "ISO language code; omit to detect."},
			}, "input_path"))
	mcpServerHandlers["aria.video.transcribe"] = mcpVideoTranscribe
}
