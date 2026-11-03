package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// canvaStub stands in for Canva's token, autofill and asset-upload endpoints.
func canvaStub(t *testing.T) *mediaStub {
	t.Helper()
	polls := map[string]int{}
	stub := newMediaStub(t, func(w http.ResponseWriter, r *http.Request, body []byte) {
		switch {
		case r.URL.Path == "/canva/oauth/token":
			form, _ := url.ParseQuery(string(body))
			user, pass, _ := r.BasicAuth()
			if user != "app" || pass != "pw" {
				w.WriteHeader(401)
				w.Write([]byte(`{"message":"bad client"}`))
				return
			}
			switch form.Get("grant_type") {
			case "authorization_code":
				if form.Get("code") != "code1" || form.Get("code_verifier") == "" {
					w.WriteHeader(400)
					return
				}
				w.Write([]byte(`{"access_token":"aaaa","refresh_token":"rrrr","expires_in":3600}`))
			case "refresh_token":
				w.Write([]byte(`{"access_token":"bbbb","expires_in":3600}`))
			}
		case r.Header.Get("Authorization") != "Bearer aaaa" && r.Header.Get("Authorization") != "Bearer bbbb":
			w.WriteHeader(401)
		case r.Method == "POST" && r.URL.Path == "/canva/autofills":
			if strings.Contains(string(body), "tpl-bad") {
				w.Write([]byte(`{"job":{"id":"job-bad","status":"in_progress"}}`))
				return
			}
			w.Write([]byte(`{"job":{"id":"job-1","status":"in_progress"}}`))
		case r.URL.Path == "/canva/autofills/job-1":
			polls["job-1"]++
			w.Write([]byte(`{"job":{"id":"job-1","status":"success","result":{"type":"create_design","design":{"id":"D1","title":"Q3","url":"https://www.canva.com/d/D1","urls":{"edit_url":"https://www.canva.com/d/D1/edit"}}}}}`))
		case r.URL.Path == "/canva/autofills/job-bad":
			w.Write([]byte(`{"job":{"id":"job-bad","status":"failed","error":{"code":"autofill_error","message":"field headline is missing"}}}`))
		case r.Method == "POST" && r.URL.Path == "/canva/asset-uploads":
			w.Write([]byte(`{"job":{"id":"up-1","status":"in_progress"}}`))
		case r.URL.Path == "/canva/asset-uploads/up-1":
			w.Write([]byte(`{"job":{"id":"up-1","status":"success","asset":{"id":"A1","name":"chart.png"}}}`))
		default:
			w.WriteHeader(404)
		}
	})
	savedAPI, savedAuth, savedPoll := canvaAPI, canvaAuthURL, canvaPollInterval
	canvaAPI, canvaAuthURL, canvaPollInterval = stub.srv.URL+"/canva", stub.srv.URL+"/authorize", time.Millisecond
	t.Cleanup(func() { canvaAPI, canvaAuthURL, canvaPollInterval = savedAPI, savedAuth, savedPoll })
	return stub
}

func TestCanvaConnectAndTokens(t *testing.T) {
	home := chartHome(t)
	canvaStub(t)
	t.Setenv("CANVA_CLIENT_ID", "")
	var out bytes.Buffer
	if err := canvaConnect(context.Background(), &out, time.Second); err == nil || !strings.Contains(err.Error(), "CANVA_CLIENT_ID") {
		t.Fatal(err)
	}
	t.Setenv("CANVA_CLIENT_ID", "app")
	t.Setenv("CANVA_CLIENT_SECRET", "pw")
	// The "browser" approves at once: it follows the redirect with the code and state.
	savedOpen := openBrowser
	t.Cleanup(func() { openBrowser = savedOpen })
	var auth *url.URL
	openBrowser = func(target string) error {
		auth, _ = url.Parse(target)
		q := auth.Query()
		go http.Get(q.Get("redirect_uri") + "?code=code1&state=" + url.QueryEscape(q.Get("state")))
		return nil
	}
	if err := canvaConnect(context.Background(), &out, 5*time.Second); err != nil {
		t.Fatal(err)
	}
	q := auth.Query()
	if q.Get("code_challenge_method") != "S256" || q.Get("client_id") != "app" || !strings.HasSuffix(q.Get("redirect_uri"), ":53219/callback") ||
		!strings.Contains(q.Get("scope"), "asset:write") || !strings.Contains(q.Get("scope"), "brandtemplate:content:read") {
		t.Fatal(q)
	}
	path := filepath.Join(home, ".aria", "canva_tokens.json")
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
		t.Fatal("the token file must be private", info.Mode())
	}
	data, _ := os.ReadFile(path)
	if strings.Contains(string(data), "pw") {
		t.Fatal("the client secret must not be written down")
	}
	if token, err := canvaAccessToken(context.Background()); err != nil || token != "aaaa" {
		t.Fatal(token, err)
	}

	// An expiring token is refreshed and the refresh token kept.
	saveCanvaTokens(canvaTokens{AccessToken: "old", RefreshToken: "rrrr", ExpiresAt: float64(time.Now().Unix() + 10)})
	if token, err := canvaAccessToken(context.Background()); err != nil || token != "bbbb" {
		t.Fatal(token, err)
	}
	var saved canvaTokens
	data, _ = os.ReadFile(path)
	json.Unmarshal(data, &saved)
	if saved.RefreshToken != "rrrr" || saved.AccessToken != "bbbb" {
		t.Fatal(saved)
	}

	// A reply for another sign-in is refused.
	openBrowser = func(target string) error {
		u, _ := url.Parse(target)
		go http.Get(u.Query().Get("redirect_uri") + "?code=code1&state=other")
		return nil
	}
	if err := canvaConnect(context.Background(), &out, 5*time.Second); err == nil || !strings.Contains(err.Error(), "state") {
		t.Fatal(err)
	}

	var stdout, stderr bytes.Buffer
	if code := canvaCommand([]string{"status"}, &stdout, &stderr); code != 0 {
		t.Fatal(stdout.String(), stderr.String())
	}
	if code := canvaCommand([]string{"disconnect"}, &stdout, &stderr); code != 0 {
		t.Fatal(stderr.String())
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("disconnect should remove the tokens")
	}
	if code := canvaCommand([]string{"status"}, &stdout, &stderr); code != 1 || !strings.Contains(stdout.String(), "canva connect") {
		t.Fatal(code, stdout.String())
	}
	if code := canvaCommand(nil, &stdout, &stderr); code != 2 {
		t.Fatal(code)
	}
}

func TestCanvaTools(t *testing.T) {
	home := chartHome(t)
	stub := canvaStub(t)
	ctx := context.Background()
	fields := map[string]any{"headline": map[string]any{"type": "text", "text": "Q3"}}
	if r := mcpCanvaDesign(ctx, map[string]any{"template_id": "tpl1", "data": fields}); r["success"] != false || !strings.Contains(r["error"].(string), "canva connect") {
		t.Fatal("not connected yet", r)
	}
	saveCanvaTokens(canvaTokens{AccessToken: "aaaa", RefreshToken: "rrrr", ExpiresAt: float64(time.Now().Add(time.Hour).Unix())})

	r := mcpCanvaDesign(ctx, map[string]any{"template_id": "tpl1", "data": fields, "title": "Q3 deck"})
	if r["success"] != true || r["design_id"] != "D1" || r["edit_url"] != "https://www.canva.com/d/D1/edit" {
		t.Fatal(r)
	}
	req, body := stub.requests[0], stub.bodies[0]
	var sent map[string]any
	json.Unmarshal(body, &sent)
	if req.URL.Path != "/canva/autofills" || sent["brand_template_id"] != "tpl1" || sent["title"] != "Q3 deck" {
		t.Fatal(string(body))
	}
	if r := mcpCanvaDesign(ctx, map[string]any{"template_id": "tpl-bad", "data": fields}); r["success"] != false || r["error"] != "field headline is missing" {
		t.Fatal(r)
	}
	for _, bad := range []map[string]any{
		{"template_id": "tpl1"},
		{"template_id": "../x", "data": fields},
		{"template_id": "tpl1", "data": map[string]any{"headline": "plain text"}},
		{"template_id": "tpl1", "data": map[string]any{"headline": map[string]any{"type": "sheet"}}},
	} {
		if r := mcpCanvaDesign(ctx, bad); r["success"] != false {
			t.Fatal("accepted", bad)
		}
	}

	os.WriteFile(filepath.Join(home, "chart.png"), tinyPNG(t, 4, 4), 0o644)
	r = mcpCanvaUploadAsset(ctx, map[string]any{"file_path": "chart.png"})
	if r["success"] != true || r["asset_id"] != "A1" || r["use_as"].(map[string]any)["asset_id"] != "A1" {
		t.Fatal(r)
	}
	req, _ = stub.last()
	for _, q := range stub.requests {
		if q.URL.Path == "/canva/asset-uploads" {
			req = q
		}
	}
	var meta map[string]string
	json.Unmarshal([]byte(req.Header.Get("Asset-Upload-Metadata")), &meta)
	if name, _ := base64.StdEncoding.DecodeString(meta["name_base64"]); string(name) != "chart.png" || req.Header.Get("Content-Type") != "application/octet-stream" {
		t.Fatal(req.Header)
	}
	os.WriteFile(filepath.Join(home, "notes.txt"), []byte("not an image"), 0o644)
	for _, bad := range []string{"notes.txt", "/etc/hosts", "missing.png"} {
		if r := mcpCanvaUploadAsset(ctx, map[string]any{"file_path": bad}); r["success"] != false {
			t.Fatal("accepted", bad)
		}
	}
}
