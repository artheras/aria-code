package main

// Canva Connect for aria-runtime mcp: the Go port of canva_client.py.
//
// Canva has no API that draws a design from scratch; Autofill fills a brand
// template made in the Canva app with data. So this signs in (OAuth 2 with
// PKCE, `aria-runtime canva connect`), uploads assets, and autofills.
//
// The app's client id and secret come from the environment
// (CANVA_CLIENT_ID, CANVA_CLIENT_SECRET) and are never written down. The
// session tokens Canva returns are kept like Aria's own sign-in: in the
// state directory, readable by the user only (canva_tokens.json, 0600).

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

var (
	canvaAuthURL      = "https://www.canva.com/api/oauth/authorize"
	canvaAPI          = "https://api.canva.com/rest/v1"
	canvaPollInterval = 2 * time.Second
)

const (
	canvaRedirectPort = 53219 // must match the redirect URL registered for the Canva app
	canvaScopes       = "asset:read asset:write brandtemplate:meta:read brandtemplate:content:read design:content:read design:content:write"
	canvaPollTimeout  = 90 * time.Second
)

type canvaTokens struct {
	AccessToken  string  `json:"access_token"`
	RefreshToken string  `json:"refresh_token"`
	ExpiresAt    float64 `json:"expires_at"`
}

func canvaTokenPath() (string, error) {
	state, err := stateRoot()
	if err != nil {
		return "", err
	}
	return filepath.Join(state, "canva_tokens.json"), nil
}

func saveCanvaTokens(t canvaTokens) error {
	path, err := canvaTokenPath()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	data, _ := json.MarshalIndent(t, "", "  ")
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func canvaApp() (id, secret string, err error) {
	id, secret = envKey("CANVA_CLIENT_ID"), envKey("CANVA_CLIENT_SECRET")
	if id == "" || secret == "" {
		return "", "", errors.New("set CANVA_CLIENT_ID and CANVA_CLIENT_SECRET (your Canva Connect app at canva.com/developers) in the environment Aria runs in")
	}
	return id, secret, nil
}

// canvaTokenRequest posts to the token endpoint with the app's basic auth.
func canvaTokenRequest(ctx context.Context, form url.Values) (canvaTokens, error) {
	id, secret, err := canvaApp()
	if err != nil {
		return canvaTokens{}, err
	}
	var reply struct {
		AccessToken  string  `json:"access_token"`
		RefreshToken string  `json:"refresh_token"`
		ExpiresIn    float64 `json:"expires_in"`
	}
	err = mediaRequest(ctx, "Canva", http.MethodPost, canvaAPI+"/oauth/token", map[string]string{
		"Authorization": "Basic " + base64.StdEncoding.EncodeToString([]byte(id+":"+secret)),
		"Content-Type":  "application/x-www-form-urlencoded",
	}, strings.NewReader(form.Encode()), &reply)
	if err != nil {
		return canvaTokens{}, err
	}
	if reply.AccessToken == "" {
		return canvaTokens{}, errors.New("Canva returned no access token")
	}
	if reply.ExpiresIn <= 0 {
		reply.ExpiresIn = 3600
	}
	return canvaTokens{reply.AccessToken, reply.RefreshToken, float64(time.Now().Unix()) + reply.ExpiresIn}, nil
}

// canvaAccessToken is a live access token, refreshed when within a minute of expiry.
func canvaAccessToken(ctx context.Context) (string, error) {
	path, err := canvaTokenPath()
	if err != nil {
		return "", err
	}
	var t canvaTokens
	if data, err := os.ReadFile(path); err != nil || json.Unmarshal(data, &t) != nil || t.AccessToken == "" {
		return "", errors.New("Canva is not connected: run `aria-runtime canva connect` once")
	}
	if t.ExpiresAt-float64(time.Now().Unix()) > 60 {
		return t.AccessToken, nil
	}
	if t.RefreshToken == "" {
		return "", errors.New("the Canva session expired: run `aria-runtime canva connect` again")
	}
	fresh, err := canvaTokenRequest(ctx, url.Values{"grant_type": {"refresh_token"}, "refresh_token": {t.RefreshToken}})
	if err != nil {
		return "", fmt.Errorf("refreshing the Canva session failed (%v): run `aria-runtime canva connect` again", err)
	}
	if fresh.RefreshToken == "" {
		fresh.RefreshToken = t.RefreshToken
	}
	if err := saveCanvaTokens(fresh); err != nil {
		return "", err
	}
	return fresh.AccessToken, nil
}

func randomURLToken(n int) string {
	b := make([]byte, n)
	rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}

// canvaConnect runs the authorization-code flow with PKCE and keeps the tokens.
func canvaConnect(ctx context.Context, out io.Writer, timeout time.Duration) error {
	id, _, err := canvaApp()
	if err != nil {
		return err
	}
	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", canvaRedirectPort))
	if err != nil {
		return fmt.Errorf("port %d is needed for Canva's redirect and is busy: %v", canvaRedirectPort, err)
	}
	defer listener.Close()
	redirect := fmt.Sprintf("http://127.0.0.1:%d/callback", canvaRedirectPort)
	verifier := randomURLToken(48)
	sum := sha256.Sum256([]byte(verifier))
	state := randomURLToken(16)
	auth := canvaAuthURL + "?" + url.Values{
		"response_type": {"code"}, "client_id": {id}, "redirect_uri": {redirect}, "scope": {canvaScopes},
		"state": {state}, "code_challenge": {base64.RawURLEncoding.EncodeToString(sum[:])}, "code_challenge_method": {"S256"},
	}.Encode()

	type result struct{ code, err string }
	done := make(chan result, 1)
	srv := &http.Server{ReadHeaderTimeout: 10 * time.Second, Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/callback" {
			http.NotFound(w, r)
			return
		}
		q := r.URL.Query()
		res := result{code: q.Get("code"), err: q.Get("error")}
		if q.Get("state") != state {
			res = result{err: "the reply did not match this sign-in (state)"}
		} else if res.code == "" && res.err == "" {
			res.err = "Canva sent no code"
		}
		title := "Canva is connected to Aria."
		if res.err != "" {
			title = "Canva sign-in failed."
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprintf(w, loginPage, title)
		select {
		case done <- res:
		default:
		}
	})}
	go srv.Serve(listener)
	defer srv.Close()

	fmt.Fprintf(out, "Opening Canva to approve access. If no browser opens, visit:\n%s\n", auth)
	openBrowser(auth)
	var res result
	select {
	case res = <-done:
	case <-time.After(timeout):
		return fmt.Errorf("no answer from Canva within %s", timeout)
	case <-ctx.Done():
		return ctx.Err()
	}
	if res.err != "" {
		return fmt.Errorf("Canva sign-in failed: %s", capRunes(res.err, 200))
	}
	tokens, err := canvaTokenRequest(ctx, url.Values{"grant_type": {"authorization_code"}, "code": {res.code},
		"redirect_uri": {redirect}, "code_verifier": {verifier}})
	if err != nil {
		return err
	}
	return saveCanvaTokens(tokens)
}

// canvaCommand is `aria-runtime canva connect|status|disconnect`.
func canvaCommand(args []string, out, errOut io.Writer) int {
	if len(args) != 1 {
		fmt.Fprintln(errOut, "usage: aria-runtime canva connect|status|disconnect")
		return 2
	}
	path, err := canvaTokenPath()
	if err != nil {
		fmt.Fprintln(errOut, err)
		return 1
	}
	switch args[0] {
	case "connect":
		if remoteScope() {
			fmt.Fprintln(errOut, "connect Canva on your own machine; a remote session cannot receive the browser's hand-back")
			return 1
		}
		if err := canvaConnect(context.Background(), out, 3*time.Minute); err != nil {
			fmt.Fprintln(errOut, err)
			return 1
		}
		fmt.Fprintln(out, "Canva is connected.")
	case "status":
		if _, err := canvaAccessToken(context.Background()); err != nil {
			fmt.Fprintln(out, err)
			return 1
		}
		fmt.Fprintln(out, "Canva is connected.")
	case "disconnect":
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			fmt.Fprintln(errOut, err)
			return 1
		}
		fmt.Fprintln(out, "Canva is disconnected on this machine. Revoke the app's access in Canva's settings to end it there too.")
	default:
		fmt.Fprintln(errOut, "usage: aria-runtime canva connect|status|disconnect")
		return 2
	}
	return 0
}

// canvaJob polls a Canva job until it succeeds or fails.
func canvaJob(ctx context.Context, token, path string, job map[string]any) (map[string]any, error) {
	deadline := time.Now().Add(canvaPollTimeout)
	for {
		status, _ := job["status"].(string)
		switch status {
		case "success":
			return job, nil
		case "failed":
			msg := "the Canva job failed"
			if e, ok := job["error"].(map[string]any); ok {
				if m, ok := e["message"].(string); ok && m != "" {
					msg = m
				}
			}
			return nil, errors.New(capRunes(msg, 300))
		}
		id, _ := job["id"].(string)
		if !taskIDPattern.MatchString(id) {
			return nil, errors.New("Canva returned no job id")
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("the Canva job %s did not finish within %s", id, canvaPollTimeout)
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(canvaPollInterval):
		}
		var reply struct {
			Job map[string]any `json:"job"`
		}
		if err := mediaRequest(ctx, "Canva", http.MethodGet, canvaAPI+path+"/"+id, map[string]string{"Authorization": "Bearer " + token}, nil, &reply); err != nil {
			return nil, err
		}
		job = reply.Job
	}
}

var canvaID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

var canvaValueTypes = map[string]bool{"text": true, "image": true, "chart": true}

func mcpCanvaDesign(ctx context.Context, args map[string]any) map[string]any {
	template, _ := args["template_id"].(string)
	template = strings.TrimSpace(template)
	data, ok := args["data"].(map[string]any)
	if !canvaID.MatchString(template) || !ok || len(data) == 0 {
		return mcpFail("template_id and data are required: data maps each template field to {\"type\": \"text\", \"text\": …}, {\"type\": \"image\", \"asset_id\": …} or {\"type\": \"chart\", …}")
	}
	for field, value := range data {
		v, ok := value.(map[string]any)
		kind, _ := v["type"].(string)
		if !ok || !canvaValueTypes[kind] {
			return mcpFail(fmt.Sprintf("data.%s must be an object whose type is text, image or chart", capRunes(field, 40)))
		}
	}
	title, _ := args["title"].(string)
	token, err := canvaAccessToken(ctx)
	if err != nil {
		return mcpFail(err.Error())
	}
	body := map[string]any{"brand_template_id": template, "data": data}
	if t := strings.TrimSpace(title); t != "" {
		body["title"] = capRunes(t, 255)
	}
	var reply struct {
		Job map[string]any `json:"job"`
	}
	if err := mediaRequest(ctx, "Canva", http.MethodPost, canvaAPI+"/autofills", map[string]string{"Authorization": "Bearer " + token, "Content-Type": "application/json"}, jsonBody(body), &reply); err != nil {
		return mcpFail(err.Error())
	}
	job, err := canvaJob(ctx, token, "/autofills", reply.Job)
	if err != nil {
		return mcpFail(err.Error())
	}
	result, _ := job["result"].(map[string]any)
	design, _ := result["design"].(map[string]any)
	urls, _ := design["urls"].(map[string]any)
	out := map[string]any{"success": true, "design_id": design["id"], "title": design["title"], "design_url": design["url"]}
	if edit, ok := urls["edit_url"].(string); ok {
		out["edit_url"] = edit
	}
	if view, ok := urls["view_url"].(string); ok {
		out["view_url"] = view
	}
	return out
}

func mcpCanvaUploadAsset(ctx context.Context, args map[string]any) map[string]any {
	raw, _ := args["file_path"].(string)
	path, err := mcpPathIn(raw, true)
	if err != nil {
		return mcpFail(err.Error())
	}
	info, err := os.Stat(path)
	if err != nil || info.Size() > maxImageUpload {
		return mcpFail(fmt.Sprintf("file_path must be an image or video of at most %d MiB", maxImageUpload>>20))
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return mcpFail(err.Error())
	}
	switch kind := http.DetectContentType(data); {
	case strings.HasPrefix(kind, "image/"), strings.HasPrefix(kind, "video/"):
	case bytes.Contains(data[:min(len(data), 512)], []byte("<svg")):
	default:
		return mcpFail("file_path must be an image or video (Canva assets)")
	}
	name, _ := args["name"].(string)
	if strings.TrimSpace(name) == "" {
		name = filepath.Base(path)
	}
	token, err := canvaAccessToken(ctx)
	if err != nil {
		return mcpFail(err.Error())
	}
	meta, _ := json.Marshal(map[string]string{"name_base64": base64.StdEncoding.EncodeToString([]byte(capRunes(name, 50)))})
	var reply struct {
		Job map[string]any `json:"job"`
	}
	if err := mediaRequest(ctx, "Canva", http.MethodPost, canvaAPI+"/asset-uploads", map[string]string{
		"Authorization": "Bearer " + token, "Content-Type": "application/octet-stream", "Asset-Upload-Metadata": string(meta),
	}, bytes.NewReader(data), &reply); err != nil {
		return mcpFail(err.Error())
	}
	job, err := canvaJob(ctx, token, "/asset-uploads", reply.Job)
	if err != nil {
		return mcpFail(err.Error())
	}
	asset, _ := job["asset"].(map[string]any)
	id, _ := asset["id"].(string)
	if id == "" {
		return mcpFail("Canva finished the upload but returned no asset id")
	}
	return map[string]any{"success": true, "asset_id": id, "name": asset["name"],
		"use_as": map[string]any{"type": "image", "asset_id": id}}
}

func init() {
	canvaTool := func(t mcpServedTool) mcpServedTool {
		t.Annotations["openWorldHint"] = true
		return t
	}
	mcpServerTools = append(mcpServerTools,
		canvaTool(writeTool("aria.report.canva_design", "Fill a Canva brand template with data (Autofill) and create a new design in the connected Canva account; returns its links. Connect once with `aria-runtime canva connect`.",
			map[string]any{
				"template_id": map[string]any{"type": "string", "description": "Canva brand template id."},
				"data":        map[string]any{"type": "object", "description": "Field name → {\"type\": \"text\", \"text\": \"Q3 Report\"}, {\"type\": \"image\", \"asset_id\": \"<from aria.report.canva_upload_asset>\"} or {\"type\": \"chart\", \"chart_data\": …}."},
				"title":       map[string]any{"type": "string", "description": "Optional title for the new design."},
			}, "template_id", "data")),
		canvaTool(writeTool("aria.report.canva_upload_asset", "Upload a local image or video (e.g. a chart) to the connected Canva account; returns an asset_id for aria.report.canva_design.",
			map[string]any{
				"file_path": map[string]any{"type": "string", "description": "Image or video under the working or home directory."},
				"name":      map[string]any{"type": "string", "description": "Optional asset name (default: the file name)."},
			}, "file_path")),
	)
	mcpServerHandlers["aria.report.canva_design"] = mcpCanvaDesign
	mcpServerHandlers["aria.report.canva_upload_asset"] = mcpCanvaUploadAsset
}
