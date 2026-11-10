package main

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func fakeToken(claims map[string]any) string {
	body, _ := json.Marshal(claims)
	return "e30." + base64.RawURLEncoding.EncodeToString(body) + ".sig"
}

func goodClaims() map[string]any {
	return map[string]any{"aud": artheraProjectID, "iss": "https://securetoken.google.com/" + artheraProjectID,
		"sub": "user-1", "email": "Person@Example.invalid", "exp": float64(time.Now().Add(time.Hour).Unix())}
}

func TestLoginRunsTheBrowserHandOff(t *testing.T) {
	var challenge string
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]string
		json.NewDecoder(r.Body).Decode(&body)
		sum := sha256.Sum256([]byte(body["code_verifier"]))
		if r.URL.Path != "/api/v2/auth/cli/exchange" || body["code"] != "one-time" || base64.RawURLEncoding.EncodeToString(sum[:]) != challenge {
			w.WriteHeader(400)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{"auth_token": fakeToken(goodClaims()), "user_id": "user-1", "refresh_token": "r"}})
	}))
	defer api.Close()
	t.Setenv("ARIA_API_URL", api.URL)
	old := openBrowser
	defer func() { openBrowser = old }()
	stray := 0
	openBrowser = func(target string) error {
		page, _ := url.Parse(target)
		q := page.Query()
		challenge = q.Get("code_challenge")
		if page.Path != "/cli-login" || q.Get("code_challenge_method") != "S256" {
			t.Error(target)
		}
		go func() {
			base := "http://127.0.0.1:" + q.Get("port") + "/callback?"
			if r, err := http.Get(base + "state=forged&handoff_code=stolen"); err == nil {
				if r.StatusCode == 404 {
					stray++
				}
				r.Body.Close()
			}
			if r, err := http.Get(base + url.Values{"state": {q.Get("state")}, "handoff_code": {"one-time"}}.Encode()); err == nil {
				r.Body.Close()
			}
		}()
		return nil
	}
	s := approvalServer(t, config{"model": "m"}, appOptions{})
	out, err := s.command(context.Background(), "t", "/login")
	if err != nil || out != "Signed in as person@example.invalid." {
		t.Fatal(out, err)
	}
	if stray != 1 {
		t.Fatal("a callback with the wrong state was not ignored")
	}
	disk, _ := loadConfig(s.opts.root)
	if disk["user_id"] != "user-1" || disk["refresh_token"] != "r" || disk["model"] != "m" || disk["token_expires_at"] == nil {
		t.Fatal(disk)
	}
	if who, _ := s.command(context.Background(), "t", "/whoami"); !strings.Contains(who, "user-1") {
		t.Fatal(who)
	}
}

func TestLoginRefusesASessionThatDoesNotMatch(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	other := goodClaims()
	other["aud"] = "someone-else"
	for _, session := range []loginSession{
		{AuthToken: fakeToken(goodClaims()), UserID: "user-2"},
		{AuthToken: fakeToken(other), UserID: "user-1"},
		{AuthToken: "not-a-jwt", UserID: "user-1"},
	} {
		if _, err := s.acceptLogin(session); err == nil {
			t.Fatal("accepted", session)
		}
	}
	if disk, _ := loadConfig(s.opts.root); disk["auth_token"] != nil {
		t.Fatal("a refused session was saved")
	}
}

func TestLoginURLs(t *testing.T) {
	t.Setenv("ARIA_API_URL", "")
	for configured, want := range map[string]string{"": artheraAPIURL, "http://localhost:8000": artheraAPIURL, "https://api.arthera.finance": artheraAPIURL, "https://gw.example.com/": "https://gw.example.com"} {
		if got := loginAPIURL(configured); got != want {
			t.Fatal(configured, got)
		}
	}
	if got := fmt.Sprint(loginAuthOrigin()); got != artheraSiteURL {
		t.Fatal(got)
	}
	t.Setenv("ARIA_RUNTIME_SCOPE", "remote")
	s := approvalServer(t, config{}, appOptions{})
	if _, err := s.command(context.Background(), "t", "/login"); err == nil {
		t.Fatal("remote login accepted")
	}
}
