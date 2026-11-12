package main

// Cloud and local media clients for aria-runtime mcp: the Go port of
// openai_image_client.py, local_image_provider.py, figma_client.py,
// kling_video_client.py and runway_video_client.py, under the same tool
// names.
//
// Keys come from the environment only (OPENAI_API_KEY, FIGMA_API_KEY,
// KLING_ACCESS_KEY + KLING_SECRET_KEY, RUNWAYML_API_SECRET), as everywhere
// else in the Go runtime. Calls that spend money (image generation and
// edits, video submission) refuse without confirmed=true and are marked
// destructive, so Aria asks a person before each one. Local image
// generation talks to an AUTOMATIC1111-compatible server (A1111, Forge,
// SD.Next) at ARIA_LOCAL_IMAGE_URL instead of loading models in-process.

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/draw"
	_ "image/gif"
	_ "image/jpeg"
	"image/png"
	"io"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	xdraw "golang.org/x/image/draw"
	_ "golang.org/x/image/webp"
)

// Endpoints are variables so tests can point them at a local server.
var (
	openAIImageBase = "https://api.openai.com/v1"
	googleImageBase = "https://us-central1-aiplatform.googleapis.com/v1"
	figmaBase       = "https://api.figma.com/v1"
	klingBase       = "https://api.klingai.com"
	runwayBase      = "https://api.dev.runwayml.com"
	mediaHTTP       = &http.Client{Timeout: 4 * time.Minute}
)

const (
	maxImageUpload           = 50 << 20
	maxImageResponse         = 80 << 20
	maxVideoDownload         = 1 << 30
	openAIImageModel         = "gpt-image-2.6-sunburst"
	openAIImageModelSunburst = "gpt-image-2.6-sunburst"
	openAIImageModelFlare    = "gpt-image-2.6-flare"
	openAIImageModelLegacy   = "gpt-image-1"
	googleImagenModel        = "imagen-3.0-generate-002"
	googleImagenFastModel    = "imagen-3.0-fast-generate-001"
	runwayVersion            = "2024-11-06"
)

func envKey(names ...string) string {
	for _, name := range names {
		if v := strings.TrimSpace(os.Getenv(name)); v != "" {
			return v
		}
	}
	return ""
}

// providerError is a provider's refusal: status and the start of its body.
func providerError(provider string, status int, body []byte) error {
	text := strings.TrimSpace(string(body))
	var parsed struct {
		Error any `json:"error"`
		Msg   any `json:"message"`
	}
	if json.Unmarshal(body, &parsed) == nil {
		if m, ok := parsed.Error.(map[string]any); ok {
			if msg, ok := m["message"].(string); ok {
				text = msg
			}
		} else if msg, ok := parsed.Error.(string); ok && msg != "" {
			text = msg
		} else if msg, ok := parsed.Msg.(string); ok && msg != "" {
			text = msg
		}
	}
	return fmt.Errorf("%s returned HTTP %d: %s", provider, status, capRunes(text, 300))
}

// mediaRequest sends a request and decodes a 2xx JSON reply into out.
func mediaRequest(ctx context.Context, provider, method, target string, headers map[string]string, body io.Reader, out any) error {
	req, err := http.NewRequestWithContext(ctx, method, target, body)
	if err != nil {
		return err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := mediaHTTP.Do(req)
	if err != nil {
		return fmt.Errorf("%s: %w", provider, err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxImageResponse+1))
	if err != nil {
		return fmt.Errorf("%s: %w", provider, err)
	}
	if len(data) > maxImageResponse {
		return fmt.Errorf("%s: the reply is larger than %d MiB", provider, maxImageResponse>>20)
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return providerError(provider, resp.StatusCode, data)
	}
	if out == nil {
		return nil
	}
	if err := json.Unmarshal(data, out); err != nil {
		return fmt.Errorf("%s: unreadable reply: %v", provider, err)
	}
	return nil
}

func jsonBody(v any) io.Reader {
	data, _ := json.Marshal(v)
	return bytes.NewReader(data)
}

func confirmedArg(args map[string]any, what, estimate string) error {
	if args["confirmed"] == true {
		return nil
	}
	return fmt.Errorf("confirmed must be true to %s, which is billed the moment it succeeds; call %s first to see the cost", what, estimate)
}

// ── Images ───────────────────────────────────────────────────────────────────

// imageCostUSD is the default model's published output price per image.
var imageCostUSD = map[string]map[string]float64{
	"low":    {"1024x1024": 0.011, "1024x1536": 0.016, "1536x1024": 0.016},
	"medium": {"1024x1024": 0.042, "1024x1536": 0.063, "1536x1024": 0.063},
	"high":   {"1024x1024": 0.167, "1024x1536": 0.25, "1536x1024": 0.25},
}

var imageCostUSDByModel = map[string]map[string]map[string]float64{
	openAIImageModelSunburst: imageCostUSD,
	openAIImageModelLegacy:   imageCostUSD,
	openAIImageModelFlare: {
		"low":    {"1024x1024": 0.005, "1024x1536": 0.008, "1536x1024": 0.008},
		"medium": {"1024x1024": 0.018, "1024x1536": 0.027, "1536x1024": 0.027},
		"high":   {"1024x1024": 0.065, "1024x1536": 0.098, "1536x1024": 0.098},
	},
	googleImagenModel: {
		"low":    {"1024x1024": 0.04, "1024x1536": 0.04, "1536x1024": 0.04},
		"medium": {"1024x1024": 0.04, "1024x1536": 0.04, "1536x1024": 0.04},
		"high":   {"1024x1024": 0.04, "1024x1536": 0.04, "1536x1024": 0.04},
	},
	googleImagenFastModel: {
		"low":    {"1024x1024": 0.02, "1024x1536": 0.02, "1536x1024": 0.02},
		"medium": {"1024x1024": 0.02, "1024x1536": 0.02, "1536x1024": 0.02},
		"high":   {"1024x1024": 0.02, "1024x1536": 0.02, "1536x1024": 0.02},
	},
}

func resolveImageProviderAndModel(args map[string]any) (string, string, error) {
	prov, _ := args["provider"].(string)
	prov = strings.ToLower(strings.TrimSpace(prov))
	if prov == "" {
		prov = strings.ToLower(strings.TrimSpace(os.Getenv("ARIA_IMAGE_PROVIDER")))
	}
	raw, _ := args["model"].(string)
	raw = strings.TrimSpace(raw)
	if raw == "" {
		raw = strings.TrimSpace(os.Getenv("ARIA_OPENAI_IMAGE_MODEL"))
	}

	switch strings.ToLower(raw) {
	case "imagen", "imagen-3", googleImagenModel:
		return "google", googleImagenModel, nil
	case "imagen-fast", googleImagenFastModel:
		return "google", googleImagenFastModel, nil
	}
	if prov == "google" || prov == "vertex" || prov == "vertexai" {
		if raw == "" {
			if q, _ := args["quality"].(string); q == "low" {
				return "google", googleImagenFastModel, nil
			}
			return "google", googleImagenModel, nil
		}
		return "", "", fmt.Errorf("unsupported Google Cloud image model %q; use %s or %s", raw, googleImagenModel, googleImagenFastModel)
	}
	// When no explicit model/provider is passed and Google Cloud is configured while OPENAI_API_KEY is absent,
	// default to Google Cloud Vertex AI (Imagen 3) so desktop Vertex AI users need no extra API key.
	if raw == "" && prov == "" && envKey("OPENAI_API_KEY") == "" && envKey("VERTEX_PROJECT", "GOOGLE_CLOUD_PROJECT", "GEMINI_API_KEY", "GOOGLE_API_KEY") != "" {
		return "google", googleImagenModel, nil
	}
	m, err := resolveOpenAIImageModel(args)
	if err != nil {
		return "", "", err
	}
	return "openai", m, nil
}

func resolveOpenAIImageModel(args map[string]any) (string, error) {
	raw, _ := args["model"].(string)
	raw = strings.TrimSpace(raw)
	if raw == "" {
		raw = strings.TrimSpace(os.Getenv("ARIA_OPENAI_IMAGE_MODEL"))
	}
	if raw == "" {
		return openAIImageModel, nil
	}
	switch strings.ToLower(raw) {
	case "sunburst", "gpt-image-2.6-sunburst", "gpt-image-2.6":
		return openAIImageModelSunburst, nil
	case "flare", "gpt-image-2.6-flare":
		return openAIImageModelFlare, nil
	case "gpt-image-1":
		return openAIImageModelLegacy, nil
	case "gpt-image-2", "gpt-image-2-2026-04-21":
		return "", errors.New("gpt-image-2 is deprecated (retiring 2027-02-10); migrate to gpt-image-2.6-sunburst or gpt-image-2.6-flare")
	default:
		return "", fmt.Errorf("unsupported OpenAI image model %q; use gpt-image-2.6-sunburst, gpt-image-2.6-flare or gpt-image-1", raw)
	}
}

func imageOptions(args map[string]any) (size, quality string, err error) {
	size, _ = args["size"].(string)
	quality, _ = args["quality"].(string)
	if size == "" {
		size = "1024x1536"
	}
	if quality == "" {
		quality = "high"
	}
	switch size {
	case "1024x1024", "1536x1024", "1024x1536", "auto":
	default:
		return "", "", errors.New("size must be 1024x1024, 1536x1024, 1024x1536 or auto")
	}
	switch quality {
	case "low", "medium", "high", "auto":
	default:
		return "", "", errors.New("quality must be low, medium, high or auto")
	}
	if comp, _ := args["complexity"].(string); comp != "" {
		switch comp {
		case "low", "medium", "high", "auto":
		default:
			return "", "", errors.New("complexity must be low, medium, high or auto")
		}
	}
	if fmtOpt, _ := args["output_format"].(string); fmtOpt != "" {
		switch strings.ToLower(fmtOpt) {
		case "png", "jpeg", "webp", "gif", "svg":
		default:
			return "", "", errors.New("output_format must be png, jpeg, webp, gif or svg")
		}
	}
	return size, quality, nil
}

func mcpEstimateImageCost(_ context.Context, args map[string]any) map[string]any {
	provider, model, err := resolveImageProviderAndModel(args)
	if err != nil {
		return mcpFail(err.Error())
	}
	size, quality, err := imageOptions(args)
	if err != nil {
		return mcpFail(err.Error())
	}
	result := map[string]any{"success": true, "provider": provider, "model": model, "size": size, "quality": quality,
		"note": "Illustrative estimate from published per-image pricing; check current pricing before relying on it."}
	modelTable := imageCostUSDByModel[model]
	if modelTable == nil {
		modelTable = imageCostUSD
	}
	if cost, ok := modelTable[quality][size]; ok {
		result["estimated_cost_usd"] = cost
	} else {
		result["estimated_cost_usd"] = nil
		result["note"] = "Auto size or quality is resolved when the request runs, so it cannot be priced ahead."
	}
	return result
}

// jpegOrientation is the EXIF orientation (1-8) of a JPEG, or 1.
func jpegOrientation(data []byte) int {
	if len(data) < 4 || data[0] != 0xFF || data[1] != 0xD8 {
		return 1
	}
	for i := 2; i+4 <= len(data); {
		if data[i] != 0xFF {
			return 1
		}
		marker := data[i+1]
		if marker == 0xD9 || marker == 0xDA {
			return 1
		}
		size := int(binary.BigEndian.Uint16(data[i+2:]))
		if size < 2 || i+2+size > len(data) {
			return 1
		}
		if seg := data[i+4 : i+2+size]; marker == 0xE1 && bytes.HasPrefix(seg, []byte("Exif\x00\x00")) {
			return tiffOrientation(seg[6:])
		}
		i += 2 + size
	}
	return 1
}

func tiffOrientation(t []byte) int {
	if len(t) < 8 {
		return 1
	}
	var bo binary.ByteOrder
	switch string(t[:2]) {
	case "II":
		bo = binary.LittleEndian
	case "MM":
		bo = binary.BigEndian
	default:
		return 1
	}
	off := int(bo.Uint32(t[4:]))
	if off < 8 || off+2 > len(t) {
		return 1
	}
	n := int(bo.Uint16(t[off:]))
	for k := 0; k < n; k++ {
		e := off + 2 + k*12
		if e+12 > len(t) {
			break
		}
		if bo.Uint16(t[e:]) == 0x0112 {
			if v := int(bo.Uint16(t[e+8:])); v >= 1 && v <= 8 {
				return v
			}
		}
	}
	return 1
}

// orient applies an EXIF orientation so the pixels stand the way they are shown.
func orient(src image.Image, o int) image.Image {
	if o <= 1 || o > 8 {
		return src
	}
	b := src.Bounds()
	w, h := b.Dx(), b.Dy()
	dw, dh := w, h
	if o >= 5 {
		dw, dh = h, w
	}
	dst := image.NewNRGBA(image.Rect(0, 0, dw, dh))
	for y := 0; y < dh; y++ {
		for x := 0; x < dw; x++ {
			var sx, sy int
			switch o {
			case 2:
				sx, sy = w-1-x, y
			case 3:
				sx, sy = w-1-x, h-1-y
			case 4:
				sx, sy = x, h-1-y
			case 5:
				sx, sy = y, x
			case 6:
				sx, sy = y, h-1-x
			case 7:
				sx, sy = w-1-y, h-1-x
			case 8:
				sx, sy = w-1-y, x
			}
			dst.Set(x, y, src.At(b.Min.X+sx, b.Min.Y+sy))
		}
	}
	return dst
}

// loadImage reads a local image upright: one frame (an iPhone MPO's first),
// EXIF orientation applied. maxSide > 0 shrinks it to fit, in multiples of 8.
func loadImage(path string, maxSide int) (image.Image, error) {
	full, err := mcpPathIn(path, true)
	if err != nil {
		return nil, err
	}
	if info, err := os.Stat(full); err != nil || info.Size() > maxImageUpload {
		return nil, fmt.Errorf("%s must be an image of at most %d MiB", filepath.Base(path), maxImageUpload>>20)
	}
	data, err := os.ReadFile(full)
	if err != nil {
		return nil, err
	}
	if cfg, _, err := image.DecodeConfig(bytes.NewReader(data)); err != nil {
		return nil, fmt.Errorf("could not read %s as PNG, JPEG, GIF or WebP: %v", filepath.Base(path), err)
	} else if int64(cfg.Width)*int64(cfg.Height) > 100_000_000 {
		return nil, fmt.Errorf("%s is larger than 100 megapixels", filepath.Base(path))
	}
	img, _, err := image.Decode(bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("could not read %s: %v", filepath.Base(path), err)
	}
	img = orient(img, jpegOrientation(data))
	if maxSide > 0 {
		b := img.Bounds()
		scale := min(1.0, float64(maxSide)/float64(max(b.Dx(), b.Dy())))
		w, h := max(8, int(float64(b.Dx())*scale)/8*8), max(8, int(float64(b.Dy())*scale)/8*8)
		if w != b.Dx() || h != b.Dy() {
			dst := image.NewNRGBA(image.Rect(0, 0, w, h))
			xdraw.CatmullRom.Scale(dst, dst.Bounds(), img, b, xdraw.Src, nil)
			img = dst
		}
	}
	return img, nil
}

// pngBytes encodes img as PNG; without keepAlpha it is flattened onto white.
func pngBytes(img image.Image, keepAlpha bool) ([]byte, error) {
	if !keepAlpha {
		flat := image.NewRGBA(img.Bounds())
		draw.Draw(flat, flat.Bounds(), &image.Uniform{color.White}, image.Point{}, draw.Src)
		draw.Draw(flat, flat.Bounds(), img, img.Bounds().Min, draw.Over)
		img = flat
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// isSVGImage checks whether data is a clean SVG document without embedded scripts.
func isSVGImage(data []byte) bool {
	trimmed := strings.TrimSpace(string(data))
	lower := strings.ToLower(trimmed)
	if !strings.HasPrefix(lower, "<svg") && !(strings.HasPrefix(lower, "<?xml") && strings.Contains(lower, "<svg")) {
		return false
	}
	if strings.Contains(lower, "<script") || strings.Contains(lower, "javascript:") || strings.Contains(lower, "onload=") {
		return false
	}
	return strings.Contains(lower, "</svg>")
}

// saveImage writes base64 image data, after checking it decodes, to a new file.
func saveImage(args map[string]any, name, b64 string) map[string]any {
	data, err := base64.StdEncoding.DecodeString(b64)
	if err != nil {
		return mcpFail("the provider returned image data that is not base64")
	}
	if isSVGImage(data) {
		ext := ".svg"
		if raw, _ := args["output_path"].(string); strings.TrimSpace(raw) != "" {
			ext = strings.ToLower(filepath.Ext(raw))
			if ext != ".svg" {
				return mcpFail("the image is svg; give output_path that extension")
			}
		}
		out, err := artifactOutput(args, "image", name, ext)
		if err != nil {
			return mcpFail(err.Error())
		}
		if err := writeNew(out, data); err != nil {
			return mcpFail(err.Error())
		}
		return map[string]any{"success": true, "path": out, "format": "svg", "bytes": len(data)}
	}
	cfg, format, err := image.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		return mcpFail("the provider returned data that is not an image")
	}
	ext := map[string]string{"png": ".png", "jpeg": ".jpg", "webp": ".webp", "gif": ".gif"}[format]
	if raw, _ := args["output_path"].(string); strings.TrimSpace(raw) != "" {
		ext = strings.ToLower(filepath.Ext(raw))
		if ext != ".png" && ext != ".jpg" && ext != ".jpeg" && ext != ".webp" && ext != ".gif" {
			return mcpFail("output_path must end in .png, .jpg, .webp, .gif or .svg")
		}
		if (ext == ".jpg" || ext == ".jpeg") != (format == "jpeg") || (ext == ".png") != (format == "png") || (ext == ".gif") != (format == "gif") {
			return mcpFail(fmt.Sprintf("the image is %s; give output_path that extension", format))
		}
	}
	out, err := artifactOutput(args, "image", name, ext)
	if err != nil {
		return mcpFail(err.Error())
	}
	if err := writeNew(out, data); err != nil {
		return mcpFail(err.Error())
	}
	return map[string]any{"success": true, "path": out, "format": format, "width": cfg.Width, "height": cfg.Height, "bytes": len(data)}
}

type openAIImageReply struct {
	Data []struct {
		B64 string `json:"b64_json"`
	} `json:"data"`
}

func (r openAIImageReply) image() (string, error) {
	if len(r.Data) == 0 || r.Data[0].B64 == "" {
		return "", errors.New("OpenAI returned no image data")
	}
	return r.Data[0].B64, nil
}

func openAIKey() (string, error) {
	if key := envKey("OPENAI_API_KEY"); key != "" {
		return key, nil
	}
	return "", errors.New("set OPENAI_API_KEY in the environment Aria runs in (see /apikey)")
}

type vertexPredictReply struct {
	Predictions []struct {
		BytesBase64Encoded string `json:"bytesBase64Encoded"`
	} `json:"predictions"`
	Data []struct {
		B64 string `json:"b64_json"`
	} `json:"data"`
}

func (r vertexPredictReply) image() (string, error) {
	if len(r.Predictions) > 0 && r.Predictions[0].BytesBase64Encoded != "" {
		return r.Predictions[0].BytesBase64Encoded, nil
	}
	if len(r.Data) > 0 && r.Data[0].B64 != "" {
		return r.Data[0].B64, nil
	}
	return "", errors.New("Google Cloud Vertex AI returned no image data")
}

func sizeToAspectRatio(size string) string {
	switch size {
	case "1536x1024":
		return "4:3"
	case "1024x1536":
		return "3:4"
	default:
		return "1:1"
	}
}

func mcpGenerateImageGoogle(ctx context.Context, args map[string]any, model, prompt, size string) map[string]any {
	project := envKey("VERTEX_PROJECT", "GOOGLE_CLOUD_PROJECT")
	location := envKey("VERTEX_LOCATION", "GOOGLE_CLOUD_LOCATION")
	if location == "" || location == "global" {
		location = "us-central1"
	}
	token := envKey("VERTEX_ACCESS_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY")
	if project == "" && token == "" {
		return mcpFail("set GOOGLE_CLOUD_PROJECT (with ADC) or GEMINI_API_KEY in the environment Aria runs in")
	}
	if project == "" {
		project = "default-project"
	}
	target := fmt.Sprintf("%s/projects/%s/locations/%s/publishers/google/models/%s:predict",
		strings.TrimRight(googleImageBase, "/"), url.PathEscape(project), url.PathEscape(location), url.PathEscape(model))
	headers := map[string]string{"Content-Type": "application/json", "X-Goog-User-Project": project}
	if token != "" {
		headers["Authorization"] = "Bearer " + token
	}
	payload := map[string]any{
		"instances":  []map[string]any{{"prompt": prompt}},
		"parameters": map[string]any{"sampleCount": 1, "aspectRatio": sizeToAspectRatio(size)},
	}
	var reply vertexPredictReply
	if err := mediaRequest(ctx, "Google Cloud Vertex AI", http.MethodPost, target, headers, jsonBody(payload), &reply); err != nil {
		return mcpFail(err.Error())
	}
	b64, err := reply.image()
	if err != nil {
		return mcpFail(err.Error())
	}
	res := saveImage(args, "generated", b64)
	if res["success"] == true {
		res["provider"] = "google"
		res["model"] = model
	}
	return res
}

func mcpGenerateImage(ctx context.Context, args map[string]any) map[string]any {
	if err := confirmedArg(args, "generate an image", "aria.report.estimate_image_cost"); err != nil {
		return mcpFail(err.Error())
	}
	prompt, _ := args["prompt"].(string)
	if strings.TrimSpace(prompt) == "" || len(prompt) > 32000 {
		return mcpFail("prompt is required (up to 32,000 characters)")
	}
	provider, model, err := resolveImageProviderAndModel(args)
	if err != nil {
		return mcpFail(err.Error())
	}
	size, quality, err := imageOptions(args)
	if err != nil {
		return mcpFail(err.Error())
	}
	if provider == "google" {
		return mcpGenerateImageGoogle(ctx, args, model, prompt, size)
	}
	key, err := openAIKey()
	if err != nil {
		return mcpFail(err.Error())
	}
	reqPayload := map[string]any{"model": model, "prompt": prompt, "size": size, "quality": quality}
	if comp, _ := args["complexity"].(string); comp != "" {
		reqPayload["complexity"] = comp
	}
	if fmtOpt, _ := args["output_format"].(string); fmtOpt != "" {
		reqPayload["output_format"] = strings.ToLower(fmtOpt)
	}
	var reply openAIImageReply
	err = mediaRequest(ctx, "OpenAI", http.MethodPost, openAIImageBase+"/images/generations",
		map[string]string{"Authorization": "Bearer " + key, "Content-Type": "application/json"},
		jsonBody(reqPayload), &reply)
	if err != nil {
		return mcpFail(err.Error())
	}
	b64, err := reply.image()
	if err != nil {
		return mcpFail(err.Error())
	}
	res := saveImage(args, "generated", b64)
	if res["success"] == true {
		res["provider"] = "openai"
		res["model"] = model
	}
	return res
}

func mcpEditImage(ctx context.Context, args map[string]any) map[string]any {
	if err := confirmedArg(args, "edit an image", "aria.report.estimate_image_cost"); err != nil {
		return mcpFail(err.Error())
	}
	prompt, _ := args["prompt"].(string)
	imagePath, _ := args["image_path"].(string)
	if strings.TrimSpace(prompt) == "" || strings.TrimSpace(imagePath) == "" {
		return mcpFail("image_path and prompt are required")
	}
	model, err := resolveOpenAIImageModel(args)
	if err != nil {
		return mcpFail(err.Error())
	}
	size, quality, err := imageOptions(args)
	if err != nil {
		return mcpFail(err.Error())
	}
	key, err := openAIKey()
	if err != nil {
		return mcpFail(err.Error())
	}
	var body bytes.Buffer
	form := multipart.NewWriter(&body)
	fields := map[string]string{"model": model, "prompt": prompt, "size": size, "quality": quality}
	if comp, _ := args["complexity"].(string); comp != "" {
		fields["complexity"] = comp
	}
	if fmtOpt, _ := args["output_format"].(string); fmtOpt != "" {
		fields["output_format"] = strings.ToLower(fmtOpt)
	}
	if regionHint, _ := args["region_hint"].(string); strings.TrimSpace(regionHint) != "" {
		fields["region_hint"] = strings.TrimSpace(regionHint)
	}
	for k, v := range fields {
		form.WriteField(k, v)
	}
	addImage := func(field, path string, keepAlpha bool) error {
		img, err := loadImage(path, 0)
		if err != nil {
			return err
		}
		data, err := pngBytes(img, keepAlpha)
		if err != nil {
			return err
		}
		h := textproto.MIMEHeader{}
		stem := strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
		h.Set("Content-Disposition", fmt.Sprintf(`form-data; name="%s"; filename="%s.png"`, field, strings.NewReplacer(`"`, "", "\\", "").Replace(stem)))
		h.Set("Content-Type", "image/png")
		part, err := form.CreatePart(h)
		if err == nil {
			_, err = part.Write(data)
		}
		return err
	}
	if err := addImage("image", imagePath, false); err != nil {
		return mcpFail(err.Error())
	}
	if mask, _ := args["mask_path"].(string); strings.TrimSpace(mask) != "" {
		if err := addImage("mask", mask, true); err != nil {
			return mcpFail("mask_path: " + err.Error())
		}
	}
	form.Close()
	var reply openAIImageReply
	err = mediaRequest(ctx, "OpenAI", http.MethodPost, openAIImageBase+"/images/edits",
		map[string]string{"Authorization": "Bearer " + key, "Content-Type": form.FormDataContentType()}, &body, &reply)
	if err != nil {
		return mcpFail(err.Error())
	}
	b64, err := reply.image()
	if err != nil {
		return mcpFail(err.Error())
	}
	stem := strings.TrimSuffix(filepath.Base(imagePath), filepath.Ext(imagePath))
	res := saveImage(args, stem+"_edited", b64)
	if res["success"] == true {
		res["model"] = model
	}
	return res
}

// ── Local images (AUTOMATIC1111-compatible API) ─────────────────────────────

func localImageBase() (string, error) {
	base := envKey("ARIA_LOCAL_IMAGE_URL")
	if base == "" {
		base = "http://127.0.0.1:7860"
	}
	u, err := url.Parse(base)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return "", errors.New("ARIA_LOCAL_IMAGE_URL must be an http(s) URL of an AUTOMATIC1111-compatible server")
	}
	return strings.TrimRight(base, "/"), nil
}

func intArg(args map[string]any, key string, def, lo, hi int) (int, error) {
	v, ok := args[key]
	if !ok || v == nil {
		return def, nil
	}
	f, ok := chartNumber(v)
	if !ok || f != float64(int(f)) || int(f) < lo || int(f) > hi {
		return 0, fmt.Errorf("%s must be a whole number from %d to %d", key, lo, hi)
	}
	return int(f), nil
}

func floatArg(args map[string]any, key string, def, lo, hi float64) (float64, error) {
	v, ok := args[key]
	if !ok || v == nil {
		return def, nil
	}
	f, ok := chartNumber(v)
	if !ok || f < lo || f > hi {
		return 0, fmt.Errorf("%s must be a number from %g to %g", key, lo, hi)
	}
	return f, nil
}

type localImageReply struct {
	Images []string `json:"images"`
}

func localImageCall(ctx context.Context, endpoint string, payload map[string]any) (string, error) {
	base, err := localImageBase()
	if err != nil {
		return "", err
	}
	var reply localImageReply
	err = mediaRequest(ctx, "the local image server", http.MethodPost, base+endpoint,
		map[string]string{"Content-Type": "application/json"}, jsonBody(payload), &reply)
	if err != nil {
		var netErr interface{ Timeout() bool }
		if errors.As(err, &netErr) || strings.Contains(err.Error(), "connection refused") {
			err = fmt.Errorf("%v; start an AUTOMATIC1111-compatible server (A1111, Forge, SD.Next) with --api, or set ARIA_LOCAL_IMAGE_URL", err)
		}
		return "", err
	}
	if len(reply.Images) == 0 || reply.Images[0] == "" {
		return "", errors.New("the local image server returned no image")
	}
	b64 := reply.Images[0]
	if i := strings.Index(b64, ","); strings.HasPrefix(b64, "data:") && i > 0 {
		b64 = b64[i+1:]
	}
	return b64, nil
}

func mcpGenerateImageLocal(ctx context.Context, args map[string]any) map[string]any {
	prompt, _ := args["prompt"].(string)
	if strings.TrimSpace(prompt) == "" {
		return mcpFail("prompt is required")
	}
	width, err := intArg(args, "width", 1024, 64, 2048)
	if err != nil {
		return mcpFail(err.Error())
	}
	height, err := intArg(args, "height", 1024, 64, 2048)
	if err != nil {
		return mcpFail(err.Error())
	}
	steps, err := intArg(args, "steps", 4, 1, 150)
	if err != nil {
		return mcpFail(err.Error())
	}
	cfg, err := floatArg(args, "cfg_scale", 1, 0, 30)
	if err != nil {
		return mcpFail(err.Error())
	}
	b64, err := localImageCall(ctx, "/sdapi/v1/txt2img", map[string]any{"prompt": prompt, "width": width, "height": height, "steps": steps, "cfg_scale": cfg})
	if err != nil {
		return mcpFail(err.Error())
	}
	return saveImage(args, "generated_local", b64)
}

func mcpEditImageLocal(ctx context.Context, args map[string]any) map[string]any {
	prompt, _ := args["prompt"].(string)
	imagePath, _ := args["image_path"].(string)
	if strings.TrimSpace(prompt) == "" || strings.TrimSpace(imagePath) == "" {
		return mcpFail("image_path and prompt are required")
	}
	strength, err := floatArg(args, "strength", 0.6, 0, 1)
	if err != nil {
		return mcpFail(err.Error())
	}
	steps, err := intArg(args, "steps", 4, 1, 150)
	if err != nil {
		return mcpFail(err.Error())
	}
	cfg, err := floatArg(args, "cfg_scale", 1, 0, 30)
	if err != nil {
		return mcpFail(err.Error())
	}
	// SDXL-class models are trained near 1024 px; a 12 MP phone photo runs out of memory.
	img, err := loadImage(imagePath, 1024)
	if err != nil {
		return mcpFail(err.Error())
	}
	data, err := pngBytes(img, false)
	if err != nil {
		return mcpFail(err.Error())
	}
	b := img.Bounds()
	b64, err := localImageCall(ctx, "/sdapi/v1/img2img", map[string]any{"prompt": prompt, "init_images": []string{base64.StdEncoding.EncodeToString(data)},
		"denoising_strength": strength, "steps": steps, "cfg_scale": cfg, "width": b.Dx(), "height": b.Dy()})
	if err != nil {
		return mcpFail(err.Error())
	}
	stem := strings.TrimSuffix(filepath.Base(imagePath), filepath.Ext(imagePath))
	return saveImage(args, stem+"_edited_local", b64)
}

// ── Figma (read only) ────────────────────────────────────────────────────────

var figmaFileKey = regexp.MustCompile(`^[A-Za-z0-9]{10,64}$`)

// figmaKeyArg accepts a file key or a figma.com file/design URL.
func figmaKeyArg(args map[string]any) (string, error) {
	raw, _ := args["file_key"].(string)
	raw = strings.TrimSpace(raw)
	if u, err := url.Parse(raw); err == nil && strings.HasSuffix(u.Host, "figma.com") {
		parts := strings.Split(strings.Trim(u.Path, "/"), "/")
		for i := 0; i+1 < len(parts); i++ {
			if parts[i] == "file" || parts[i] == "design" || parts[i] == "board" || parts[i] == "proto" {
				raw = parts[i+1]
				if i+3 < len(parts) && parts[i+2] == "branch" {
					raw = parts[i+3]
				}
				break
			}
		}
	}
	if !figmaFileKey.MatchString(raw) {
		return "", errors.New("file_key is required: the key from the file's URL (figma.com/design/<file_key>/…)")
	}
	return raw, nil
}

func figmaGet(ctx context.Context, path string, out any) error {
	token := envKey("FIGMA_API_KEY", "FIGMA_TOKEN")
	if token == "" {
		return errors.New("set FIGMA_API_KEY (a Figma personal access token) in the environment Aria runs in")
	}
	return mediaRequest(ctx, "Figma", http.MethodGet, figmaBase+path, map[string]string{"X-Figma-Token": token}, nil, out)
}

func summarizeFigmaNode(node map[string]any, depth int) map[string]any {
	out := map[string]any{"name": node["name"], "type": node["type"]}
	if id, ok := node["id"].(string); ok {
		out["id"] = id
	}
	children, _ := node["children"].([]any)
	if len(children) > 0 && depth > 0 {
		var kids []any
		for _, c := range children {
			if m, ok := c.(map[string]any); ok {
				kids = append(kids, summarizeFigmaNode(m, depth-1))
			}
		}
		out["children"] = kids
	} else if len(children) > 0 {
		out["child_count"] = len(children)
	}
	return out
}

func mcpFigmaReadFile(ctx context.Context, args map[string]any) map[string]any {
	key, err := figmaKeyArg(args)
	if err != nil {
		return mcpFail(err.Error())
	}
	depth, err := intArg(args, "depth", 2, 0, 6)
	if err != nil {
		return mcpFail(err.Error())
	}
	var file struct {
		Name         string         `json:"name"`
		LastModified string         `json:"lastModified"`
		Document     map[string]any `json:"document"`
	}
	// The API's own depth bounds the download; pages are depth 1.
	if err := figmaGet(ctx, "/files/"+key+"?depth="+strconv.Itoa(depth+2), &file); err != nil {
		return mcpFail(err.Error())
	}
	var pages []any
	children, _ := file.Document["children"].([]any)
	for _, p := range children {
		if m, ok := p.(map[string]any); ok {
			pages = append(pages, summarizeFigmaNode(m, depth))
		}
	}
	return map[string]any{"success": true, "file_key": key, "name": file.Name, "last_modified": file.LastModified, "pages": pages}
}

func mcpFigmaComments(ctx context.Context, args map[string]any) map[string]any {
	key, err := figmaKeyArg(args)
	if err != nil {
		return mcpFail(err.Error())
	}
	var reply struct {
		Comments []struct {
			Message    string  `json:"message"`
			CreatedAt  string  `json:"created_at"`
			ResolvedAt *string `json:"resolved_at"`
			User       struct {
				Handle string `json:"handle"`
			} `json:"user"`
		} `json:"comments"`
	}
	if err := figmaGet(ctx, "/files/"+key+"/comments", &reply); err != nil {
		return mcpFail(err.Error())
	}
	comments := make([]any, 0, len(reply.Comments))
	for _, c := range reply.Comments {
		comments = append(comments, map[string]any{"message": c.Message, "user": c.User.Handle, "created_at": c.CreatedAt, "resolved_at": c.ResolvedAt})
	}
	return map[string]any{"success": true, "file_key": key, "comments": comments}
}

// ── Video generation (Kling, Runway) ────────────────────────────────────────

type videoProvider struct {
	costPerSecond float64
	durations     []int
	ratios        []string
	submit        func(ctx context.Context, prompt string, duration int, ratio string) (string, error)
	poll          func(ctx context.Context, id string) (status string, videoURL string, detail map[string]any, err error)
}

// klingJWT is the HS256 token Kling expects, signed with the secret key.
func klingJWT(now time.Time) (string, error) {
	ak, sk := envKey("KLING_ACCESS_KEY"), envKey("KLING_SECRET_KEY")
	if ak == "" || sk == "" {
		return "", errors.New("set KLING_ACCESS_KEY and KLING_SECRET_KEY in the environment Aria runs in")
	}
	enc := base64.RawURLEncoding
	header := enc.EncodeToString([]byte(`{"alg":"HS256","typ":"JWT"}`))
	claims, _ := json.Marshal(map[string]any{"iss": ak, "exp": now.Unix() + 1800, "nbf": now.Unix() - 5})
	signing := header + "." + enc.EncodeToString(claims)
	mac := hmac.New(sha256.New, []byte(sk))
	mac.Write([]byte(signing))
	return signing + "." + enc.EncodeToString(mac.Sum(nil)), nil
}

func klingHeaders() (map[string]string, error) {
	token, err := klingJWT(time.Now())
	if err != nil {
		return nil, err
	}
	return map[string]string{"Authorization": "Bearer " + token, "Content-Type": "application/json"}, nil
}

func runwayHeaders() (map[string]string, error) {
	key := envKey("RUNWAYML_API_SECRET")
	if key == "" {
		return nil, errors.New("set RUNWAYML_API_SECRET in the environment Aria runs in")
	}
	return map[string]string{"Authorization": "Bearer " + key, "X-Runway-Version": runwayVersion, "Content-Type": "application/json"}, nil
}

var taskIDPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

var videoProviders = map[string]videoProvider{
	"kling": {
		costPerSecond: 0.07, durations: []int{5, 10}, ratios: []string{"16:9", "9:16", "1:1"},
		submit: func(ctx context.Context, prompt string, duration int, ratio string) (string, error) {
			headers, err := klingHeaders()
			if err != nil {
				return "", err
			}
			var reply struct {
				Code    int    `json:"code"`
				Message string `json:"message"`
				Data    struct {
					TaskID string `json:"task_id"`
				} `json:"data"`
			}
			body := map[string]any{"model_name": "kling-v1", "prompt": prompt, "duration": strconv.Itoa(duration), "aspect_ratio": ratio, "mode": "std"}
			if err := mediaRequest(ctx, "Kling", http.MethodPost, klingBase+"/v1/videos/text2video", headers, jsonBody(body), &reply); err != nil {
				return "", err
			}
			if reply.Code != 0 || reply.Data.TaskID == "" {
				return "", fmt.Errorf("Kling refused the job: %s", capRunes(reply.Message, 300))
			}
			return reply.Data.TaskID, nil
		},
		poll: func(ctx context.Context, id string) (string, string, map[string]any, error) {
			headers, err := klingHeaders()
			if err != nil {
				return "", "", nil, err
			}
			var reply struct {
				Code    int    `json:"code"`
				Message string `json:"message"`
				Data    struct {
					Status     string `json:"task_status"`
					StatusMsg  string `json:"task_status_msg"`
					TaskResult struct {
						Videos []struct {
							URL string `json:"url"`
						} `json:"videos"`
					} `json:"task_result"`
				} `json:"data"`
			}
			if err := mediaRequest(ctx, "Kling", http.MethodGet, klingBase+"/v1/videos/text2video/"+id, headers, nil, &reply); err != nil {
				return "", "", nil, err
			}
			if reply.Code != 0 {
				return "", "", nil, fmt.Errorf("Kling: %s", capRunes(reply.Message, 300))
			}
			d := reply.Data
			switch d.Status {
			case "succeed":
				if len(d.TaskResult.Videos) == 0 || d.TaskResult.Videos[0].URL == "" {
					return "failed", "", nil, errors.New("Kling reported success but returned no video")
				}
				return "succeeded", d.TaskResult.Videos[0].URL, nil, nil
			case "failed":
				return "failed", "", map[string]any{"reason": capRunes(d.StatusMsg, 300)}, nil
			}
			return d.Status, "", nil, nil
		},
	},
	"runway": {
		costPerSecond: 0.25, durations: []int{2, 3, 4, 5, 6, 7, 8, 9, 10}, ratios: []string{"1280:720", "720:1280", "1920:1080", "1080:1920"},
		submit: func(ctx context.Context, prompt string, duration int, ratio string) (string, error) {
			headers, err := runwayHeaders()
			if err != nil {
				return "", err
			}
			var reply struct {
				ID string `json:"id"`
			}
			body := map[string]any{"model": "gen4.5", "promptText": prompt, "ratio": ratio, "duration": duration}
			if err := mediaRequest(ctx, "Runway", http.MethodPost, runwayBase+"/v1/text_to_video", headers, jsonBody(body), &reply); err != nil {
				return "", err
			}
			if reply.ID == "" {
				return "", errors.New("Runway returned no task id")
			}
			return reply.ID, nil
		},
		poll: func(ctx context.Context, id string) (string, string, map[string]any, error) {
			headers, err := runwayHeaders()
			if err != nil {
				return "", "", nil, err
			}
			var reply struct {
				Status   string   `json:"status"`
				Output   []string `json:"output"`
				Failure  string   `json:"failure"`
				Progress *float64 `json:"progress"`
			}
			if err := mediaRequest(ctx, "Runway", http.MethodGet, runwayBase+"/v1/tasks/"+id, headers, nil, &reply); err != nil {
				return "", "", nil, err
			}
			switch reply.Status {
			case "SUCCEEDED":
				if len(reply.Output) == 0 {
					return "failed", "", nil, errors.New("Runway reported success but returned no video")
				}
				return "succeeded", reply.Output[0], nil, nil
			case "FAILED":
				return "failed", "", map[string]any{"reason": capRunes(reply.Failure, 300)}, nil
			}
			detail := map[string]any{}
			if reply.Progress != nil {
				detail["progress"] = *reply.Progress
			}
			return strings.ToLower(reply.Status), "", detail, nil
		},
	},
}

func videoProviderArg(args map[string]any) (string, videoProvider, error) {
	name, _ := args["provider"].(string)
	name = strings.ToLower(strings.TrimSpace(name))
	p, ok := videoProviders[name]
	if !ok {
		return "", p, errors.New("provider must be kling or runway")
	}
	return name, p, nil
}

func videoDuration(args map[string]any, p videoProvider) (int, error) {
	d, err := intArg(args, "duration", 5, 1, 60)
	if err != nil {
		return 0, err
	}
	for _, ok := range p.durations {
		if d == ok {
			return d, nil
		}
	}
	return 0, fmt.Errorf("duration must be one of %v seconds for this provider", p.durations)
}

func mcpVideoGenerateEstimate(_ context.Context, args map[string]any) map[string]any {
	name, p, err := videoProviderArg(args)
	if err != nil {
		return mcpFail(err.Error())
	}
	d, err := videoDuration(args, p)
	if err != nil {
		return mcpFail(err.Error())
	}
	return map[string]any{"success": true, "provider": name, "duration_sec": d, "estimated_cost_usd": float64(int(float64(d)*p.costPerSecond*100+0.5)) / 100,
		"note": "Illustrative estimate; check current pricing on your " + name + " account before relying on it."}
}

func mcpVideoGenerateSubmit(ctx context.Context, args map[string]any) map[string]any {
	if err := confirmedArg(args, "submit a video generation job", "aria.video.generate_estimate"); err != nil {
		return mcpFail(err.Error())
	}
	name, p, err := videoProviderArg(args)
	if err != nil {
		return mcpFail(err.Error())
	}
	prompt, _ := args["prompt"].(string)
	if strings.TrimSpace(prompt) == "" || len([]rune(prompt)) > 2500 {
		return mcpFail("prompt is required (up to 2,500 characters)")
	}
	d, err := videoDuration(args, p)
	if err != nil {
		return mcpFail(err.Error())
	}
	ratio, _ := args["aspect_ratio"].(string)
	if ratio == "" {
		ratio = p.ratios[0]
	}
	known := false
	for _, r := range p.ratios {
		known = known || r == ratio
	}
	if !known {
		return mcpFail(fmt.Sprintf("aspect_ratio must be one of %v for %s", p.ratios, name))
	}
	id, err := p.submit(ctx, prompt, d, ratio)
	if err != nil {
		return mcpFail(err.Error())
	}
	return map[string]any{"success": true, "provider": name, "task_id": id, "next": "poll aria.video.generate_status with this task_id; generation takes minutes"}
}

// downloadVideo saves a finished video, at most 1 GiB, to a new file.
func downloadVideo(ctx context.Context, args map[string]any, provider, videoURL string) (string, error) {
	u, err := url.Parse(videoURL)
	if err != nil || u.Scheme != "https" {
		return "", errors.New("the provider's video link is not https")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, videoURL, nil)
	if err != nil {
		return "", err
	}
	resp, err := mediaHTTP.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("the video download returned HTTP %d", resp.StatusCode)
	}
	out, err := artifactOutput(args, "video", provider+"_video", ".mp4")
	if err != nil {
		return "", err
	}
	f, err := os.OpenFile(out, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return "", err
	}
	n, err := io.Copy(f, io.LimitReader(resp.Body, maxVideoDownload+1))
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err == nil && n > maxVideoDownload {
		err = errors.New("the video is larger than 1 GiB")
	}
	if err != nil {
		os.Remove(out)
		return "", err
	}
	return out, nil
}

func mcpVideoGenerateStatus(ctx context.Context, args map[string]any) map[string]any {
	name, p, err := videoProviderArg(args)
	if err != nil {
		return mcpFail(err.Error())
	}
	id, _ := args["task_id"].(string)
	if !taskIDPattern.MatchString(strings.TrimSpace(id)) {
		return mcpFail("task_id is required (from aria.video.generate_submit)")
	}
	status, videoURL, detail, err := p.poll(ctx, strings.TrimSpace(id))
	if err != nil {
		return mcpFail(err.Error())
	}
	result := map[string]any{"success": status != "failed", "provider": name, "task_id": id, "status": status}
	for k, v := range detail {
		result[k] = v
	}
	if status == "failed" {
		result["error"] = "generation failed"
		if reason, _ := detail["reason"].(string); reason != "" {
			result["error"] = reason
		}
		return result
	}
	if videoURL != "" {
		result["video_url"] = videoURL
		if args["download"] != false {
			path, err := downloadVideo(ctx, args, name, videoURL)
			if err != nil {
				result["download_error"] = err.Error()
			} else {
				result["path"] = path
			}
		}
	}
	return result
}

// ── Registration ─────────────────────────────────────────────────────────────

func init() {
	network := func(t mcpServedTool) mcpServedTool {
		t.Annotations["openWorldHint"] = true
		return t
	}
	paid := func(t mcpServedTool) mcpServedTool {
		t.Annotations["openWorldHint"] = true
		t.Annotations["destructiveHint"] = true
		return t
	}
	imageProviderProp := map[string]any{"type": "string", "enum": []any{"google", "openai"}, "description": "Optional provider override: 'google' (Vertex AI Imagen 3, default when GOOGLE_CLOUD_PROJECT is set and OPENAI_API_KEY is unset) or 'openai' (GPT Image 2.6)."}
	modelProp := map[string]any{"type": "string", "enum": []any{"imagen-3.0-generate-002", "imagen-3.0-fast-generate-001", "imagen", "imagen-fast", "gpt-image-2.6-sunburst", "gpt-image-2.6-flare", "sunburst", "flare", "gpt-image-1"}, "description": "Google Cloud Vertex AI Imagen 3 (imagen / imagen-fast) or OpenAI GPT Image 2.6 (sunburst / flare)."}
	sizeProp := map[string]any{"type": "string", "enum": []any{"1024x1024", "1536x1024", "1024x1536", "auto"}, "description": "Default: 1024x1536"}
	qualityProp := map[string]any{"type": "string", "enum": []any{"low", "medium", "high", "auto"}, "description": "Default: high"}
	complexityProp := map[string]any{"type": "string", "enum": []any{"low", "medium", "high", "auto"}, "description": "Optional adjustable complexity for GPT Image 2.6."}
	outputFormatProp := map[string]any{"type": "string", "enum": []any{"png", "jpeg", "webp", "gif", "svg"}, "description": "Optional output format (PNG, JPEG, WebP, animated GIF or vector SVG)."}
	regionHintProp := map[string]any{"type": "string", "description": "Optional marker-assisted local editing region or visual annotation hint for GPT Image 2.6."}
	confirmedProp := map[string]any{"type": "boolean", "description": "Must be true: this is billed the moment it succeeds."}
	imageOut := map[string]any{"type": "string", "description": "Optional new .png/.jpg/.webp/.gif/.svg file (never overwritten); default: a new file under Aria's artifacts/image."}
	providerProp := map[string]any{"type": "string", "enum": []any{"kling", "runway"}}
	mcpServerTools = append(mcpServerTools,
		readOnlyTool("aria.report.estimate_image_cost", "Estimated cost of one Google Cloud Vertex AI Imagen 3 or OpenAI GPT Image 2.6 image at a size and quality. Call before generate_image or edit_image.",
			map[string]any{"provider": imageProviderProp, "model": modelProp, "size": sizeProp, "quality": qualityProp}),
		paid(writeTool("aria.report.generate_image", "Generate an image from a prompt with Google Cloud Vertex AI Imagen 3 (GOOGLE_CLOUD_PROJECT) or OpenAI GPT Image 2.6 (OPENAI_API_KEY), into a new file. Paid: needs confirmed=true.",
			map[string]any{"prompt": map[string]any{"type": "string", "description": "Text prompt, e.g. a skill's compiled poster prompt."},
				"provider": imageProviderProp, "model": modelProp, "size": sizeProp, "quality": qualityProp, "complexity": complexityProp, "output_format": outputFormatProp, "confirmed": confirmedProp, "output_path": imageOut}, "prompt", "confirmed")),
		paid(writeTool("aria.report.edit_image", "Transform a local photo with OpenAI GPT Image 2.6 (sunburst/flare), optionally with a transparent mask or marker-assisted region_hint, into a new file. Paid: needs confirmed=true.",
			map[string]any{"image_path": map[string]any{"type": "string", "description": "The photo to transform (PNG, JPEG, WebP; EXIF rotation is applied)."},
				"prompt":        map[string]any{"type": "string", "description": "How to transform it."},
				"mask_path":     map[string]any{"type": "string", "description": "Optional PNG mask: transparent areas are edited, opaque ones kept."},
				"region_hint":   regionHintProp,
				"model":         modelProp,
				"size":          sizeProp,
				"quality":       qualityProp,
				"complexity":    complexityProp,
				"output_format": outputFormatProp,
				"confirmed":     confirmedProp,
				"output_path":   imageOut}, "image_path", "prompt", "confirmed")),
		network(writeTool("aria.report.generate_image_local", "Generate an image on a local AUTOMATIC1111-compatible server (ARIA_LOCAL_IMAGE_URL, default http://127.0.0.1:7860), into a new file. No cost.",
			map[string]any{"prompt": map[string]any{"type": "string"},
				"width":       map[string]any{"type": "integer", "description": "Default: 1024"},
				"height":      map[string]any{"type": "integer", "description": "Default: 1024"},
				"steps":       map[string]any{"type": "integer", "description": "Default: 4 (SDXL-Turbo)."},
				"cfg_scale":   map[string]any{"type": "number", "description": "Default: 1 (no guidance, for turbo models); about 7 for full models."},
				"output_path": imageOut}, "prompt")),
		network(writeTool("aria.report.edit_image_local", "Restyle a local photo (image-to-image) on a local AUTOMATIC1111-compatible server, into a new file. No cost.",
			map[string]any{"image_path": map[string]any{"type": "string", "description": "The photo to transform; shrunk to 1024 px on the long side."},
				"prompt":      map[string]any{"type": "string"},
				"strength":    map[string]any{"type": "number", "description": "0–1, how far to move from the original (default 0.6)."},
				"steps":       map[string]any{"type": "integer", "description": "Default: 4"},
				"cfg_scale":   map[string]any{"type": "number", "description": "Default: 1"},
				"output_path": imageOut}, "image_path", "prompt")),
		network(readOnlyTool("aria.figma.read_file", "A Figma file's name, last change and page/frame tree to a set depth (FIGMA_API_KEY). Read only.",
			map[string]any{"file_key": map[string]any{"type": "string", "description": "File key, or the file's figma.com URL."},
				"depth": map[string]any{"type": "integer", "description": "Tree levels per page, 0–6 (default 2)."}}, "file_key")),
		network(readOnlyTool("aria.figma.comments", "Comments on a Figma file: message, author, created and resolved times (FIGMA_API_KEY). Read only.",
			map[string]any{"file_key": map[string]any{"type": "string", "description": "File key, or the file's figma.com URL."}}, "file_key")),
		readOnlyTool("aria.video.generate_estimate", "Estimated cost of a Kling or Runway text-to-video job. Call before generate_submit.",
			map[string]any{"provider": providerProp, "duration": map[string]any{"type": "integer", "description": "Seconds: Kling 5 or 10, Runway 2–10 (default 5)."}}, "provider"),
		paid(writeTool("aria.video.generate_submit", "Submit a text-to-video job to Kling (KLING_ACCESS_KEY, KLING_SECRET_KEY) or Runway (RUNWAYML_API_SECRET). Paid: needs confirmed=true. Returns a task_id.",
			map[string]any{"provider": providerProp, "prompt": map[string]any{"type": "string"},
				"duration":     map[string]any{"type": "integer", "description": "Default: 5"},
				"aspect_ratio": map[string]any{"type": "string", "description": "Kling 16:9 (default), 9:16, 1:1; Runway 1280:720 (default), 720:1280, 1920:1080, 1080:1920."},
				"confirmed":    confirmedProp}, "provider", "prompt", "confirmed")),
		network(writeTool("aria.video.generate_status", "Status of a submitted video job; when finished, downloads the video into a new file (download=false to skip).",
			map[string]any{"provider": providerProp, "task_id": map[string]any{"type": "string"},
				"download":    map[string]any{"type": "boolean", "description": "Default: true"},
				"output_path": map[string]any{"type": "string", "description": "Optional new .mp4 file (never overwritten)."}}, "provider", "task_id")),
	)
	for name, handler := range map[string]func(context.Context, map[string]any) map[string]any{
		"aria.report.estimate_image_cost":  mcpEstimateImageCost,
		"aria.report.generate_image":       mcpGenerateImage,
		"aria.report.edit_image":           mcpEditImage,
		"aria.report.generate_image_local": mcpGenerateImageLocal,
		"aria.report.edit_image_local":     mcpEditImageLocal,
		"aria.figma.read_file":             mcpFigmaReadFile,
		"aria.figma.comments":              mcpFigmaComments,
		"aria.video.generate_estimate":     mcpVideoGenerateEstimate,
		"aria.video.generate_submit":       mcpVideoGenerateSubmit,
		"aria.video.generate_status":       mcpVideoGenerateStatus,
	} {
		mcpServerHandlers[name] = handler
	}
}
