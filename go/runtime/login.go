package main

// /login: Arthera account sign-in in the browser, ported from
// src/aria_code/apps/cli/web_login.py. The account page on Arthera's own
// host lets the user pick Google, Apple or email (or sign up), then hands a
// one-time code back to a loopback port this process listens on.
//
//   - state is random and checked on the way back, so a callback this run
//     did not start is ignored;
//   - a PKCE code_verifier stays here and only its SHA-256 goes into the
//     page, so the code is useless to anything else that sees the redirect;
//   - the session itself never appears in a URL: it is fetched by POST.
//
// The token's claims are checked against Arthera's Firebase project before
// it is saved; that is a consistency check, the API verifies the signature.

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const (
	artheraSiteURL   = "https://arthera.xyz"
	artheraAPIURL    = "https://api.arthera.xyz"
	artheraProjectID = "arthera-66e5b"
	loginTimeout     = 5 * time.Minute
)

// currentArtheraURL moves the retired arthera.finance hosts to today's.
func currentArtheraURL(value string, website bool) string {
	value = strings.TrimRight(strings.TrimSpace(value), "/")
	u, err := url.Parse(value)
	if err == nil {
		host := strings.ToLower(u.Hostname())
		if host == "arthera.finance" || strings.HasSuffix(host, ".arthera.finance") {
			if website {
				return artheraSiteURL
			}
			return artheraAPIURL
		}
	}
	return value
}

// loginAPIURL is ARIA_API_URL, else the configured api_url unless it is a
// loopback placeholder, else production (google_login.login_api_url).
func loginAPIURL(configured string) string {
	if override := currentArtheraURL(os.Getenv("ARIA_API_URL"), false); override != "" {
		return override
	}
	configured = currentArtheraURL(configured, false)
	u, err := url.Parse(configured)
	host := ""
	if err == nil {
		host = strings.ToLower(u.Hostname())
	}
	if configured == "" || host == "localhost" || host == "127.0.0.1" || host == "::1" || host == "0.0.0.0" {
		return artheraAPIURL
	}
	return configured
}

func loginAuthOrigin() string {
	if override := strings.TrimSpace(os.Getenv("ARIA_AUTH_URL")); override != "" {
		return currentArtheraURL(override, true)
	}
	return artheraSiteURL
}

func randomToken(n int) string {
	b := make([]byte, n)
	rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}

func pkcePair() (verifier, challenge string) {
	verifier = randomToken(48)
	sum := sha256.Sum256([]byte(verifier))
	return verifier, base64.RawURLEncoding.EncodeToString(sum[:])
}

// tokenClaims are the JWT's claims when it belongs to Arthera's Firebase
// project and names a user; nil otherwise. The signature is the API's to
// verify.
func tokenClaims(token string) map[string]any {
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return nil
	}
	body, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[1], "="))
	if err != nil {
		return nil
	}
	var claims map[string]any
	if json.Unmarshal(body, &claims) != nil {
		return nil
	}
	if claims["aud"] != artheraProjectID || claims["iss"] != "https://securetoken.google.com/"+artheraProjectID {
		return nil
	}
	if sub, _ := claims["sub"].(string); sub == "" {
		return nil
	}
	return claims
}

// openBrowser asks the desktop to open url; ARIA_OPEN_BROWSER=0 prints it
// only (and tests replace this).
var openBrowser = func(target string) error {
	if os.Getenv("ARIA_OPEN_BROWSER") == "0" {
		return errors.New("browser opening is off")
	}
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", target)
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", target)
	default:
		cmd = exec.Command("xdg-open", target)
	}
	return cmd.Start()
}

const loginPage = `<html><body style="font-family:system-ui;padding:3rem"><h2>%s</h2><p>You can close this tab and return to the terminal.</p></body></html>`

func (s *server) loginCommand(ctx context.Context, turn string) (string, error) {
	if remoteScope() {
		return "", errors.New("sign in on your own machine; a remote session cannot receive the browser's hand-back")
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", err
	}
	port := listener.Addr().(*net.TCPAddr).Port
	state := randomToken(24)
	verifier, challenge := pkcePair()
	codes := make(chan url.Values, 1)
	server := &http.Server{ReadHeaderTimeout: 10 * time.Second, Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		params := r.URL.Query()
		if r.URL.Path != "/callback" || subtle.ConstantTimeCompare([]byte(params.Get("state")), []byte(state)) != 1 {
			// Not this run's callback: ignore it and keep waiting.
			http.NotFound(w, r)
			return
		}
		ok := params.Get("handoff_code") != ""
		title := "Sign-in failed."
		if ok {
			title = "Signed in."
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Cache-Control", "no-store")
		if !ok {
			w.WriteHeader(http.StatusBadRequest)
		}
		fmt.Fprintf(w, loginPage, title)
		select {
		case codes <- params:
		default:
		}
	})}
	go server.Serve(listener)
	defer server.Close()

	page := loginAuthOrigin() + "/cli-login?" + url.Values{"port": {fmt.Sprint(port)}, "state": {state},
		"code_challenge": {challenge}, "code_challenge_method": {"S256"}}.Encode()
	notice := "Opening your browser to sign in or create an account…"
	if openBrowser(page) != nil {
		notice = "Open this page to sign in or create an account:"
	}
	s.emit("answer.delta", turn, map[string]any{"text": notice + "\n" + page + "\n\nWaiting for the browser (Esc cancels)…\n"})

	var params url.Values
	select {
	case params = <-codes:
	case <-time.After(loginTimeout):
		return "", fmt.Errorf("timed out after %s waiting for the browser", loginTimeout)
	case <-ctx.Done():
		return "", ctx.Err()
	}
	code := params.Get("handoff_code")
	if code == "" {
		message := params.Get("message")
		if message == "" {
			message = "the sign-in did not carry a handoff code"
		}
		return "", errors.New(message)
	}
	session, err := exchangeLogin(ctx, loginAPIURL(s.cfg.text("api_url", "")), code, verifier, state)
	if err != nil {
		return "", err
	}
	return s.acceptLogin(session)
}

type loginSession struct {
	AuthToken    string `json:"auth_token"`
	RefreshToken string `json:"refresh_token"`
	UserID       string `json:"user_id"`
	Email        string `json:"email"`
	DisplayName  string `json:"display_name"`
}

func exchangeLogin(ctx context.Context, api, code, verifier, state string) (loginSession, error) {
	body, _ := json.Marshal(map[string]string{"code": code, "code_verifier": verifier, "state": state})
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(api, "/")+"/api/v2/auth/cli/exchange", bytes.NewReader(body))
	if err != nil {
		return loginSession{}, err
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return loginSession{}, fmt.Errorf("could not redeem the sign-in: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return loginSession{}, fmt.Errorf("could not redeem the sign-in: HTTP %d", response.StatusCode)
	}
	var reply struct {
		Data loginSession `json:"data"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&reply); err != nil {
		return loginSession{}, fmt.Errorf("could not redeem the sign-in: %w", err)
	}
	if reply.Data.AuthToken == "" {
		return loginSession{}, errors.New("the sign-in did not return a session")
	}
	return reply.Data, nil
}

// acceptLogin saves the session when its token names the user it claims to.
func (s *server) acceptLogin(session loginSession) (string, error) {
	claims := tokenClaims(session.AuthToken)
	if claims == nil || session.UserID == "" || claims["sub"] != session.UserID {
		return "", errors.New("the session identity could not be verified; run /login again")
	}
	email, _ := claims["email"].(string)
	if email == "" {
		email = session.Email
	}
	email = strings.ToLower(strings.TrimSpace(email))
	name := session.DisplayName
	if name == "" {
		name, _ = claims["name"].(string)
	}
	disk, err := loadConfig(s.opts.root)
	if err != nil {
		return "", err
	}
	values := map[string]any{"auth_token": session.AuthToken, "user_id": session.UserID, "email": email,
		"display_name": name, "auth_provider": "firebase"}
	delete(disk, "refresh_token")
	delete(disk, "token_expires_at")
	delete(s.cfg, "refresh_token")
	delete(s.cfg, "token_expires_at")
	if session.RefreshToken != "" {
		values["refresh_token"] = session.RefreshToken
	}
	if exp, ok := claims["exp"].(float64); ok {
		values["token_expires_at"] = time.Unix(int64(exp), 0).UTC().Format(time.RFC3339)
	}
	for key, value := range values {
		disk[key], s.cfg[key] = value, value
	}
	if err := atomicJSON(filepath.Join(s.opts.root, "config.json"), disk); err != nil {
		return "", err
	}
	who := email
	if who == "" {
		who = session.UserID
	}
	return "Signed in as " + who + ".", nil
}
