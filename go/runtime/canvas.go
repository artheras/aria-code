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
<title>Aria Canvas & Computer Use Studio</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
         background: #111113; color: #e4e4e7; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
  header { display: flex; align-items: center; gap: 12px; padding: 8px 16px;
           border-bottom: 1px solid #27272a; background: #141416; z-index: 20; }
  header .title { font-weight: 600; font-size: 13px; color: #f4f4f5; display: flex; align-items: center; gap: 8px; letter-spacing: -0.01em; }
  header .empty { color: #71717a; font-size: 12px; }
  .icon-sm { width: 15px; height: 15px; vertical-align: -2px; flex-shrink: 0; }
  .nav-tabs { display: flex; gap: 2px; background: #18181b; padding: 3px; border-radius: 7px; border: 1px solid #27272a; }
  .nav-tabs button { background: transparent; color: #a1a1aa; border: none; border-radius: 5px; padding: 4px 10px;
                     font-size: 12px; font-weight: 500; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; transition: all 0.12s; }
  .nav-tabs button.active { background: #27272a; color: #f4f4f5; }
  .stepper { display: flex; align-items: center; gap: 6px; margin-left: auto; }
  .stepper button, .hud-btn { background: #18181b; color: #e4e4e7; border: 1px solid #27272a;
                     border-radius: 6px; padding: 4px 10px; cursor: pointer; font-size: 12px; display: inline-flex; align-items: center; gap: 6px; transition: background 0.12s; }
  .stepper button:hover, .hud-btn:hover { background: #27272a; }
  .stepper button:disabled { opacity: 0.4; cursor: default; }
  .stepper span { font-size: 12px; color: #a1a1aa; min-width: 48px; text-align: center; }
  main { flex: 1; position: relative; overflow: hidden; }
  iframe, img { width: 100%; height: 100%; border: none; display: block; object-fit: contain; background: #fff; }
  .placeholder { display: flex; align-items: center; justify-content: center;
                 height: 100%; color: #71717a; font-size: 13px; text-align: center; padding: 20px; }

  /* Computer Use Studio Viewport (Linear / Geist Neutral Zinc) */
  #cu-studio { display: none; width: 100%; height: 100%; flex-direction: column; background: #111113; }
  #cu-studio.pip-mode { position: fixed; right: 18px; bottom: 18px; width: 380px; height: 260px;
                        border-radius: 10px; border: 1px solid #3f3f46; box-shadow: 0 16px 40px rgba(0,0,0,0.6); z-index: 999; overflow: hidden; }
  .cu-toolbar { display: flex; align-items: center; gap: 8px; padding: 8px 14px; background: #141416; border-bottom: 1px solid #27272a; font-size: 12px; }
  .cu-url-pill { flex: 1; background: #18181b; border: 1px solid #27272a; border-radius: 5px; padding: 4px 10px;
                 color: #a1a1aa; font-family: ui-monospace, SFMono-Regular, monospace; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mode-pill { padding: 3px 9px; border-radius: 5px; font-size: 11px; font-weight: 500; display: inline-flex; align-items: center; gap: 6px; border: 1px solid #27272a; background: #18181b; color: #e4e4e7; }
  .status-dot { width: 7px; height: 7px; border-radius: 50%; background: #10b981; }
  .mode-takeover .status-dot { background: #f59e0b; }
  .mode-paused .status-dot { background: #ef4444; }
  .cu-stage { flex: 1; position: relative; margin: 12px; border-radius: 8px; overflow: hidden;
              border: 1px solid #27272a; background: #161618; }
  .cu-stage.takeover { border-color: #d97706; cursor: crosshair; }
  #cu-virtual-cursor { position: absolute; left: 0; top: 0; pointer-events: none; z-index: 30;
                       transition: transform 0.22s cubic-bezier(0.22, 1, 0.36, 1); }
  .thought-bubble { position: absolute; left: 16px; top: -6px; background: #18181b;
                    color: #f4f4f5; border: 1px solid #3f3f46; border-radius: 6px; padding: 4px 9px; font-size: 11px;
                    white-space: nowrap; box-shadow: 0 4px 12px rgba(0,0,0,0.35); }
  #cu-diff-box { position: absolute; border: 1px dashed #71717a; background: rgba(255, 255, 255, 0.03);
                 border-radius: 4px; pointer-events: none; transition: all 0.18s ease; display: none; z-index: 20; }
  .cu-timeline { display: flex; align-items: center; gap: 10px; padding: 8px 14px; background: #141416; border-top: 1px solid #27272a; font-size: 11px; }
  .trail-dots { display: flex; align-items: center; gap: 6px; flex: 1; overflow-x: auto; padding: 2px 0; }
  .trail-dot { width: 8px; height: 8px; border-radius: 50%; background: #a1a1aa;
               cursor: pointer; transition: transform 0.12s; flex-shrink: 0; }
  .trail-dot:hover { transform: scale(1.35); background: #f4f4f5; }

  /* Icon Specimen Grid */
  #icon-specimen { display: none; flex: 1; padding: 24px 32px; overflow-y: auto; background: #111113; }
  .spec-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; margin-top: 12px; margin-bottom: 24px; }
  .spec-card { background: #161618; border: 1px solid #27272a; border-radius: 8px; padding: 16px; display: flex; flex-direction: column; gap: 10px; }
  .spec-icon-box { width: 36px; height: 36px; border-radius: 7px; background: #1f1f23; display: flex; align-items: center; justify-content: center; color: #f4f4f5; }
  .spec-name { font-size: 13px; font-weight: 500; color: #f4f4f5; }
  .spec-sub { font-size: 11px; font-family: ui-monospace, SFMono-Regular, monospace; color: #71717a; }
</style>
</head>
<body>
<svg style="display:none" xmlns="http://www.w3.org/2000/svg">
  <symbol id="icon-mark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
    <polygon points="12 3 20 19 12 15.5 4 19 12 3"/>
  </symbol>
  <symbol id="icon-layers" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
    <polygon points="12 3 2 8 12 13 22 8 12 3"/><polyline points="2 13 12 18 22 13"/><polyline points="2 17.5 12 22.5 22 17.5"/>
  </symbol>
  <symbol id="icon-cu" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
    <rect x="3" y="4" width="18" height="14" rx="2.5"/><line x1="3" y1="8" x2="21" y2="8"/>
    <path d="M11.5 11.5L17.5 14L14.5 15.2L13.3 18.2L11.5 11.5Z" fill="currentColor"/>
  </symbol>
  <symbol id="icon-grid" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
    <rect x="4" y="4" width="6.8" height="6.8" rx="1.5"/><rect x="13.2" y="4" width="6.8" height="6.8" rx="1.5"/>
    <rect x="4" y="13.2" width="6.8" height="6.8" rx="1.5"/><rect x="13.2" y="13.2" width="6.8" height="6.8" rx="1.5"/>
  </symbol>
  <symbol id="icon-handoff" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
    <path d="M17 8L21 12L17 16"/><path d="M3 12H21"/><path d="M7 16L3 12L7 8"/>
  </symbol>
  <symbol id="icon-play" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
    <polygon points="6 4 20 12 6 20 6 4"/>
  </symbol>
  <symbol id="icon-pip" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
    <rect x="3" y="4" width="18" height="16" rx="2"/><rect x="12" y="12" width="7" height="6" rx="1"/>
  </symbol>
  <symbol id="icon-python" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 3C8.5 3 8.5 4.8 8.5 4.8V7H12.5V8H6.5C6.5 8 3.5 8 3.5 12C3.5 16 6.2 15.8 6.2 15.8H8V13.5C8 13.5 7.9 11 10.5 11H15C15 11 17 11.1 17 8.8V5.2C17 5.2 17.2 3 12 3Z"/>
    <path d="M12 21C15.5 21 15.5 19.2 15.5 19.2V17H11.5V16H17.5C17.5 16 20.5 16 20.5 12C20.5 8 17.8 8.2 17.8 8.2H16V10.5C16 10.5 16.1 13 13.5 13H9C9 13 7 12.9 7 15.2V18.8C7 18.8 6.8 21 12 21Z"/>
  </symbol>
  <symbol id="icon-go" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
    <rect x="3" y="5" width="18" height="14" rx="3.5"/>
    <path d="M10.5 10.2C9.9 9.6 9.1 9.4 8.3 9.6C7.1 9.9 6.4 11 6.5 12.3C6.6 13.6 7.6 14.5 8.9 14.5C10.1 14.5 10.9 13.7 11 12.5H8.8"/>
    <circle cx="15.2" cy="12" r="2.5"/>
  </symbol>
  <symbol id="icon-rust" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="7.5"/>
    <path d="M12 2.5V4.5M12 19.5V21.5M2.5 12H4.5M19.5 12H21.5M5.3 5.3L6.7 6.7M17.3 17.3L18.7 18.7M18.7 5.3L17.3 6.7M6.7 17.3L5.3 18.7"/>
    <path d="M9.8 14.8V9.2H12.6C13.6 9.2 14.3 9.8 14.3 10.7C14.3 11.6 13.6 12.2 12.6 12.2H9.8M12.3 12.2L14.4 14.8"/>
  </symbol>
  <symbol id="icon-ts" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
    <rect x="3.5" y="3.5" width="17" height="17" rx="3.5"/>
    <path d="M7.5 10.5H12.2M9.85 10.5V16.5"/>
    <path d="M16.8 11.3C16.3 10.6 14.9 10.4 14.1 11.1C13.3 11.8 14 12.8 15.3 13.2C16.6 13.6 17.2 14.5 16.7 15.6C16.2 16.5 14.5 16.6 13.6 15.7"/>
  </symbol>
  <symbol id="icon-finance" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
    <path d="M4 19.5H20"/><rect x="5.5" y="12" width="3" height="5" rx="0.6"/>
    <rect x="10.5" y="8.5" width="3" height="8.5" rx="0.6"/><rect x="15.5" y="5.5" width="3" height="11.5" rx="0.6"/>
  </symbol>
  <symbol id="icon-health" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
    <rect x="3.5" y="4.5" width="17" height="15" rx="3"/>
    <path d="M6.5 12H9.5L11.2 8.5L13.2 15.5L14.8 12H17.5"/>
  </symbol>
  <symbol id="icon-cube" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round">
    <path d="M12 3.5L19.5 7.8V16.2L12 20.5L4.5 16.2V7.8L12 3.5Z"/>
    <path d="M4.5 7.8L12 12.1L19.5 7.8M12 12.1V20.5"/>
  </symbol>
  <symbol id="icon-shield" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 3.5L19 6.5V11.8C19 16.3 15.9 19.4 12 20.8C8.1 19.4 5 16.3 5 11.8V6.5L12 3.5Z"/>
    <path d="M9.2 12.2L11.1 14.1L15 10.2"/>
  </symbol>
</svg>
<header>
  <span class="title"><svg class="icon-sm"><use href="#icon-mark"/></svg>Aria Studio</span>
  <div class="nav-tabs">
    <button id="tab-artifacts" class="active"><svg class="icon-sm"><use href="#icon-layers"/></svg>Artifacts</button>
    <button id="tab-cu"><svg class="icon-sm"><use href="#icon-cu"/></svg>Computer Use</button>
    <button id="tab-icons"><svg class="icon-sm"><use href="#icon-grid"/></svg>System Icons</button>
  </div>
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
<section id="cu-studio">
  <div class="cu-toolbar">
    <span id="cu-mode-badge" class="mode-pill"><span class="status-dot"></span><span id="cu-mode-text">Autonomous</span></span>
    <span id="cu-url" class="cu-url-pill">https://aria.local/computer-use-sandbox</span>
    <span id="cu-diff-badge" style="color:#a1a1aa;font-size:11px;font-family:ui-monospace,monospace;">Diff 0.0%</span>
    <button class="hud-btn" id="btn-takeover" title="Pause Aria and take over mouse/keyboard"><svg class="icon-sm"><use href="#icon-handoff"/></svg><span id="btn-takeover-label">Takeover</span></button>
    <button class="hud-btn" id="btn-demo" title="Simulate agent trajectory"><svg class="icon-sm"><use href="#icon-play"/></svg>Demo</button>
    <button class="hud-btn" id="btn-pip" title="Toggle Picture-in-Picture mini window"><svg class="icon-sm"><use href="#icon-pip"/></svg>PiP</button>
  </div>
  <div class="cu-stage" id="cu-stage">
    <svg id="cu-trail-svg" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:15;"></svg>
    <div id="cu-diff-box"></div>
    <div id="cu-virtual-cursor" style="transform: translate(180px, 140px);">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="#f4f4f5" stroke="#111113" stroke-width="1.8">
        <path d="M3 3l7.07 16.97 2.51-7.39 7.39-2.51L3 3z"/>
      </svg>
      <div class="thought-bubble" id="cu-thought">Ready — observing viewport</div>
    </div>
    <div id="cu-mock-surface" style="padding:28px;color:#a1a1aa;font-size:13px;user-select:none;">
      <div style="display:flex;gap:12px;flex-wrap:wrap;">
        <div style="background:#18181b;border:1px solid #27272a;padding:10px 16px;border-radius:6px;color:#f4f4f5;">
          <span style="background:#27272a;color:#a1a1aa;font-family:ui-monospace,monospace;font-size:10px;padding:2px 5px;border-radius:4px;margin-right:8px;">1</span>Filter Invoices
        </div>
        <div style="background:#18181b;border:1px solid #27272a;padding:10px 16px;border-radius:6px;color:#f4f4f5;">
          <span style="background:#27272a;color:#a1a1aa;font-family:ui-monospace,monospace;font-size:10px;padding:2px 5px;border-radius:4px;margin-right:8px;">2</span>Verify Total ($4,280.00)
        </div>
        <div style="background:#27272a;border:1px solid #3f3f46;padding:10px 16px;border-radius:6px;color:#f4f4f5;">
          <span style="background:#18181b;color:#e4e4e7;font-family:ui-monospace,monospace;font-size:10px;padding:2px 5px;border-radius:4px;margin-right:8px;">3</span>Export PDF Report
        </div>
      </div>
    </div>
  </div>
  <div class="cu-timeline">
    <span style="color:#a1a1aa;font-weight:500;">Timeline</span>
    <div class="trail-dots" id="cu-trail-dots"></div>
    <span id="cu-frames-count" style="color:#71717a;font-family:ui-monospace,monospace;">0 frames</span>
  </div>
</section>
<section id="icon-specimen">
  <div style="font-size:11px;font-family:ui-monospace,monospace;color:#71717a;letter-spacing:0.06em;">RUNTIMES &amp; LANGUAGES (24PX GRID · 1.5PX STROKE)</div>
  <div class="spec-grid">
    <div class="spec-card"><div class="spec-icon-box"><svg width="20" height="20"><use href="#icon-python"/></svg></div><div class="spec-name">Python</div><div class="spec-sub">src/aria_code</div></div>
    <div class="spec-card"><div class="spec-icon-box"><svg width="20" height="20"><use href="#icon-go"/></svg></div><div class="spec-name">Go</div><div class="spec-sub">go/runtime</div></div>
    <div class="spec-card"><div class="spec-icon-box"><svg width="20" height="20"><use href="#icon-rust"/></svg></div><div class="spec-name">Rust</div><div class="spec-sub">sandbox / kernel</div></div>
    <div class="spec-card"><div class="spec-icon-box"><svg width="20" height="20"><use href="#icon-ts"/></svg></div><div class="spec-name">TypeScript</div><div class="spec-sub">desktop / canvas</div></div>
  </div>
  <div style="font-size:11px;font-family:ui-monospace,monospace;color:#71717a;letter-spacing:0.06em;">CORE SERVICES</div>
  <div class="spec-grid">
    <div class="spec-card"><div class="spec-icon-box"><svg width="20" height="20"><use href="#icon-cu"/></svg></div><div class="spec-name">Computer Use</div><div class="spec-sub">Browser &amp; Desktop</div></div>
    <div class="spec-card"><div class="spec-icon-box"><svg width="20" height="20"><use href="#icon-finance"/></svg></div><div class="spec-name">Finance &amp; Quant</div><div class="spec-sub">Backtest &amp; SEC</div></div>
    <div class="spec-card"><div class="spec-icon-box"><svg width="20" height="20"><use href="#icon-health"/></svg></div><div class="spec-name">Healthcare</div><div class="spec-sub">PubMed &amp; Trials</div></div>
    <div class="spec-card"><div class="spec-icon-box"><svg width="20" height="20"><use href="#icon-cube"/></svg></div><div class="spec-name">Spatial 3D</div><div class="spec-sub">Realty &amp; Scene</div></div>
    <div class="spec-card"><div class="spec-icon-box"><svg width="20" height="20"><use href="#icon-shield"/></svg></div><div class="spec-name">Access &amp; Audit</div><div class="spec-sub">Approval &amp; Policy</div></div>
    <div class="spec-card"><div class="spec-icon-box"><svg width="20" height="20"><use href="#icon-grid"/></svg></div><div class="spec-name">Skills &amp; Plugins</div><div class="spec-sub">Extension Catalog</div></div>
  </div>
</section>
<script>
let state = { threads: {}, active_thread_id: null };
let activeIndex = 0;
let cuState = {
  mode: 'autonomous',
  cursor_x: 180,
  cursor_y: 140,
  thought: 'Ready — observing viewport',
  url: 'https://aria.local/computer-use-sandbox',
  visual_diff_pct: 0,
  changed_region: null,
  recorded_frames: 0,
  action_trail: []
};
let cursorHistory = [{x: 180, y: 140}];

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
    main.innerHTML = '<div style="width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#111113;color:#e4e4e7;">' +
      '<svg width="32" height="32" style="margin-bottom:12px;"><use href="#icon-cube"/></svg>' +
      '<div style="font-size:16px;font-weight:600;margin-bottom:6px;">3D Asset: ' + version.path.split(/[\\/]/).pop() + '</div>' +
      '<div style="font-size:12px;color:#71717a;margin-bottom:18px;">Direct WebGL 3D Preview Pipeline Active</div>' +
      '<a href="' + url + '" target="_blank" style="color:#f4f4f5;border:1px solid #27272a;background:#18181b;padding:7px 14px;border-radius:6px;text-decoration:none;font-size:12px;font-weight:500;">Download 3D Asset</a>' +
      '</div>';
  } else {
    main.innerHTML = '<iframe id="canvas-frame" src="' + url + '" sandbox="allow-scripts"></iframe>';
  }
}

function renderComputerUse() {
  const cursor = document.getElementById('cu-virtual-cursor');
  const thoughtEl = document.getElementById('cu-thought');
  const badge = document.getElementById('cu-mode-badge');
  const modeText = document.getElementById('cu-mode-text');
  const stage = document.getElementById('cu-stage');
  const takeoverLabel = document.getElementById('btn-takeover-label');
  const diffBox = document.getElementById('cu-diff-box');

  const cx = cuState.cursor_x || 180;
  const cy = cuState.cursor_y || 140;
  cursor.style.transform = 'translate(' + cx + 'px, ' + cy + 'px)';
  thoughtEl.textContent = cuState.thought || 'Observing';
  document.getElementById('cu-url').textContent = cuState.url || 'https://aria.local/computer-use';
  document.getElementById('cu-diff-badge').textContent = 'Diff ' + Number(cuState.visual_diff_pct || 0).toFixed(1) + '%';
  document.getElementById('cu-frames-count').textContent = (cuState.recorded_frames || cursorHistory.length) + ' frames';

  if (cuState.mode === 'takeover') {
    badge.className = 'mode-pill mode-takeover';
    modeText.textContent = 'Takeover';
    stage.classList.add('takeover');
    takeoverLabel.textContent = 'Return Control';
  } else if (cuState.mode === 'paused') {
    badge.className = 'mode-pill mode-paused';
    modeText.textContent = 'Paused';
    stage.classList.remove('takeover');
    takeoverLabel.textContent = 'Takeover';
  } else {
    badge.className = 'mode-pill';
    modeText.textContent = 'Autonomous';
    stage.classList.remove('takeover');
    takeoverLabel.textContent = 'Takeover';
  }

  if (cuState.changed_region && cuState.changed_region.w > 0) {
    diffBox.style.display = 'block';
    diffBox.style.left = cuState.changed_region.x + 'px';
    diffBox.style.top = cuState.changed_region.y + 'px';
    diffBox.style.width = cuState.changed_region.w + 'px';
    diffBox.style.height = cuState.changed_region.h + 'px';
  } else {
    diffBox.style.display = 'none';
  }

  const last = cursorHistory[cursorHistory.length - 1];
  if (!last || last.x !== cx || last.y !== cy) {
    cursorHistory.push({x: cx, y: cy});
    if (cursorHistory.length > 6) cursorHistory.shift();
  }
  const svg = document.getElementById('cu-trail-svg');
  if (cursorHistory.length > 1) {
    const pts = cursorHistory.map(p => p.x + ',' + p.y).join(' ');
    svg.innerHTML = '<polyline points="' + pts + '" fill="none" stroke="rgba(161,161,170,0.35)" stroke-width="1.5" stroke-dasharray="3 3"/>';
  }

  const dots = document.getElementById('cu-trail-dots');
  const trail = cuState.action_trail || [];
  dots.innerHTML = trail.map((item, idx) =>
    '<div class="trail-dot" title="Step ' + (idx + 1) + ': ' + (item.thought || item.action || '') + '"></div>'
  ).join('');
}

async function syncComputerUseState(patch) {
  Object.assign(cuState, patch);
  renderComputerUse();
  try {
    const resp = await fetch('/api/v2/computer-use/state', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch)
    });
    if (resp.ok) {
      cuState = await resp.json();
      renderComputerUse();
    }
  } catch (e) {}
}

function switchTab(activeId) {
  ['tab-artifacts', 'tab-cu', 'tab-icons'].forEach(id => {
    document.getElementById(id).classList.toggle('active', id === activeId);
  });
  document.getElementById('main').style.display = activeId === 'tab-artifacts' ? 'block' : 'none';
  document.getElementById('cu-studio').style.display = activeId === 'tab-cu' ? 'flex' : 'none';
  document.getElementById('icon-specimen').style.display = activeId === 'tab-icons' ? 'block' : 'none';
  if (activeId === 'tab-cu') renderComputerUse();
}

document.getElementById('tab-artifacts').onclick = () => switchTab('tab-artifacts');
document.getElementById('tab-cu').onclick = () => switchTab('tab-cu');
document.getElementById('tab-icons').onclick = () => switchTab('tab-icons');
document.getElementById('btn-takeover').onclick = () => {
  const nextMode = cuState.mode === 'takeover' ? 'autonomous' : 'takeover';
  const nextThought = nextMode === 'takeover'
    ? 'Human takeover active — waiting for user input'
    : 'Resumed autonomous control';
  syncComputerUseState({ mode: nextMode, thought: nextThought });
};
document.getElementById('btn-pip').onclick = () => {
  document.getElementById('cu-studio').classList.toggle('pip-mode');
};
document.getElementById('cu-stage').onclick = (e) => {
  if (cuState.mode !== 'takeover') return;
  const rect = e.currentTarget.getBoundingClientRect();
  const x = Math.round(e.clientX - rect.left);
  const y = Math.round(e.clientY - rect.top);
  syncComputerUseState({
    cursor_x: x,
    cursor_y: y,
    thought: 'User clicked (' + x + ', ' + y + ') during takeover',
    action_entry: { action: 'human_click', x, y, thought: 'Human takeover click' }
  });
};
document.getElementById('btn-demo').onclick = () => {
  const steps = [
    { cursor_x: 95, cursor_y: 52, thought: 'Clicking [1] Filter Invoices', visual_diff_pct: 12.4, changed_region: {x: 28, y: 28, w: 145, h: 46} },
    { cursor_x: 265, cursor_y: 52, thought: 'Inspecting [2] Total ($4,280.00)', visual_diff_pct: 6.8, changed_region: {x: 185, y: 28, w: 180, h: 46} },
    { cursor_x: 455, cursor_y: 52, thought: 'Clicking [3] Export PDF Report', visual_diff_pct: 24.1, changed_region: {x: 380, y: 28, w: 165, h: 46} }
  ];
  steps.forEach((step, idx) => {
    setTimeout(() => {
      syncComputerUseState({
        ...step,
        recorded_frames: (cuState.recorded_frames || 0) + 1,
        action_entry: { action: 'click', thought: step.thought }
      });
    }, idx * 700);
  });
};

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
    } else if (msg.type === 'computer-use') {
      Object.assign(cuState, msg);
      renderComputerUse();
    }
  } catch(e) {}
};

refreshState(true);
</script>
</body>
</html>
`
