package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"image"
	"image/color"
	"image/jpeg"
	"image/png"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func tinyPNG(t *testing.T, w, h int) []byte {
	t.Helper()
	img := image.NewNRGBA(image.Rect(0, 0, w, h))
	for x := 0; x < w; x++ {
		img.Set(x, 0, color.NRGBA{255, 0, 0, 255})
	}
	var buf bytes.Buffer
	png.Encode(&buf, img)
	return buf.Bytes()
}

// exifJPEG is a w×h JPEG with an EXIF orientation tag.
func exifJPEG(t *testing.T, w, h, orientation int) []byte {
	t.Helper()
	var buf bytes.Buffer
	jpeg.Encode(&buf, image.NewRGBA(image.Rect(0, 0, w, h)), nil)
	data := buf.Bytes()
	tiff := []byte("MM\x00\x2a\x00\x00\x00\x08\x00\x01\x01\x12\x00\x03\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00")
	binary.BigEndian.PutUint16(tiff[18:], uint16(orientation))
	seg := append([]byte("Exif\x00\x00"), tiff...)
	app1 := []byte{0xFF, 0xE1, 0, 0}
	binary.BigEndian.PutUint16(app1[2:], uint16(len(seg)+2))
	return append(append(append([]byte{}, data[:2]...), append(app1, seg...)...), data[2:]...)
}

func TestImageOrientation(t *testing.T) {
	if got := jpegOrientation(exifJPEG(t, 4, 2, 6)); got != 6 {
		t.Fatal(got)
	}
	if jpegOrientation(tinyPNG(t, 2, 2)) != 1 || jpegOrientation([]byte{0xFF, 0xD8, 0xFF}) != 1 {
		t.Fatal("no EXIF means upright")
	}
	src := image.NewNRGBA(image.Rect(0, 0, 3, 2))
	src.Set(0, 0, color.NRGBA{255, 0, 0, 255}) // top-left marker
	for o, want := range map[int][3]int{2: {3, 2, 2*10 + 0}, 3: {3, 2, 2*10 + 1}, 6: {2, 3, 1*10 + 0}, 8: {2, 3, 0*10 + 2}} {
		out := orient(src, o)
		b := out.Bounds()
		if b.Dx() != want[0] || b.Dy() != want[1] {
			t.Fatal(o, b)
		}
		x, y := want[2]/10, want[2]%10
		if r, _, _, _ := out.At(x, y).RGBA(); r == 0 {
			t.Fatalf("orientation %d: marker not at %d,%d", o, x, y)
		}
	}
	home := chartHome(t)
	os.WriteFile(filepath.Join(home, "phone.jpg"), exifJPEG(t, 40, 20, 6), 0o644)
	img, err := loadImage("phone.jpg", 0)
	if err != nil || img.Bounds().Dx() != 20 || img.Bounds().Dy() != 40 {
		t.Fatal("a portrait phone photo must load upright", err)
	}
	os.WriteFile(filepath.Join(home, "big.png"), tinyPNG(t, 3000, 1000), 0o644)
	if img, err := loadImage("big.png", 1024); err != nil || img.Bounds().Dx() != 1024 || img.Bounds().Dy() != 336 {
		t.Fatal(img.Bounds(), err)
	}
	os.WriteFile(filepath.Join(home, "notes.txt"), []byte("hello"), 0o644)
	if _, err := loadImage("notes.txt", 0); err == nil {
		t.Fatal("text is not an image")
	}
}

// mediaStub is one TLS server standing in for every provider.
type mediaStub struct {
	mu       sync.Mutex
	requests []*http.Request
	bodies   [][]byte
	srv      *httptest.Server
}

func newMediaStub(t *testing.T, handle func(w http.ResponseWriter, r *http.Request, body []byte)) *mediaStub {
	t.Helper()
	s := &mediaStub{}
	s.srv = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		s.mu.Lock()
		s.requests = append(s.requests, r)
		s.bodies = append(s.bodies, body)
		s.mu.Unlock()
		handle(w, r, body)
	}))
	t.Cleanup(s.srv.Close)
	saved := []string{openAIImageBase, figmaBase, klingBase, runwayBase}
	savedClient := mediaHTTP
	openAIImageBase, figmaBase, klingBase, runwayBase = s.srv.URL+"/openai", s.srv.URL+"/figma", s.srv.URL+"/kling", s.srv.URL+"/runway"
	mediaHTTP = s.srv.Client()
	t.Cleanup(func() {
		openAIImageBase, figmaBase, klingBase, runwayBase = saved[0], saved[1], saved[2], saved[3]
		mediaHTTP = savedClient
	})
	return s
}

func (s *mediaStub) last() (*http.Request, []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.requests[len(s.requests)-1], s.bodies[len(s.bodies)-1]
}

func TestOpenAIImageTools(t *testing.T) {
	home := chartHome(t)
	ctx := context.Background()
	png64 := base64.StdEncoding.EncodeToString(tinyPNG(t, 8, 12))
	stub := newMediaStub(t, func(w http.ResponseWriter, r *http.Request, body []byte) {
		if strings.Contains(string(body), "refuse me") {
			w.WriteHeader(400)
			w.Write([]byte(`{"error":{"message":"Your request was rejected by the safety system."}}`))
			return
		}
		w.Write([]byte(`{"data":[{"b64_json":"` + png64 + `"}]}`))
	})
	if r := mcpEstimateImageCost(ctx, map[string]any{}); r["estimated_cost_usd"] != 0.25 {
		t.Fatal(r)
	}
	if r := mcpEstimateImageCost(ctx, map[string]any{"size": "auto"}); r["estimated_cost_usd"] != nil {
		t.Fatal(r)
	}
	if r := mcpGenerateImage(ctx, map[string]any{"prompt": "a poster"}); r["success"] != false || !strings.Contains(r["error"].(string), "confirmed") {
		t.Fatal("unconfirmed paid calls must be refused", r)
	}
	t.Setenv("OPENAI_API_KEY", "")
	if r := mcpGenerateImage(ctx, map[string]any{"prompt": "a poster", "confirmed": true}); r["success"] != false || !strings.Contains(r["error"].(string), "OPENAI_API_KEY") {
		t.Fatal(r)
	}
	if len(stub.requests) != 0 {
		t.Fatal("nothing may be sent before the checks pass")
	}
	t.Setenv("OPENAI_API_KEY", "sk-test")
	r := mcpGenerateImage(ctx, map[string]any{"prompt": "a poster", "confirmed": true, "size": "1024x1024", "quality": "low"})
	if r["success"] != true || r["width"] != 8 || !strings.HasPrefix(r["path"].(string), filepath.Join(home, ".aria", "artifacts", "image")) {
		t.Fatal(r)
	}
	req, body := stub.last()
	if req.URL.Path != "/openai/images/generations" || req.Header.Get("Authorization") != "Bearer sk-test" || !strings.Contains(string(body), `"quality":"low"`) {
		t.Fatal(req.URL.Path, string(body))
	}
	if r := mcpGenerateImage(ctx, map[string]any{"prompt": "refuse me", "confirmed": true}); r["success"] != false || !strings.Contains(r["error"].(string), "safety system") {
		t.Fatal(r)
	}
	if r := mcpGenerateImage(ctx, map[string]any{"prompt": "x", "confirmed": true, "size": "999x999"}); r["success"] != false {
		t.Fatal(r)
	}

	os.WriteFile(filepath.Join(home, "phone.jpg"), exifJPEG(t, 40, 20, 6), 0o644)
	os.WriteFile(filepath.Join(home, "mask.png"), tinyPNG(t, 20, 40), 0o644)
	named := filepath.Join(home, "edited.png")
	r = mcpEditImage(ctx, map[string]any{"image_path": "phone.jpg", "mask_path": "mask.png", "prompt": "duotone", "confirmed": true, "output_path": named})
	if r["success"] != true || !sameFile(r["path"].(string), named) {
		t.Fatal(r)
	}
	req, body = stub.last()
	if err := req.ParseMultipartForm(10 << 20); err != nil {
		// The body was consumed by the stub; parse it again from the copy.
		req.Body = io.NopCloser(bytes.NewReader(body))
		if err := req.ParseMultipartForm(10 << 20); err != nil {
			t.Fatal(err)
		}
	}
	if req.FormValue("prompt") != "duotone" || req.FormValue("model") != openAIImageModel {
		t.Fatal(req.MultipartForm.Value)
	}
	f, _, err := req.FormFile("image")
	if err != nil {
		t.Fatal(err)
	}
	cfg, format, err := image.DecodeConfig(f)
	if err != nil || format != "png" || cfg.Width != 20 || cfg.Height != 40 {
		t.Fatal("the photo must be sent upright as PNG", format, cfg, err)
	}
	if _, _, err := req.FormFile("mask"); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []map[string]any{
		{"image_path": "phone.jpg", "prompt": "x"},
		{"image_path": "/etc/hosts", "prompt": "x", "confirmed": true},
		{"image_path": "phone.jpg", "prompt": "x", "confirmed": true, "output_path": named},
		{"image_path": "phone.jpg", "prompt": "x", "confirmed": true, "output_path": filepath.Join(home, "x.jpg")},
	} {
		if r := mcpEditImage(ctx, bad); r["success"] != false {
			t.Fatal("accepted", bad, r)
		}
	}
}

func TestLocalImageTools(t *testing.T) {
	home := chartHome(t)
	ctx := context.Background()
	var got map[string]any
	png64 := base64.StdEncoding.EncodeToString(tinyPNG(t, 16, 16))
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&got)
		got["path"] = r.URL.Path
		w.Write([]byte(`{"images":["data:image/png;base64,` + png64 + `"]}`))
	}))
	defer srv.Close()
	t.Setenv("ARIA_LOCAL_IMAGE_URL", srv.URL+"/")
	if r := mcpGenerateImageLocal(ctx, map[string]any{"prompt": "a lighthouse", "steps": 6}); r["success"] != true || got["path"] != "/sdapi/v1/txt2img" || got["steps"] != 6.0 || got["cfg_scale"] != 1.0 {
		t.Fatal(r, got)
	}
	os.WriteFile(filepath.Join(home, "wide.png"), tinyPNG(t, 2000, 1000), 0o644)
	if r := mcpEditImageLocal(ctx, map[string]any{"image_path": "wide.png", "prompt": "ink"}); r["success"] != true || got["path"] != "/sdapi/v1/img2img" ||
		got["width"] != 1024.0 || got["height"] != 512.0 || got["denoising_strength"] != 0.6 || len(got["init_images"].([]any)) != 1 {
		t.Fatal(r, got)
	}
	for _, bad := range []map[string]any{{"prompt": ""}, {"prompt": "x", "width": 10}, {"prompt": "x", "steps": 2.5}} {
		if r := mcpGenerateImageLocal(ctx, bad); r["success"] != false {
			t.Fatal("accepted", bad)
		}
	}
	if r := mcpEditImageLocal(ctx, map[string]any{"image_path": "wide.png", "prompt": "x", "strength": 2}); r["success"] != false {
		t.Fatal(r)
	}
	srv.Close()
	if r := mcpGenerateImageLocal(ctx, map[string]any{"prompt": "x"}); r["success"] != false || !strings.Contains(r["error"].(string), "ARIA_LOCAL_IMAGE_URL") {
		t.Fatal("a stopped server should say how to start one", r)
	}
	t.Setenv("ARIA_LOCAL_IMAGE_URL", "file:///etc")
	if r := mcpGenerateImageLocal(ctx, map[string]any{"prompt": "x"}); r["success"] != false {
		t.Fatal(r)
	}
}

func TestFigmaTools(t *testing.T) {
	chartHome(t)
	ctx := context.Background()
	stub := newMediaStub(t, func(w http.ResponseWriter, r *http.Request, _ []byte) {
		if strings.HasSuffix(r.URL.Path, "/comments") {
			w.Write([]byte(`{"comments":[{"message":"Tighten the hero","created_at":"2026-10-30T10:00:00Z","resolved_at":null,"user":{"handle":"lin"}}]}`))
			return
		}
		w.Write([]byte(`{"name":"Landing","lastModified":"2026-10-31T09:00:00Z","document":{"children":[
			{"id":"0:1","name":"Page 1","type":"CANVAS","children":[{"id":"1:2","name":"Hero","type":"FRAME","children":[{"name":"Title","type":"TEXT"},{"name":"CTA","type":"INSTANCE"}]}]}]}}`))
	})
	t.Setenv("FIGMA_API_KEY", "")
	t.Setenv("FIGMA_TOKEN", "")
	if r := mcpFigmaReadFile(ctx, map[string]any{"file_key": "aaaaaaaaaaaa"}); r["success"] != false || !strings.Contains(r["error"].(string), "FIGMA_API_KEY") {
		t.Fatal(r)
	}
	t.Setenv("FIGMA_API_KEY", "figd_test")
	r := mcpFigmaReadFile(ctx, map[string]any{"file_key": "https://www.figma.com/design/aaaaaaaaaaaa/Landing?node-id=1-2", "depth": 1})
	if r["success"] != true || r["file_key"] != "aaaaaaaaaaaa" {
		t.Fatal(r)
	}
	page := r["pages"].([]any)[0].(map[string]any)
	hero := page["children"].([]any)[0].(map[string]any)
	if hero["name"] != "Hero" || hero["child_count"] != 2 || hero["children"] != nil {
		t.Fatal("depth 1 should list frames and count their children", page)
	}
	req, _ := stub.last()
	if req.Header.Get("X-Figma-Token") != "figd_test" || req.URL.Query().Get("depth") != "3" {
		t.Fatal(req.Header, req.URL)
	}
	if r := mcpFigmaComments(ctx, map[string]any{"file_key": "https://www.figma.com/design/aaaaaaaaaaaa/branch/bbbbbbbbbbbb/Landing"}); r["success"] != true ||
		r["file_key"] != "bbbbbbbbbbbb" || r["comments"].([]any)[0].(map[string]any)["user"] != "lin" {
		t.Fatal(r)
	}
	for _, key := range []string{"", "../../me", "short"} {
		if r := mcpFigmaComments(ctx, map[string]any{"file_key": key}); r["success"] != false {
			t.Fatal("accepted", key)
		}
	}
}

func TestKlingJWT(t *testing.T) {
	t.Setenv("KLING_ACCESS_KEY", "ak")
	t.Setenv("KLING_SECRET_KEY", "sk")
	now := time.Unix(1_800_000_000, 0)
	token, err := klingJWT(now)
	if err != nil {
		t.Fatal(err)
	}
	parts := strings.Split(token, ".")
	mac := hmac.New(sha256.New, []byte("sk"))
	mac.Write([]byte(parts[0] + "." + parts[1]))
	if len(parts) != 3 || parts[2] != base64.RawURLEncoding.EncodeToString(mac.Sum(nil)) {
		t.Fatal("bad signature", token)
	}
	claims, _ := base64.RawURLEncoding.DecodeString(parts[1])
	var c map[string]any
	json.Unmarshal(claims, &c)
	if c["iss"] != "ak" || c["exp"] != float64(now.Unix()+1800) || c["nbf"] != float64(now.Unix()-5) {
		t.Fatal(c)
	}
	t.Setenv("KLING_SECRET_KEY", "")
	if _, err := klingJWT(now); err == nil {
		t.Fatal("both keys are needed")
	}
}

func TestVideoGeneration(t *testing.T) {
	home := chartHome(t)
	ctx := context.Background()
	var stub *mediaStub
	stub = newMediaStub(t, func(w http.ResponseWriter, r *http.Request, body []byte) {
		switch {
		case r.URL.Path == "/kling/v1/videos/text2video":
			w.Write([]byte(`{"code":0,"data":{"task_id":"k-123"}}`))
		case r.URL.Path == "/kling/v1/videos/text2video/k-123":
			w.Write([]byte(`{"code":0,"data":{"task_status":"processing"}}`))
		case r.URL.Path == "/kling/v1/videos/text2video/k-bad":
			w.Write([]byte(`{"code":0,"data":{"task_status":"failed","task_status_msg":"content policy"}}`))
		case r.URL.Path == "/runway/v1/text_to_video":
			w.Write([]byte(`{"id":"r-9"}`))
		case r.URL.Path == "/runway/v1/tasks/r-9":
			w.Write([]byte(`{"status":"SUCCEEDED","output":["` + stub.srv.URL + `/files/out.mp4"]}`))
		case r.URL.Path == "/files/out.mp4":
			w.Write([]byte("fake mp4 bytes"))
		default:
			w.WriteHeader(404)
		}
	})
	if r := mcpVideoGenerateEstimate(ctx, map[string]any{"provider": "kling", "duration": 10}); r["estimated_cost_usd"] != 0.7 {
		t.Fatal(r)
	}
	if r := mcpVideoGenerateEstimate(ctx, map[string]any{"provider": "kling", "duration": 7}); r["success"] != false {
		t.Fatal("Kling takes 5 or 10 seconds", r)
	}
	if r := mcpVideoGenerateSubmit(ctx, map[string]any{"provider": "kling", "prompt": "waves"}); r["success"] != false || len(stub.requests) != 0 {
		t.Fatal("unconfirmed submissions must not reach the provider", r)
	}
	t.Setenv("KLING_ACCESS_KEY", "ak")
	t.Setenv("KLING_SECRET_KEY", "sk")
	t.Setenv("RUNWAYML_API_SECRET", "rw")
	if r := mcpVideoGenerateSubmit(ctx, map[string]any{"provider": "kling", "prompt": "waves", "confirmed": true, "aspect_ratio": "9:16"}); r["success"] != true || r["task_id"] != "k-123" {
		t.Fatal(r)
	}
	req, body := stub.last()
	if !strings.HasPrefix(req.Header.Get("Authorization"), "Bearer ey") || !strings.Contains(string(body), `"duration":"5"`) || !strings.Contains(string(body), `"aspect_ratio":"9:16"`) {
		t.Fatal(req.Header, string(body))
	}
	if r := mcpVideoGenerateStatus(ctx, map[string]any{"provider": "kling", "task_id": "k-123"}); r["success"] != true || r["status"] != "processing" || r["path"] != nil {
		t.Fatal(r)
	}
	if r := mcpVideoGenerateStatus(ctx, map[string]any{"provider": "kling", "task_id": "k-bad"}); r["success"] != false || r["error"] != "content policy" {
		t.Fatal(r)
	}
	if r := mcpVideoGenerateSubmit(ctx, map[string]any{"provider": "runway", "prompt": "waves", "confirmed": true, "duration": 8}); r["task_id"] != "r-9" {
		t.Fatal(r)
	}
	req, _ = stub.last()
	if req.Header.Get("X-Runway-Version") != runwayVersion || req.Header.Get("Authorization") != "Bearer rw" {
		t.Fatal(req.Header)
	}
	r := mcpVideoGenerateStatus(ctx, map[string]any{"provider": "runway", "task_id": "r-9"})
	if r["status"] != "succeeded" || !strings.HasPrefix(r["path"].(string), filepath.Join(home, ".aria", "artifacts", "video", "runway_video_")) {
		t.Fatal(r)
	}
	if data, _ := os.ReadFile(r["path"].(string)); string(data) != "fake mp4 bytes" {
		t.Fatal("download")
	}
	for _, bad := range []map[string]any{
		{"provider": "sora", "task_id": "x"},
		{"provider": "runway", "task_id": "../tasks"},
		{"provider": "runway"},
	} {
		if r := mcpVideoGenerateStatus(ctx, bad); r["success"] != false {
			t.Fatal("accepted", bad)
		}
	}
	if r := mcpVideoGenerateSubmit(ctx, map[string]any{"provider": "runway", "prompt": "x", "confirmed": true, "aspect_ratio": "16:9"}); r["success"] != false {
		t.Fatal("Runway ratios are pixel pairs", r)
	}
}
