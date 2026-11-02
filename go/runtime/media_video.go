package main

// Video tools for `aria-runtime mcp`: a port of video_editor.py and the
// scene detection of video_analysis.py. ffmpeg and ffprobe do the work, run
// with an argument list (never a shell) and a deadline. Inputs must sit under
// the working directory or the home directory. Outputs go to
// <state>/artifacts/video unless the caller names a new file there; an
// existing file is never overwritten (ffmpeg -n, where Python used -y).
//
// Changes from Python: scene cuts come from ffmpeg's own scene score instead
// of OpenCV, and text overlays are drawn here with a system font that has the
// text's glyphs (CJK included), then composited by ffmpeg, so neither Pillow
// nor ffmpeg's optional drawtext filter is needed. Transcription (local
// Whisper) is not ported.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/draw"
	"image/png"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"golang.org/x/image/font"
	"golang.org/x/image/font/gofont/goregular"
	"golang.org/x/image/font/opentype"
	"golang.org/x/image/font/sfnt"
	"golang.org/x/image/math/fixed"
)

const ffmpegTimeout = 5 * time.Minute

// mcpPathIn resolves a caller-supplied path that must lie under the working
// directory or the home directory.
func mcpPathIn(value any, mustExist bool) (string, error) {
	text, _ := value.(string)
	if strings.TrimSpace(text) == "" {
		return "", errors.New("a path is required")
	}
	abs, err := filepath.Abs(expandUser(strings.TrimSpace(text)))
	if err != nil {
		return "", err
	}
	check := abs
	if resolved, err := filepath.EvalSymlinks(abs); err == nil {
		check = resolved
	} else if mustExist {
		return "", fmt.Errorf("file not found: %s", text)
	} else if dir, err := filepath.EvalSymlinks(filepath.Dir(abs)); err == nil {
		check = filepath.Join(dir, filepath.Base(abs))
	}
	cwd, _ := os.Getwd()
	bases := []string{cwd}
	if home, err := os.UserHomeDir(); err == nil {
		bases = append(bases, home)
	}
	if state, err := stateRoot(); err == nil {
		bases = append(bases, state)
	}
	for _, base := range bases {
		if resolved, err := filepath.EvalSymlinks(base); err == nil {
			base = resolved
		}
		if base != "" && insideRoot(check, base) {
			if mustExist {
				if info, err := os.Stat(check); err != nil || !info.Mode().IsRegular() {
					return "", fmt.Errorf("file not found: %s", text)
				}
			}
			return check, nil
		}
	}
	return "", errors.New("paths must be inside the server's working directory or your home directory")
}

// videoOutput is the caller's new file, or a fresh name under the artifacts directory.
func videoOutput(args map[string]any, input, suffix, ext string) (string, error) {
	if ext == "" {
		ext = filepath.Ext(input)
	}
	if raw, ok := args["output_path"].(string); ok && strings.TrimSpace(raw) != "" {
		out, err := mcpPathIn(raw, false)
		if err != nil {
			return "", err
		}
		if _, err := os.Stat(out); err == nil {
			return "", errors.New("output_path already exists; choose a new file")
		}
		return out, nil
	}
	state, err := stateRoot()
	if err != nil {
		return "", err
	}
	dir := filepath.Join(state, "artifacts", "video")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	stem := strings.TrimSuffix(filepath.Base(input), filepath.Ext(input))
	return filepath.Join(dir, fmt.Sprintf("%s_%s_%d%s", stem, suffix, time.Now().UnixNano(), ext)), nil
}

func runFFmpeg(ctx context.Context, args ...string) (string, error) {
	if _, err := exec.LookPath("ffmpeg"); err != nil {
		return "", errors.New("ffmpeg not found on PATH; install ffmpeg")
	}
	ctx, cancel := context.WithTimeout(ctx, ffmpegTimeout)
	defer cancel()
	var stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, "ffmpeg", append([]string{"-hide_banner", "-nostdin", "-n"}, args...)...)
	cmd.Stderr = &stderr
	err := cmd.Run()
	if ctx.Err() != nil {
		return "", fmt.Errorf("ffmpeg timed out after %s", ffmpegTimeout)
	}
	if err != nil {
		tail := stderr.String()
		if len(tail) > 800 {
			tail = tail[len(tail)-800:]
		}
		return "", fmt.Errorf("ffmpeg failed: %s", strings.TrimSpace(tail))
	}
	return stderr.String(), nil
}

type videoProbe struct {
	Streams []struct {
		CodecType string `json:"codec_type"`
		CodecName string `json:"codec_name"`
		Width     int    `json:"width"`
		Height    int    `json:"height"`
		FrameRate string `json:"r_frame_rate"`
	} `json:"streams"`
	Format struct {
		Duration string `json:"duration"`
		Size     string `json:"size"`
	} `json:"format"`
}

func probeVideo(ctx context.Context, path string) (videoProbe, error) {
	var p videoProbe
	if _, err := exec.LookPath("ffprobe"); err != nil {
		return p, errors.New("ffprobe not found on PATH; install ffmpeg")
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "ffprobe", "-v", "error", "-show_entries",
		"stream=width,height,codec_type,codec_name,r_frame_rate:format=duration,size", "-of", "json", path).Output()
	if err != nil {
		return p, errors.New("ffprobe could not read the file")
	}
	err = json.Unmarshal(out, &p)
	return p, err
}

func (p videoProbe) video() (w, h int, ok bool) {
	for _, s := range p.Streams {
		if s.CodecType == "video" && s.Width > 0 && s.Height > 0 {
			return s.Width, s.Height, true
		}
	}
	return 0, 0, false
}

func (p videoProbe) hasAudio() bool {
	for _, s := range p.Streams {
		if s.CodecType == "audio" {
			return true
		}
	}
	return false
}

func mcpVideoProbe(ctx context.Context, args map[string]any) map[string]any {
	in, err := mcpPathIn(args["input_path"], true)
	if err != nil {
		return mcpFail(err.Error())
	}
	p, err := probeVideo(ctx, in)
	if err != nil {
		return mcpFail(err.Error())
	}
	duration, _ := strconv.ParseFloat(p.Format.Duration, 64)
	size, _ := strconv.ParseInt(p.Format.Size, 10, 64)
	return map[string]any{"success": true, "path": in, "duration_seconds": duration, "size_bytes": size, "streams": p.Streams}
}

func number(args map[string]any, key string) (float64, bool) {
	v, ok := args[key].(float64)
	return v, ok && !math.IsNaN(v) && !math.IsInf(v, 0)
}

func videoDone(out string, err error) map[string]any {
	if err != nil {
		os.Remove(out) // a partial file is not a result
		return mcpFail(err.Error())
	}
	return map[string]any{"success": true, "path": out}
}

func mcpVideoTrim(ctx context.Context, args map[string]any) map[string]any {
	in, err := mcpPathIn(args["input_path"], true)
	if err != nil {
		return mcpFail(err.Error())
	}
	start, ok1 := number(args, "start")
	end, ok2 := number(args, "end")
	if !ok1 || !ok2 || start < 0 || end <= start {
		return mcpFail("start and end are seconds with 0 <= start < end")
	}
	out, err := videoOutput(args, in, "trimmed", "")
	if err != nil {
		return mcpFail(err.Error())
	}
	_, err = runFFmpeg(ctx, "-ss", fmt.Sprint(start), "-to", fmt.Sprint(end), "-i", in, "-c:v", "libx264", "-c:a", "aac", out)
	return videoDone(out, err)
}

func mcpVideoConcat(ctx context.Context, args map[string]any) map[string]any {
	raw, _ := args["input_paths"].([]any)
	if len(raw) < 2 || len(raw) > 50 {
		return mcpFail("input_paths must list 2 to 50 videos")
	}
	var inputs []string
	var probes []videoProbe
	anyAudio := false
	for _, item := range raw {
		in, err := mcpPathIn(item, true)
		if err != nil {
			return mcpFail(err.Error())
		}
		p, err := probeVideo(ctx, in)
		if err != nil {
			return mcpFail(err.Error())
		}
		if _, _, ok := p.video(); !ok {
			return mcpFail("not a video: " + filepath.Base(in))
		}
		inputs, probes = append(inputs, in), append(probes, p)
		anyAudio = anyAudio || p.hasAudio()
	}
	out, err := videoOutput(args, inputs[0], "concat", "")
	if err != nil {
		return mcpFail(err.Error())
	}
	// The concat filter needs every input at one size, aspect and rate. Each
	// clip is fitted into the first clip's frame (letterboxed, not stretched),
	// and a clip without sound gets silence, so the joined file keeps audio
	// whenever any clip has it.
	w, h, _ := probes[0].video()
	w, h = w/2*2, h/2*2
	var ffArgs, filters, labels []string
	for i, in := range inputs {
		ffArgs = append(ffArgs, "-i", in)
		filters = append(filters, fmt.Sprintf("[%d:v:0]scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v%d]", i, w, h, w, h, i))
		label := fmt.Sprintf("[v%d]", i)
		if anyAudio {
			if probes[i].hasAudio() {
				filters = append(filters, fmt.Sprintf("[%d:a:0]aresample=44100,aformat=channel_layouts=stereo[a%d]", i, i))
			} else {
				d, _ := strconv.ParseFloat(probes[i].Format.Duration, 64)
				filters = append(filters, fmt.Sprintf("anullsrc=channel_layout=stereo:sample_rate=44100,atrim=duration=%.3f[a%d]", d, i))
			}
			label += fmt.Sprintf("[a%d]", i)
		}
		labels = append(labels, label)
	}
	audio := 0
	if anyAudio {
		audio = 1
	}
	filters = append(filters, strings.Join(labels, "")+fmt.Sprintf("concat=n=%d:v=1:a=%d[v]", len(inputs), audio)+map[bool]string{true: "[a]", false: ""}[anyAudio])
	ffArgs = append(ffArgs, "-filter_complex", strings.Join(filters, ";"), "-map", "[v]")
	if anyAudio {
		ffArgs = append(ffArgs, "-map", "[a]", "-c:a", "aac")
	}
	_, err = runFFmpeg(ctx, append(ffArgs, "-c:v", "libx264", out)...)
	return videoDone(out, err)
}

func mcpVideoOverlayAudio(ctx context.Context, args map[string]any) map[string]any {
	in, err := mcpPathIn(args["input_path"], true)
	if err != nil {
		return mcpFail(err.Error())
	}
	audio, err := mcpPathIn(args["audio_path"], true)
	if err != nil {
		return mcpFail(err.Error())
	}
	out, err := videoOutput(args, in, "audio", "")
	if err != nil {
		return mcpFail(err.Error())
	}
	replace, _ := args["replace"].(bool)
	p, _ := probeVideo(ctx, in)
	if replace || !p.hasAudio() {
		_, err = runFFmpeg(ctx, "-i", in, "-i", audio, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-shortest", out)
	} else {
		_, err = runFFmpeg(ctx, "-i", in, "-i", audio, "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first[a]",
			"-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-shortest", out)
	}
	return videoDone(out, err)
}

var videoFormats = map[string]bool{"mp4": true, "mov": true, "mkv": true, "webm": true, "avi": true}

func mcpVideoConvert(ctx context.Context, args map[string]any) map[string]any {
	in, err := mcpPathIn(args["input_path"], true)
	if err != nil {
		return mcpFail(err.Error())
	}
	ext := ""
	if format, _ := args["output_format"].(string); format != "" {
		format = strings.ToLower(strings.TrimPrefix(format, "."))
		if !videoFormats[format] {
			return mcpFail("output_format must be one of mp4, mov, mkv, webm, avi")
		}
		ext = "." + format
	}
	ffArgs := []string{"-i", in}
	if aspect, _ := args["aspect"].(string); aspect != "" {
		w, h, ok := strings.Cut(aspect, ":")
		aw, err1 := strconv.Atoi(w)
		ah, err2 := strconv.Atoi(h)
		if !ok || err1 != nil || err2 != nil || aw < 1 || ah < 1 || aw > 100 || ah > 100 {
			return mcpFail("aspect must look like 9:16")
		}
		// Crop to fill (centre), then make both sides even for the encoder.
		ffArgs = append(ffArgs, "-vf", fmt.Sprintf("crop='min(iw,ih*%d/%d)':'min(ih,iw*%d/%d)',scale=trunc(iw/2)*2:trunc(ih/2)*2", aw, ah, ah, aw))
	}
	out, err := videoOutput(args, in, "converted", ext)
	if err != nil {
		return mcpFail(err.Error())
	}
	codec := []string{"-c:v", "libx264", "-c:a", "aac"}
	if strings.EqualFold(filepath.Ext(out), ".webm") {
		codec = []string{"-c:v", "libvpx-vp9", "-c:a", "libopus"}
	}
	_, err = runFFmpeg(ctx, append(append(ffArgs, codec...), out)...)
	return videoDone(out, err)
}

// atempoChain splits a speed factor into atempo stages, each within 0.5..2.
func atempoChain(factor float64) string {
	var stages []string
	for factor > 2 {
		stages = append(stages, "atempo=2.0")
		factor /= 2
	}
	for factor < 0.5 {
		stages = append(stages, "atempo=0.5")
		factor /= 0.5
	}
	return strings.Join(append(stages, fmt.Sprintf("atempo=%.4f", factor)), ",")
}

func mcpVideoChangeSpeed(ctx context.Context, args map[string]any) map[string]any {
	in, err := mcpPathIn(args["input_path"], true)
	if err != nil {
		return mcpFail(err.Error())
	}
	factor, ok := number(args, "factor")
	if !ok || factor < 0.1 || factor > 16 {
		return mcpFail("factor must be between 0.1 and 16")
	}
	out, err := videoOutput(args, in, "speed", "")
	if err != nil {
		return mcpFail(err.Error())
	}
	p, _ := probeVideo(ctx, in)
	if p.hasAudio() {
		_, err = runFFmpeg(ctx, "-i", in, "-filter_complex", fmt.Sprintf("[0:v]setpts=%.6f*PTS[v];[0:a]%s[a]", 1/factor, atempoChain(factor)),
			"-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-c:a", "aac", out)
	} else {
		_, err = runFFmpeg(ctx, "-i", in, "-vf", fmt.Sprintf("setpts=%.6f*PTS", 1/factor), "-c:v", "libx264", out)
	}
	return videoDone(out, err)
}

var scenePTS = regexp.MustCompile(`pts_time:([0-9.]+)`)

func mcpVideoDetectScenes(ctx context.Context, args map[string]any) map[string]any {
	in, err := mcpPathIn(args["input_path"], true)
	if err != nil {
		return mcpFail(err.Error())
	}
	threshold := 30.0
	if v, ok := number(args, "threshold"); ok {
		threshold = v
	}
	if threshold <= 0 || threshold >= 100 {
		return mcpFail("threshold must be between 0 and 100 (higher finds fewer, surer cuts)")
	}
	if _, err := exec.LookPath("ffmpeg"); err != nil {
		return mcpFail("ffmpeg not found on PATH; install ffmpeg")
	}
	cctx, cancel := context.WithTimeout(ctx, ffmpegTimeout)
	defer cancel()
	var stderr bytes.Buffer
	cmd := exec.CommandContext(cctx, "ffmpeg", "-hide_banner", "-nostdin", "-i", in,
		"-vf", fmt.Sprintf("select='gt(scene,%.4f)',showinfo", threshold/100), "-f", "null", "-")
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return mcpFail("ffmpeg could not analyse the video")
	}
	cuts := []float64{}
	for _, m := range scenePTS.FindAllStringSubmatch(stderr.String(), -1) {
		if v, err := strconv.ParseFloat(m[1], 64); err == nil {
			cuts = append(cuts, math.Round(v*100)/100)
		}
	}
	return map[string]any{"success": true, "scene_cuts_sec": cuts, "threshold": threshold,
		"limitations": []string{"Cuts come from ffmpeg's scene-change score; fades and fast motion can add or hide cuts."}}
}

// textFont is the first installed font that has every glyph of text (so CJK
// captions render), falling back to the Go font for Latin text.
var fontCandidates = []string{
	"/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/Helvetica.ttc",
	"/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
	"/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
	`C:\Windows\Fonts\msyh.ttc`, `C:\Windows\Fonts\arial.ttf`,
}

func textFont(text string) (*opentype.Font, error) {
	covers := func(f *opentype.Font) bool {
		var buf sfnt.Buffer
		for _, r := range text {
			if r == ' ' || r == '\n' {
				continue
			}
			if i, err := f.GlyphIndex(&buf, r); err != nil || i == 0 {
				return false
			}
		}
		return true
	}
	for _, path := range fontCandidates {
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		if collection, err := opentype.ParseCollection(data); err == nil {
			for i := 0; i < collection.NumFonts(); i++ {
				if f, err := collection.Font(i); err == nil && covers(f) {
					return f, nil
				}
			}
		}
	}
	f, err := opentype.Parse(goregular.TTF)
	if err != nil {
		return nil, err
	}
	if !covers(f) {
		return nil, errors.New("no installed font has the glyphs for this text; install a CJK font such as Noto Sans CJK")
	}
	return f, nil
}

func parseColor(name string) (color.RGBA, error) {
	named := map[string]color.RGBA{"white": {255, 255, 255, 255}, "black": {0, 0, 0, 255}, "yellow": {255, 221, 0, 255},
		"red": {230, 40, 40, 255}, "green": {40, 200, 90, 255}, "blue": {40, 120, 240, 255}}
	if c, ok := named[strings.ToLower(name)]; ok {
		return c, nil
	}
	if len(name) == 7 && name[0] == '#' {
		v, err := strconv.ParseUint(name[1:], 16, 32)
		if err == nil {
			return color.RGBA{uint8(v >> 16), uint8(v >> 8), uint8(v), 255}, nil
		}
	}
	return color.RGBA{}, errors.New("font_color must be a name (white, black, yellow, red, green, blue) or #RRGGBB")
}

// captionImage draws text on a transparent frame-sized image, on a
// half-transparent band, at the top, centre or bottom.
func captionImage(width, height int, text, position string, size float64, fg color.RGBA) (*image.RGBA, error) {
	f, err := textFont(text)
	if err != nil {
		return nil, err
	}
	face, err := opentype.NewFace(f, &opentype.FaceOptions{Size: size, DPI: 72, Hinting: font.HintingFull})
	if err != nil {
		return nil, err
	}
	defer face.Close()
	img := image.NewRGBA(image.Rect(0, 0, width, height))
	lines := strings.Split(text, "\n")
	metrics := face.Metrics()
	lineH := (metrics.Ascent + metrics.Descent).Ceil()
	textW := 0
	for _, line := range lines {
		textW = max(textW, font.MeasureString(face, line).Ceil())
	}
	textH := lineH * len(lines)
	const margin, pad = 40, 10
	x := (width - textW) / 2
	y := map[string]int{"top": margin, "center": (height - textH) / 2}[position]
	if position != "top" && position != "center" {
		y = height - textH - margin
	}
	band := image.Rect(max(0, x-pad), max(0, y-pad), min(width, x+textW+pad), min(height, y+textH+pad))
	draw.Draw(img, band, &image.Uniform{color.RGBA{0, 0, 0, 128}}, image.Point{}, draw.Over)
	d := &font.Drawer{Dst: img, Src: &image.Uniform{fg}, Face: face}
	for i, line := range lines {
		d.Dot = fixed.P(x+(textW-font.MeasureString(face, line).Ceil())/2, y+metrics.Ascent.Ceil()+i*lineH)
		d.DrawString(line)
	}
	return img, nil
}

func mcpVideoOverlayText(ctx context.Context, args map[string]any) map[string]any {
	in, err := mcpPathIn(args["input_path"], true)
	if err != nil {
		return mcpFail(err.Error())
	}
	text, _ := args["text"].(string)
	if strings.TrimSpace(text) == "" || len([]rune(text)) > 300 {
		return mcpFail("text must be 1 to 300 characters")
	}
	position, _ := args["position"].(string)
	if position == "" {
		position = "bottom"
	}
	if position != "top" && position != "center" && position != "bottom" {
		return mcpFail("position must be top, center or bottom")
	}
	size := 36.0
	if v, ok := number(args, "font_size"); ok {
		size = v
	}
	if size < 8 || size > 300 {
		return mcpFail("font_size must be between 8 and 300")
	}
	name, _ := args["font_color"].(string)
	if name == "" {
		name = "white"
	}
	fg, err := parseColor(name)
	if err != nil {
		return mcpFail(err.Error())
	}
	p, err := probeVideo(ctx, in)
	if err != nil {
		return mcpFail(err.Error())
	}
	w, h, ok := p.video()
	if !ok {
		return mcpFail("the file has no video stream")
	}
	img, err := captionImage(w, h, text, position, size, fg)
	if err != nil {
		return mcpFail(err.Error())
	}
	tmp, err := os.CreateTemp("", "aria-caption-*.png")
	if err != nil {
		return mcpFail(err.Error())
	}
	defer os.Remove(tmp.Name())
	if err := png.Encode(tmp, img); err != nil {
		tmp.Close()
		return mcpFail(err.Error())
	}
	tmp.Close()
	out, err := videoOutput(args, in, "text", "")
	if err != nil {
		return mcpFail(err.Error())
	}
	_, err = runFFmpeg(ctx, "-i", in, "-i", tmp.Name(), "-filter_complex", "[0:v][1:v]overlay=0:0", "-c:v", "libx264", "-c:a", "copy", out)
	return videoDone(out, err)
}
