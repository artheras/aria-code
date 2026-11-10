package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func needFFmpeg(t *testing.T) {
	t.Helper()
	for _, tool := range []string{"ffmpeg", "ffprobe"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skip(tool + " not installed")
		}
	}
}

// clip makes a short test video with a tone; colour switches make a scene cut.
func clip(t *testing.T, dir, name, size string, colors ...string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	var args []string
	for _, c := range colors {
		args = append(args, "-f", "lavfi", "-i", "color=c="+c+":s="+size+":d=1:r=25")
	}
	args = append(args, "-f", "lavfi", "-i", "sine=frequency=440:duration="+map[int]string{1: "1", 2: "2"}[len(colors)])
	filter := ""
	for i := range colors {
		filter += "[" + string(rune('0'+i)) + ":v]"
	}
	filter += "concat=n=" + string(rune('0'+len(colors))) + ":v=1:a=0[v]"
	args = append(args, "-filter_complex", filter, "-map", "[v]", "-map", string(rune('0'+len(colors)))+":a",
		"-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path)
	if out, err := exec.Command("ffmpeg", append([]string{"-hide_banner", "-loglevel", "error", "-y"}, args...)...).CombinedOutput(); err != nil {
		t.Fatal(string(out), err)
	}
	return path
}

func duration(t *testing.T, path string) float64 {
	t.Helper()
	p, err := probeVideo(context.Background(), path)
	if err != nil {
		t.Fatal(err)
	}
	var d float64
	if _, err := fmtSscan(p.Format.Duration, &d); err != nil {
		t.Fatal(p.Format.Duration, err)
	}
	return d
}

func TestVideoTools(t *testing.T) {
	needFFmpeg(t)
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("ARIA_HOME", filepath.Join(home, ".aria"))
	t.Chdir(home)
	ctx := context.Background()
	a := clip(t, home, "a.mp4", "320x240", "red", "blue")
	b := clip(t, home, "b.mp4", "640x360", "green")

	probe := mcpVideoProbe(ctx, map[string]any{"input_path": a})
	if probe["success"] != true || probe["duration_seconds"].(float64) < 1.9 {
		t.Fatal(probe)
	}
	scenes := mcpVideoDetectScenes(ctx, map[string]any{"input_path": a})
	cuts := scenes["scene_cuts_sec"].([]float64)
	if len(cuts) != 1 || cuts[0] < 0.9 || cuts[0] > 1.1 {
		t.Fatal(scenes)
	}
	trimmed := mcpVideoTrim(ctx, map[string]any{"input_path": a, "start": 0.5, "end": 1.5})
	if trimmed["success"] != true || !strings.HasPrefix(trimmed["path"].(string), filepath.Join(home, ".aria", "artifacts", "video")) {
		t.Fatal(trimmed)
	}
	if d := duration(t, trimmed["path"].(string)); d < 0.9 || d > 1.2 {
		t.Fatal("trim length", d)
	}
	joined := mcpVideoConcat(ctx, map[string]any{"input_paths": []any{a, b}})
	if joined["success"] != true {
		t.Fatal(joined)
	}
	if d := duration(t, joined["path"].(string)); d < 2.8 || d > 3.3 {
		t.Fatal("concat length", d)
	}
	silent := filepath.Join(home, "silent.mp4")
	if out, err := exec.Command("ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=white:s=200x200:d=1:r=10",
		"-c:v", "libx264", "-pix_fmt", "yuv420p", silent).CombinedOutput(); err != nil {
		t.Fatal(string(out), err)
	}
	mixed := mcpVideoConcat(ctx, map[string]any{"input_paths": []any{silent, a}})
	if p, _ := probeVideo(ctx, mixed["path"].(string)); mixed["success"] != true || !p.hasAudio() {
		t.Fatal("a silent clip dropped the audio", mixed)
	}
	fast := mcpVideoChangeSpeed(ctx, map[string]any{"input_path": a, "factor": 4.0})
	if fast["success"] != true {
		t.Fatal(fast)
	}
	if d := duration(t, fast["path"].(string)); d > 0.8 {
		t.Fatal("4x speed length", d)
	}
	vertical := mcpVideoConvert(ctx, map[string]any{"input_path": a, "aspect": "9:16", "output_format": "mkv"})
	if vertical["success"] != true || filepath.Ext(vertical["path"].(string)) != ".mkv" {
		t.Fatal(vertical)
	}
	if p, _ := probeVideo(ctx, vertical["path"].(string)); p.Streams[0].Width*16 > p.Streams[0].Height*9+16 {
		t.Fatal("not cropped to 9:16", p.Streams[0])
	}
	captioned := mcpVideoOverlayText(ctx, map[string]any{"input_path": a, "text": "Q3 revenue 收入", "position": "top", "font_color": "#ffcc00"})
	if _, fontErr := textFont("Q3 revenue 收入"); (fontErr == nil) != (captioned["success"] == true) {
		t.Fatal("a caption fails only when no installed font covers it", captioned, fontErr)
	}
	named := filepath.Join(home, "out.mp4")
	if r := mcpVideoOverlayAudio(ctx, map[string]any{"input_path": b, "audio_path": a, "replace": true, "output_path": named}); r["success"] != true {
		t.Fatal(r)
	}
	// An existing file is never overwritten, and paths stay inside.
	if r := mcpVideoTrim(ctx, map[string]any{"input_path": a, "start": 0.0, "end": 1.0, "output_path": named}); r["success"] != false || !strings.Contains(r["error"].(string), "exists") {
		t.Fatal(r)
	}
	for _, args := range []map[string]any{
		{"input_path": "/etc/hostname", "start": 0.0, "end": 1.0},
		{"input_path": a, "start": 2.0, "end": 1.0},
		{"input_path": a, "start": 0.0, "end": 1.0, "output_path": "/tmp/../etc/x.mp4"},
	} {
		if r := mcpVideoTrim(ctx, args); r["success"] != false {
			t.Fatal("accepted", args, r)
		}
	}
	if r := mcpVideoConvert(ctx, map[string]any{"input_path": a, "output_format": "exe"}); r["success"] != false {
		t.Fatal(r)
	}
	if r := mcpVideoChangeSpeed(ctx, map[string]any{"input_path": a, "factor": 0.0}); r["success"] != false {
		t.Fatal(r)
	}
	if _, err := os.Stat(filepath.Join(home, ".aria", "artifacts", "video")); err != nil {
		t.Fatal(err)
	}
}

func TestAtempoChain(t *testing.T) {
	for factor, want := range map[float64]string{1.5: "atempo=1.5000", 4: "atempo=2.0,atempo=2.0000", 0.25: "atempo=0.5,atempo=0.5000"} {
		if got := atempoChain(factor); got != want {
			t.Fatal(factor, got)
		}
	}
}

func TestCaptionImageDrawsText(t *testing.T) {
	img, err := captionImage(320, 240, "Hello", "bottom", 24, colorWhite)
	if err != nil {
		t.Fatal(err)
	}
	lit := 0
	for y := 150; y < 240; y++ {
		for x := 0; x < 320; x++ {
			if img.RGBAAt(x, y).R > 200 {
				lit++
			}
		}
	}
	if lit < 30 || img.RGBAAt(5, 5).A != 0 {
		t.Fatal("caption not drawn at the bottom on a transparent frame", lit)
	}
}

var colorWhite, _ = parseColor("white")

func fmtSscan(text string, v *float64) (int, error) { return fmt.Sscan(text, v) }
