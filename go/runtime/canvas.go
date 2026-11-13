package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Suffixes that the Canvas preview panel can render.
var renderableArtifactSuffixes = map[string]bool{
	".html":     true,
	".htm":      true,
	".md":       true,
	".markdown": true,
	".svg":      true,
	".png":      true,
	".jpg":      true,
	".jpeg":     true,
	".gif":      true,
	".webp":     true,
	".gltf":     true,
	".glb":      true,
	".obj":      true,
	".stl":      true,
}

const maxArtifactBytes = 8 * 1024 * 1024 // 8 MiB

type canvasArtifactVersion struct {
	Index     int    `json:"index"`
	Path      string `json:"path"`
	CreatedAt string `json:"created_at"`
	Kind      string `json:"kind"`
}

type canvasArtifactThread struct {
	ID        string                  `json:"id"`
	Versions  []canvasArtifactVersion `json:"versions"`
	UpdatedAt float64                 `json:"updated_at"`
}

type canvasBridgeAction struct {
	Type      string         `json:"type"`
	Action    string         `json:"action"`
	Payload   map[string]any `json:"payload,omitempty"`
	Topic     string         `json:"topic,omitempty"`
	Timestamp string         `json:"timestamp"`
}

type canvasServer struct {
	mu           sync.RWMutex
	listener     net.Listener
	httpServer   *http.Server
	handler      http.Handler
	url          string
	threads      map[string]*canvasArtifactThread
	activeThread string
	sseClients   map[chan []byte]bool
	bridgeLogs   []canvasBridgeAction
	discovery    string
}

var (
	activeCanvasMu sync.Mutex
	activeCanvas   *canvasServer
)

func canvasDiscoveryPath() (string, error) {
	root, err := stateRoot()
	if err != nil {
		return "", err
	}
	return filepath.Join(root, "canvas.json"), nil
}

func isProcessAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	if pid == os.Getpid() {
		return true
	}
	proc, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	if runtime.GOOS == "windows" {
		return true
	}
	return proc.Signal(syscall.Signal(0)) == nil
}

type canvasDiscovery struct {
	URL       string  `json:"url"`
	PID       int     `json:"pid"`
	StartedAt float64 `json:"started_at"`
}

func readCanvasDiscovery() (*canvasDiscovery, error) {
	discPath, err := canvasDiscoveryPath()
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(discPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var disc canvasDiscovery
	if err := json.Unmarshal(data, &disc); err != nil {
		return nil, err
	}
	if disc.PID > 0 && !isProcessAlive(disc.PID) {
		_ = os.Remove(discPath)
		return nil, nil
	}
	return &disc, nil
}

func getActiveCanvas() *canvasServer {
	activeCanvasMu.Lock()
	defer activeCanvasMu.Unlock()
	return activeCanvas
}

func (cs *canvasServer) URL() string {
	cs.mu.RLock()
	defer cs.mu.RUnlock()
	return cs.url
}

func kindForSuffix(suffix string) string {
	suffix = strings.ToLower(suffix)
	switch suffix {
	case ".html", ".htm":
		return "html"
	case ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg":
		return "image"
	case ".md", ".markdown":
		return "markdown"
	case ".gltf", ".glb", ".obj", ".stl":
		return "3d"
	default:
		return "file"
	}
}

func (cs *canvasServer) broadcast(payload map[string]any) {
	data, err := json.Marshal(payload)
	if err != nil {
		return
	}
	msg := []byte(fmt.Sprintf("data: %s\n\n", data))

	cs.mu.Lock()
	defer cs.mu.Unlock()
	for ch := range cs.sseClients {
		select {
		case ch <- msg:
		default:
		}
	}
}

func (cs *canvasServer) recordVersion(topic, filePath string) {
	ext := filepath.Ext(filePath)
	kind := kindForSuffix(ext)
	now := time.Now().UTC().Format(time.RFC3339)

	cs.mu.Lock()
	thread, exists := cs.threads[topic]
	if !exists {
		thread = &canvasArtifactThread{ID: topic, Versions: []canvasArtifactVersion{}}
		cs.threads[topic] = thread
	}
	v := canvasArtifactVersion{
		Index:     len(thread.Versions),
		Path:      filePath,
		CreatedAt: now,
		Kind:      kind,
	}
	thread.Versions = append(thread.Versions, v)
	thread.UpdatedAt = float64(time.Now().UnixNano()) / 1e9
	cs.activeThread = topic
	cs.mu.Unlock()

	cs.broadcast(map[string]any{
		"type":          "new-version",
		"thread_id":     topic,
		"version_index": v.Index,
		"url":           fmt.Sprintf("/artifact/%s?v=%d", topic, v.Index),
	})
}

// startCanvasServer starts the canvas preview HTTP server on 127.0.0.1:0 (or ARIA_CANVAS_PORT).
func startCanvasServer() (*canvasServer, error) {
	activeCanvasMu.Lock()
	defer activeCanvasMu.Unlock()
	if activeCanvas != nil && activeCanvas.url != "" {
		return activeCanvas, nil
	}

	discPath, err := canvasDiscoveryPath()
	if err != nil {
		return nil, err
	}

	port := 0
	if envPort := os.Getenv("ARIA_CANVAS_PORT"); envPort != "" {
		fmt.Sscanf(envPort, "%d", &port)
	}

	var listener net.Listener
	if port > 0 {
		l, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
		if err != nil {
			return nil, err
		}
		listener = l
	} else {
		for p := 8765; p <= 8775; p++ {
			l, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", p))
			if err == nil {
				listener = l
				break
			}
		}
		if listener == nil {
			l, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				return nil, err
			}
			listener = l
		}
	}

	actualPort := listener.Addr().(*net.TCPAddr).Port
	url := fmt.Sprintf("http://127.0.0.1:%d/", actualPort)

	cs := &canvasServer{
		listener:   listener,
		url:        url,
		threads:    map[string]*canvasArtifactThread{},
		sseClients: map[chan []byte]bool{},
		discovery:  discPath,
	}

	mux := newCanvasMux(cs)
	cs.handler = mux
	cs.httpServer = &http.Server{
		Handler:      mux,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 60 * time.Second,
	}

	go func() {
		_ = cs.httpServer.Serve(listener)
	}()

	// Write discovery file
	if discPath != "" {
		_ = os.MkdirAll(filepath.Dir(discPath), 0o755)
		data, _ := json.MarshalIndent(map[string]any{
			"url":        url,
			"pid":        os.Getpid(),
			"started_at": float64(time.Now().UnixNano()) / 1e9,
		}, "", "  ")
		_ = atomicWrite(discPath, data)
	}

	activeCanvas = cs
	return cs, nil
}

func stopCanvasServer() error {
	activeCanvasMu.Lock()
	defer activeCanvasMu.Unlock()
	if activeCanvas == nil {
		return nil
	}
	cs := activeCanvas
	activeCanvas = nil

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	if cs.httpServer != nil {
		_ = cs.httpServer.Shutdown(ctx)
	}
	if cs.discovery != "" {
		_ = os.Remove(cs.discovery)
	}

	cs.mu.Lock()
	for ch := range cs.sseClients {
		close(ch)
	}
	cs.sseClients = nil
	cs.mu.Unlock()

	return nil
}

func newCanvasMux(cs *canvasServer) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/", cs.handleIndex)
	mux.HandleFunc("/events", cs.handleEvents)
	mux.HandleFunc("/state", cs.handleState)
	mux.HandleFunc("/api/artifacts", cs.handleState)
	mux.HandleFunc("/artifact/", cs.handleArtifact)
	mux.HandleFunc("/api/bridge", cs.handleBridge)
	return mux
}

func attachCanvasToGateway(gatewayURL string) *canvasServer {
	activeCanvasMu.Lock()
	defer activeCanvasMu.Unlock()
	if activeCanvas != nil {
		return activeCanvas
	}
	cs := &canvasServer{
		url:        strings.TrimRight(gatewayURL, "/") + "/canvas/",
		threads:    map[string]*canvasArtifactThread{},
		sseClients: map[chan []byte]bool{},
	}
	cs.handler = newCanvasMux(cs)
	activeCanvas = cs
	return cs
}

func detachGatewayCanvas(cs *canvasServer) {
	if cs == nil {
		return
	}
	activeCanvasMu.Lock()
	defer activeCanvasMu.Unlock()
	if activeCanvas == cs && cs.httpServer == nil {
		activeCanvas = nil
	}
}

func (cs *canvasServer) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(canvasShellHTML))
}

func (cs *canvasServer) handleEvents(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "Streaming unsupported!", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	clientCh := make(chan []byte, 16)
	cs.mu.Lock()
	cs.sseClients[clientCh] = true
	cs.mu.Unlock()

	defer func() {
		cs.mu.Lock()
		delete(cs.sseClients, clientCh)
		cs.mu.Unlock()
	}()

	notify := r.Context().Done()
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-notify:
			return
		case msg, ok := <-clientCh:
			if !ok {
				return
			}
			_, err := w.Write(msg)
			if err != nil {
				return
			}
			flusher.Flush()
		case <-ticker.C:
			_, err := w.Write([]byte(": ping\n\n"))
			if err != nil {
				return
			}
			flusher.Flush()
		}
	}
}

func (cs *canvasServer) handleState(w http.ResponseWriter, r *http.Request) {
	cs.mu.RLock()
	defer cs.mu.RUnlock()

	threadsPayload := map[string]any{}
	for tid, thread := range cs.threads {
		versions := make([]map[string]any, 0, len(thread.Versions))
		for _, v := range thread.Versions {
			versions = append(versions, map[string]any{
				"index":      v.Index,
				"created_at": v.CreatedAt,
				"kind":       v.Kind,
			})
		}
		threadsPayload[tid] = map[string]any{
			"versions":   versions,
			"updated_at": thread.UpdatedAt,
		}
	}

	resp := map[string]any{
		"threads":          threadsPayload,
		"active_thread_id": cs.activeThread,
	}

	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(resp)
}

func (cs *canvasServer) handleArtifact(w http.ResponseWriter, r *http.Request) {
	threadID := strings.TrimPrefix(r.URL.Path, "/artifact/")
	if threadID == "" {
		http.NotFound(w, r)
		return
	}

	cs.mu.RLock()
	thread := cs.threads[threadID]
	cs.mu.RUnlock()

	if thread == nil || len(thread.Versions) == 0 {
		http.NotFound(w, r)
		return
	}

	idx := len(thread.Versions) - 1
	if vParam := r.URL.Query().Get("v"); vParam != "" {
		var customIdx int
		if _, err := fmt.Sscanf(vParam, "%d", &customIdx); err == nil {
			if customIdx >= 0 && customIdx < len(thread.Versions) {
				idx = customIdx
			}
		}
	}

	version := thread.Versions[idx]
	data, err := os.ReadFile(version.Path)
	if err != nil {
		http.Error(w, fmt.Sprintf("Artifact file missing: %s", version.Path), http.StatusNotFound)
		return
	}

	// Artifact security headers
	w.Header().Set("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com data:; img-src data: blob:; media-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("Cache-Control", "no-store")

	contentType := "application/octet-stream"
	switch version.Kind {
	case "html":
		contentType = "text/html; charset=utf-8"
	case "markdown":
		contentType = "text/markdown; charset=utf-8"
	case "image":
		switch strings.ToLower(filepath.Ext(version.Path)) {
		case ".png":
			contentType = "image/png"
		case ".jpg", ".jpeg":
			contentType = "image/jpeg"
		case ".gif":
			contentType = "image/gif"
		case ".webp":
			contentType = "image/webp"
		case ".svg":
			contentType = "image/svg+xml"
		}
	}

	w.Header().Set("Content-Type", contentType)
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(data)
}

func (cs *canvasServer) handleBridge(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodGet {
		cs.mu.RLock()
		logs := append([]canvasBridgeAction{}, cs.bridgeLogs...)
		cs.mu.RUnlock()
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_ = json.NewEncoder(w).Encode(map[string]any{"success": true, "actions": logs})
		return
	}

	if r.Method == http.MethodPost {
		body, err := io.ReadAll(io.LimitReader(r.Body, 64<<10))
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		var action canvasBridgeAction
		if err := json.Unmarshal(body, &action); err != nil {
			http.Error(w, "invalid JSON bridge payload", http.StatusBadRequest)
			return
		}
		action.Timestamp = time.Now().UTC().Format(time.RFC3339)

		cs.mu.Lock()
		cs.bridgeLogs = append(cs.bridgeLogs, action)
		if len(cs.bridgeLogs) > 100 {
			cs.bridgeLogs = cs.bridgeLogs[len(cs.bridgeLogs)-100:]
		}
		cs.mu.Unlock()

		if action.Action == "version" || action.Type == "version" {
			topic, _ := action.Payload["topic"].(string)
			path, _ := action.Payload["path"].(string)
			if topic != "" && path != "" {
				cs.recordVersion(topic, path)
			}
		}

		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_ = json.NewEncoder(w).Encode(map[string]any{"success": true, "received": action.Action})
		return
	}

	http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
}

// publishArtifactTool publishes an artifact, writes to disk & metadata, and alerts live canvas.
func publishArtifactTool(ctx context.Context, sc *scope, root string, params map[string]any) toolResult {
	rawPath := strings.TrimSpace(str(params, "path", ""))
	contentVal, hasContent := params["content"]

	var payload []byte
	var suffix, stem string

	if rawPath != "" && !hasContent {
		// Read existing workspace file (must be inside workspace, not just arbitrary readRoot)
		cleanPath := rawPath
		if !filepath.IsAbs(cleanPath) {
			cleanPath = filepath.Join(sc.workspace, cleanPath)
		}
		cleanPath = filepath.Clean(cleanPath)
		if !insideRoot(cleanPath, sc.workspace) {
			return failed(fmt.Errorf("Refusing to publish %s: it is outside the workspace. Publishing renders a file in a browser; only workspace files may be published.", rawPath))
		}

		p, err := sc.open(rawPath, false)
		if err != nil {
			return failed(fmt.Errorf("Refusing to publish %s: %w. Publishing renders a file in a browser; only workspace files may be published.", rawPath, err))
		}
		defer p.Close()

		data, err := readRegular(p, maxArtifactBytes)
		if err != nil {
			return failed(fmt.Errorf("Could not read %s: %w", rawPath, err))
		}
		payload = data
		suffix = strings.ToLower(filepath.Ext(rawPath))
		stem = strings.TrimSuffix(filepath.Base(rawPath), suffix)
	} else if hasContent {
		filename := strings.TrimSpace(str(params, "filename", ""))
		if filename == "" {
			return failed(errors.New("Missing 'filename' — needed to know how to render the content"))
		}
		if filepath.Base(filename) != filename || strings.Contains(filename, "/") || strings.Contains(filename, "\\") {
			return failed(errors.New("'filename' must be a bare file name, not a path"))
		}
		var text string
		if s, ok := contentVal.(string); ok {
			text = s
		} else {
			text = fmt.Sprint(contentVal)
		}
		payload = []byte(text)
		if len(payload) > maxArtifactBytes {
			return failed(errors.New("Content exceeds 8MB limit"))
		}
		suffix = strings.ToLower(filepath.Ext(filename))
		stem = strings.TrimSuffix(filename, suffix)
	} else {
		return failed(errors.New("Provide either 'path' (an existing file) or 'content' plus 'filename'"))
	}

	if !renderableArtifactSuffixes[suffix] {
		var renderable []string
		for s := range renderableArtifactSuffixes {
			renderable = append(renderable, s)
		}
		sort.Strings(renderable)
		return failed(fmt.Errorf("%s cannot be rendered on the canvas. Renderable: %s", suffix, strings.Join(renderable, ", ")))
	}

	title := strings.TrimSpace(str(params, "title", stem))
	if title == "" {
		title = stem
	}
	topic := strings.TrimSpace(str(params, "topic", title))
	if topic == "" {
		topic = title
	}
	desc := strings.TrimSpace(str(params, "description", ""))

	// Compute artifact destination: <root>/artifacts/canvas/<date>/<stem>.<ext>
	dateDir := time.Now().UTC().Format("2006-01-02")
	canvasDir := filepath.Join(root, "artifacts", "canvas", dateDir)
	_ = os.MkdirAll(canvasDir, 0o755)

	targetBase := stem
	if targetBase == "" {
		targetBase = "artifact"
	}
	targetPath := filepath.Join(canvasDir, targetBase+suffix)
	attempt := 2
	for attempt < 100 {
		if _, err := os.Stat(targetPath); os.IsNotExist(err) {
			break
		}
		targetPath = filepath.Join(canvasDir, fmt.Sprintf("%s-%d%s", targetBase, attempt, suffix))
		attempt++
	}

	if err := atomicWrite(targetPath, payload); err != nil {
		return failed(fmt.Errorf("failed to write artifact file: %w", err))
	}

	// Write metadata JSON
	nowStr := time.Now().UTC().Format(time.RFC3339)
	metaPath := targetPath + ".metadata.json"
	meta := map[string]any{
		"kind":        strings.TrimPrefix(suffix, "."),
		"status":      "complete",
		"title":       title,
		"topic":       topic,
		"description": desc,
		"created_at":  nowStr,
		"artifact": map[string]any{
			"path":          targetPath,
			"metadata_path": metaPath,
			"category":      "canvas",
			"topic":         topic,
		},
	}
	metaBytes, _ := json.MarshalIndent(meta, "", "  ")
	_ = atomicWrite(metaPath, metaBytes)

	// Notify active canvas server if running
	cs := getActiveCanvas()
	live := cs != nil && cs.URL() != ""
	if live {
		cs.recordVersion(topic, targetPath)
	} else if disc, _ := readCanvasDiscovery(); disc != nil && disc.URL != "" {
		live = true
		go func(url string) {
			payload, _ := json.Marshal(map[string]any{
				"type":   "version",
				"action": "version",
				"payload": map[string]any{
					"topic": topic,
					"path":  targetPath,
				},
			})
			req, err := http.NewRequestWithContext(context.Background(), http.MethodPost, strings.TrimRight(url, "/")+"/api/bridge", bytes.NewReader(payload))
			if err == nil {
				req.Header.Set("Content-Type", "application/json")
				client := &http.Client{Timeout: 3 * time.Second}
				resp, err := client.Do(req)
				if err == nil {
					_ = resp.Body.Close()
				}
			}
		}(disc.URL)
	}

	hint := "Recorded. Tell the user to run /canvas to open the live preview panel."
	if live {
		hint = "Published — it is already showing on the open canvas."
	}

	return toolResult{
		Success: true,
		Data: map[string]any{
			"path":     targetPath,
			"title":    title,
			"topic":    topic,
			"metadata": metaPath,
			"live":     live,
			"hint":     hint,
		},
	}
}

// /canvas command handler
func (s *server) canvasCommand(ctx context.Context, args []string) (string, error) {
	sub := ""
	if len(args) > 0 {
		sub = strings.ToLower(args[0])
	}

	switch sub {
	case "stop":
		cs := getActiveCanvas()
		if cs != nil {
			if err := stopCanvasServer(); err != nil {
				return "", err
			}
			return "Stopped canvas preview server.", nil
		}
		if disc, _ := readCanvasDiscovery(); disc != nil && disc.PID > 0 {
			if disc.PID == os.Getpid() {
				_ = stopCanvasServer()
			} else if proc, err := os.FindProcess(disc.PID); err == nil {
				_ = proc.Signal(syscall.SIGTERM)
				discPath, _ := canvasDiscoveryPath()
				if discPath != "" {
					_ = os.Remove(discPath)
				}
			}
			return "Stopped canvas preview server.", nil
		}
		return "Canvas preview server is not running.", nil
	case "status":
		cs := getActiveCanvas()
		if cs != nil && cs.URL() != "" {
			return fmt.Sprintf("Canvas preview server is running at %s", cs.URL()), nil
		}
		if disc, _ := readCanvasDiscovery(); disc != nil && disc.URL != "" {
			return fmt.Sprintf("Canvas preview server is running at %s", disc.URL), nil
		}
		return "Canvas preview server is not running. Run /canvas to start.", nil
	case "bridge":
		cs := getActiveCanvas()
		if cs == nil {
			return "Canvas preview server is not running.", nil
		}
		cs.mu.RLock()
		logs := append([]canvasBridgeAction{}, cs.bridgeLogs...)
		cs.mu.RUnlock()
		if len(logs) == 0 {
			return "No bridge actions received from Canvas widgets yet.", nil
		}
		data, _ := json.MarshalIndent(logs, "", "  ")
		return string(data), nil
	default:
		cs := getActiveCanvas()
		if cs != nil && cs.URL() != "" {
			return fmt.Sprintf("Canvas preview server is already running: %s\nRun /canvas stop to shut it down.", cs.URL()), nil
		}
		if disc, _ := readCanvasDiscovery(); disc != nil && disc.URL != "" {
			return fmt.Sprintf("Canvas preview server is already running: %s\nRun /canvas stop to shut it down.", disc.URL), nil
		}
		server, err := startCanvasServer()
		if err != nil {
			return "", fmt.Errorf("failed to start canvas preview server: %w", err)
		}
		return fmt.Sprintf("Canvas preview server started: %s\nGenerated reports/dashboards will render here live without page refreshes.\nUse /canvas stop to shut down.", server.URL()), nil
	}
}

// /artifacts command handler
func (s *server) artifactsCommand(ctx context.Context, args []string) (string, error) {
	limit := 20
	if len(args) > 0 {
		var custom int
		if _, err := fmt.Sscanf(args[0], "%d", &custom); err == nil && custom > 0 {
			limit = custom
		}
	}
	items := listArtifacts(ctx, limit)
	if len(items) == 0 {
		return "No artifacts found.", nil
	}
	var b strings.Builder
	b.WriteString(fmt.Sprintf("Recent Artifacts (%d)\n\n", len(items)))
	for _, it := range items {
		topic := it.Topic
		if topic == "" {
			topic = "-"
		}
		b.WriteString(fmt.Sprintf("• [%s] %s (%s, %d bytes)\n  %s\n", it.Kind, topic, it.Status, it.SizeBytes, it.Path))
	}
	return strings.TrimSpace(b.String()), nil
}

const canvasShellHTML = `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Aria Canvas Live Preview</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0d1117; color: #c9d1d9; height: 100vh; display: flex; flex-direction: column; }
  header { display: flex; align-items: center; gap: 12px; padding: 10px 16px;
           border-bottom: 1px solid #21262d; background: #161b22; }
  header .title { font-weight: 600; font-size: 14px; color: #58a6ff; }
  header .empty { color: #8b949e; font-size: 13px; }
  .stepper { display: flex; align-items: center; gap: 6px; margin-left: auto; }
  .stepper button { background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
                     border-radius: 6px; padding: 4px 10px; cursor: pointer; font-size: 13px; }
  .stepper button:disabled { opacity: 0.4; cursor: default; }
  .stepper span { font-size: 12px; color: #8b949e; min-width: 48px; text-align: center; }
  main { flex: 1; position: relative; }
  iframe, img { width: 100%; height: 100%; border: none; display: block; object-fit: contain; background: #fff; }
  .placeholder { display: flex; align-items: center; justify-content: center;
                 height: 100%; color: #8b949e; font-size: 14px; text-align: center; padding: 20px; }
</style>
</head>
<body>
<header>
  <span class="title">Aria Canvas</span>
  <span class="empty" id="status">Waiting for the first artifact&hellip;</span>
  <div class="stepper" id="stepper" style="display:none">
    <button id="prev">&lsaquo;</button>
    <span id="pos"></span>
    <button id="next">&rsaquo;</button>
  </div>
</header>
<main id="main">
  <div class="placeholder">Run a command or let Aria publish an artifact — it will render here live with instant version stepping.</div>
</main>
<script>
let state = { threads: {}, active_thread_id: null };
let activeIndex = 0;

function render() {
  const main = document.getElementById('main');
  const status = document.getElementById('status');
  const stepper = document.getElementById('stepper');
  const thread = state.active_thread_id ? state.threads[state.active_thread_id] : null;
  if (!thread || thread.versions.length === 0) {
    main.innerHTML = '<div class="placeholder">Run a command or let Aria publish an artifact — it will render here live with instant version stepping.</div>';
    status.textContent = 'Waiting for the first artifact\u2026';
    stepper.style.display = 'none';
    return;
  }
  status.textContent = state.active_thread_id;
  stepper.style.display = 'flex';
  activeIndex = Math.max(0, Math.min(activeIndex, thread.versions.length - 1));
  document.getElementById('pos').textContent = (activeIndex + 1) + ' / ' + thread.versions.length;
  document.getElementById('prev').disabled = activeIndex <= 0;
  document.getElementById('next').disabled = activeIndex >= thread.versions.length - 1;
  const version = thread.versions[activeIndex];
  const url = '/artifact/' + encodeURIComponent(state.active_thread_id) + '?v=' + version.index + '&_=' + Date.now();
  if (version.kind === 'image') {
    main.innerHTML = '<img src="' + url + '">';
  } else if (version.kind === '3d') {
    main.innerHTML = '<div style="width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#0d1117;color:#58a6ff;">' +
      '<div style="font-size:20px;font-weight:600;margin-bottom:8px;">📦 3D Asset: ' + version.path.split(/[\\/]/).pop() + '</div>' +
      '<div style="font-size:13px;color:#8b949e;margin-bottom:20px;">Direct WebGL 3D Preview Pipeline Active</div>' +
      '<a href="' + url + '" target="_blank" style="color:#58a6ff;border:1px solid #30363d;padding:8px 16px;border-radius:6px;text-decoration:none;font-weight:500;">Download 3D Asset</a>' +
      '</div>';
  } else {
    main.innerHTML = '<iframe id="canvas-frame" src="' + url + '" sandbox="allow-scripts"></iframe>';
  }
}

async function refreshState(jumpToLatest) {
  try {
    const resp = await fetch('/state');
    state = await resp.json();
    if (jumpToLatest && state.active_thread_id) {
      const thread = state.threads[state.active_thread_id];
      activeIndex = thread ? thread.versions.length - 1 : 0;
    }
    render();
  } catch(e) {}
}

document.getElementById('prev').onclick = () => { activeIndex--; render(); };
document.getElementById('next').onclick = () => { activeIndex++; render(); };

// Host bridge: listen for postMessage events from sandboxed iframe widgets
window.addEventListener('message', (event) => {
  if (event.data && typeof event.data === 'object' && event.data.type === 'aria.canvas.action') {
    fetch('/api/bridge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        type: event.data.type,
        action: event.data.action || 'click',
        payload: event.data.payload || {},
        topic: state.active_thread_id || ''
      })
    }).catch(() => {});
  }
});

const es = new EventSource('/events');
es.onmessage = (event) => {
  try {
    const msg = JSON.parse(event.data);
    if (msg.type === 'new-version') {
      refreshState(true);
    }
  } catch(e) {}
};

refreshState(true);
</script>
</body>
</html>
`
