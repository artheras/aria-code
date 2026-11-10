package main

import (
	"context"
	"crypto/hmac"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const maxArchiveBytes = 25 << 20

type service struct {
	root string
	ttl  time.Duration
}

func main() {
	if len(os.Args) >= 3 && os.Args[1] == "--review-archive" {
		archivePath := os.Args[2]
		filename := filepath.Base(archivePath)
		if len(os.Args) >= 4 && strings.TrimSpace(os.Args[3]) != "" {
			filename = os.Args[3]
		}
		payload, err := os.ReadFile(archivePath)
		if err != nil {
			fmt.Fprintln(os.Stderr, err.Error())
			os.Exit(1)
		}
		report, err := reviewArchive(payload, filename)
		if err != nil {
			fmt.Fprintln(os.Stderr, err.Error())
			os.Exit(1)
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetEscapeHTML(false)
		enc.SetIndent("", "  ")
		if err := enc.Encode(report); err != nil {
			fmt.Fprintln(os.Stderr, err.Error())
			os.Exit(1)
		}
		return
	}
	root := strings.TrimSpace(os.Getenv("ARIA_CODE_WORKSPACE_ROOT"))
	if root == "" {
		root = filepath.Join(os.TempDir(), "aria-code-workspaces")
	}
	root, err := filepath.Abs(root)
	if err != nil {
		log.Fatal("invalid workspace root")
	}
	ttlHours, err := strconv.Atoi(os.Getenv("ARIA_CODE_WORKSPACE_TTL_HOURS"))
	if err != nil || ttlHours <= 0 {
		ttlHours = 24
	}
	if ttlHours > 168 {
		ttlHours = 168
	}
	if err := os.MkdirAll(root, 0700); err != nil {
		log.Fatal("cannot create workspace root")
	}
	_ = os.Chmod(root, 0700)
	s := &service{root: root, ttl: time.Duration(ttlHours) * time.Hour}
	port := strings.TrimPrefix(envOr("PORT", "8787"), ":")
	server := &http.Server{Addr: ":" + port, Handler: s.routes(), ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second}
	log.Printf("project review service listening on %s", server.Addr)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	s.cleanupExpired(time.Now())
	go func() {
		ticker := time.NewTicker(30 * time.Minute)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer cancel()
				_ = server.Shutdown(shutdown)
				return
			case <-ticker.C:
				s.cleanupExpired(time.Now())
			}
		}
	}()
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal("project review server stopped")
	}
}

func (s *service) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "service": "aria-code-project-review"})
	})
	mux.HandleFunc("POST /v1/project-reviews", s.createReview)
	mux.HandleFunc("POST /v1/workspaces", s.createWorkspace)
	mux.HandleFunc("GET /v1/workspaces/{workspaceID}", s.getWorkspace)
	mux.HandleFunc("DELETE /v1/workspaces/{workspaceID}", s.deleteWorkspace)
	mux.HandleFunc("POST /v1/workspaces/{workspaceID}/files/read", s.readFile)
	mux.HandleFunc("POST /v1/workspaces/{workspaceID}/files/list", s.listFiles)
	mux.HandleFunc("POST /v1/workspaces/{workspaceID}/search", s.searchFiles)
	mux.HandleFunc("POST /v1/workspaces/{workspaceID}/changes", s.proposeChange)
	mux.HandleFunc("GET /v1/workspaces/{workspaceID}/changes/{changeID}", s.getChange)
	mux.HandleFunc("POST /v1/workspaces/{workspaceID}/changes/{changeID}/apply", s.applyChange)
	mux.HandleFunc("POST /v1/workspaces/{workspaceID}/changes/{changeID}/rollback", s.rollbackChange)
	return limitBody(mux)
}

func envOr(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func limitBody(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.Body = http.MaxBytesReader(w, r.Body, maxArchiveBytes+(1<<20))
		next.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func fail(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"detail": message})
}

func (s *service) authorize(r *http.Request) error {
	expected := strings.TrimSpace(os.Getenv("ARIA_CODE_REVIEW_TOKEN"))
	if expected == "" {
		return errAuthNotConfigured
	}
	supplied := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
	if supplied == "" || !hmac.Equal([]byte(supplied), []byte(expected)) {
		return errInvalidToken
	}
	return nil
}

var (
	errAuthNotConfigured = errors.New("Project review service authentication is not configured.")
	errInvalidToken      = errors.New("Invalid project review service token.")
)

func (s *service) auth(w http.ResponseWriter, r *http.Request) bool {
	if err := s.authorize(r); err != nil {
		status := http.StatusUnauthorized
		if errors.Is(err, errAuthNotConfigured) {
			status = http.StatusServiceUnavailable
		}
		fail(w, status, err.Error())
		return false
	}
	return true
}

func (s *service) multipartProject(w http.ResponseWriter, r *http.Request) ([]byte, string, error) {
	if err := r.ParseMultipartForm(1 << 20); err != nil {
		return nil, "", fmt.Errorf("invalid multipart request")
	}
	if r.MultipartForm != nil {
		defer r.MultipartForm.RemoveAll()
	}
	file, header, err := r.FormFile("project")
	if err != nil {
		return nil, "", fmt.Errorf("project file is required")
	}
	defer file.Close()
	payload, err := io.ReadAll(io.LimitReader(file, maxArchiveBytes+1))
	if err != nil {
		return nil, "", fmt.Errorf("could not read project archive")
	}
	if len(payload) > maxArchiveBytes {
		return nil, "", fmt.Errorf("Project archive exceeds the upload limit.")
	}
	name := filepath.Base(strings.ReplaceAll(header.Filename, "\\", "/"))
	if name == "." || name == string(filepath.Separator) || name == "" {
		name = "project.zip"
	}
	return payload, name, nil
}

func decodeJSON[T any](w http.ResponseWriter, r *http.Request, out *T) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 2<<20)
	decoder := json.NewDecoder(r.Body)
	if err := decoder.Decode(out); err != nil {
		fail(w, http.StatusUnprocessableEntity, "Invalid request body.")
		return false
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		fail(w, http.StatusUnprocessableEntity, "Invalid request body.")
		return false
	}
	return true
}

func (s *service) createReview(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	payload, name, err := s.multipartProject(w, r)
	if err != nil {
		status := http.StatusBadRequest
		if strings.Contains(err.Error(), "upload limit") {
			status = http.StatusRequestEntityTooLarge
		}
		fail(w, status, err.Error())
		return
	}
	report, err := reviewArchive(payload, name)
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, report)
}

type ownerRequest struct {
	OwnerID string `json:"owner_id"`
}

func (s *service) createWorkspace(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	if err := r.ParseMultipartForm(1 << 20); err != nil {
		fail(w, http.StatusUnprocessableEntity, "Invalid multipart request.")
		return
	}
	if r.MultipartForm != nil {
		defer r.MultipartForm.RemoveAll()
	}
	owner, run := r.FormValue("owner_id"), r.FormValue("run_id")
	if len(owner) == 0 || len(owner) > 512 || len(run) == 0 || len(run) > 512 {
		fail(w, http.StatusUnprocessableEntity, "owner_id and run_id are required.")
		return
	}
	payload, name, err := s.multipartProject(w, r)
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	manifest, err := s.createWorkspaceFromArchive(payload, name, owner, run)
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, manifest.public())
}

func requestOwner(r *http.Request) string { return r.URL.Query().Get("owner_id") }
func (s *service) getWorkspace(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	if requestOwner(r) == "" {
		fail(w, http.StatusUnprocessableEntity, "owner_id is required.")
		return
	}
	manifest, err := s.workspace(r.PathValue("workspaceID"), requestOwner(r))
	if err != nil {
		fail(w, http.StatusNotFound, "Workspace not found.")
		return
	}
	writeJSON(w, http.StatusOK, manifest.public())
}

func (s *service) deleteWorkspace(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	if requestOwner(r) == "" {
		fail(w, http.StatusUnprocessableEntity, "owner_id is required.")
		return
	}
	if _, err := s.workspace(r.PathValue("workspaceID"), requestOwner(r)); err != nil {
		fail(w, http.StatusNotFound, "Workspace not found.")
		return
	}
	dir, _ := s.workspaceDir(r.PathValue("workspaceID"))
	if err := os.RemoveAll(dir); err != nil {
		fail(w, http.StatusInternalServerError, "Could not delete workspace.")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *service) readFile(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	var req struct {
		OwnerID string `json:"owner_id"`
		Path    string `json:"path"`
		Offset  int    `json:"offset"`
		Limit   *int   `json:"limit"`
	}
	if !decodeJSON(w, r, &req) {
		return
	}
	limit := 400
	if req.Limit != nil {
		limit = *req.Limit
	}
	if req.Path == "" || len(req.Path) > 1000 || req.Offset < 0 || limit < 1 || limit > 2000 {
		fail(w, 422, "Invalid request body.")
		return
	}
	result, err := s.workspaceRead(r.PathValue("workspaceID"), req.OwnerID, req.Path, req.Offset, limit)
	if err != nil {
		fail(w, 400, err.Error())
		return
	}
	writeJSON(w, 200, result)
}

func (s *service) listFiles(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	var req struct {
		OwnerID string `json:"owner_id"`
		Path    string `json:"path"`
		Pattern string `json:"pattern"`
	}
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.Path == "" {
		req.Path = "."
	}
	if req.Pattern == "" {
		req.Pattern = "*"
	}
	if len(req.Path) > 1000 || len(req.Pattern) > 200 {
		fail(w, 422, "Invalid request body.")
		return
	}
	result, err := s.workspaceList(r.PathValue("workspaceID"), req.OwnerID, req.Path, req.Pattern)
	if err != nil {
		fail(w, 400, err.Error())
		return
	}
	writeJSON(w, 200, result)
}

func (s *service) searchFiles(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	var req struct {
		OwnerID  string `json:"owner_id"`
		Pattern  string `json:"pattern"`
		Path     string `json:"path"`
		FileGlob string `json:"file_glob"`
	}
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.Path == "" {
		req.Path = "."
	}
	if req.FileGlob == "" {
		req.FileGlob = "**/*"
	}
	if len(req.Pattern) == 0 || len(req.Pattern) > 500 || len(req.Path) > 1000 || len(req.FileGlob) > 200 {
		fail(w, 422, "Invalid request body.")
		return
	}
	result, err := s.workspaceSearch(r.PathValue("workspaceID"), req.OwnerID, req.Path, req.Pattern, req.FileGlob)
	if err != nil {
		fail(w, 400, err.Error())
		return
	}
	writeJSON(w, 200, result)
}

func (s *service) proposeChange(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	var req struct {
		OwnerID        string  `json:"owner_id"`
		Path           string  `json:"path"`
		Content        string  `json:"content"`
		ExpectedSHA256 *string `json:"expected_sha256"`
	}
	if !decodeJSON(w, r, &req) {
		return
	}
	if len(req.OwnerID) == 0 || len(req.OwnerID) > 512 || len(req.Path) == 0 || len(req.Path) > 1000 || len(req.Content) > 1<<20 || (req.ExpectedSHA256 != nil && len(*req.ExpectedSHA256) != 64) {
		fail(w, 422, "Invalid request body.")
		return
	}
	change, err := s.propose(r.PathValue("workspaceID"), req.OwnerID, req.Path, req.Content, req.ExpectedSHA256)
	if err != nil {
		fail(w, 400, err.Error())
		return
	}
	writeJSON(w, 201, change)
}

func (s *service) getChange(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	if requestOwner(r) == "" {
		fail(w, http.StatusUnprocessableEntity, "owner_id is required.")
		return
	}
	change, err := s.change(r.PathValue("workspaceID"), r.PathValue("changeID"), requestOwner(r))
	if err != nil {
		fail(w, 404, "Change proposal not found.")
		return
	}
	writeJSON(w, 200, change)
}

func (s *service) applyChange(w http.ResponseWriter, r *http.Request) { s.changeStatus(w, r, "apply") }
func (s *service) rollbackChange(w http.ResponseWriter, r *http.Request) {
	s.changeStatus(w, r, "rollback")
}
func (s *service) changeStatus(w http.ResponseWriter, r *http.Request, action string) {
	if !s.auth(w, r) {
		return
	}
	var req ownerRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	status, result, err := s.applyOrRollback(r.PathValue("workspaceID"), r.PathValue("changeID"), req.OwnerID, action)
	if err != nil {
		fail(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, result)
}
