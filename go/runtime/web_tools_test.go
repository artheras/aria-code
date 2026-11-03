package main

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestFetchURLAndPrivateAddresses(t *testing.T) {
	for raw, ok := range map[string]bool{
		"example.com/a": true, "https://example.com": true, "http://example.com:8080/x": true,
		"": false, "ftp://example.com": false, "file:///etc/passwd": false, "https://user:pw@example.com": false, "https://": false,
	} {
		if _, err := fetchURL(raw); (err == nil) != ok {
			t.Errorf("%q: %v", raw, err)
		}
	}
	if u, _ := fetchURL("example.com/a"); u.Scheme != "https" {
		t.Fatal("a bare host defaults to https")
	}
	for ip, private := range map[string]bool{
		"127.0.0.1": true, "10.1.2.3": true, "172.16.0.1": true, "192.168.1.1": true, "169.254.169.254": true,
		"100.64.0.1": true, "::1": true, "fe80::1": true, "0.0.0.0": true, "8.8.8.8": false, "2606:4700::1111": false,
	} {
		if got := privateIP(net.ParseIP(ip)); got != private {
			t.Errorf("%s: %v", ip, got)
		}
	}
	t.Setenv("ARIA_WEB_FETCH_ALLOW_PRIVATE", "")
	ctx := context.Background()
	for _, host := range []string{"127.0.0.1", "localhost", "api.localhost", "169.254.169.254", "::1"} {
		if checkHost(ctx, host) == nil {
			t.Errorf("%s was allowed", host)
		}
	}
	if checkHost(ctx, "8.8.8.8") != nil {
		t.Fatal("a public address is allowed")
	}
	if m := githubBlob.FindStringSubmatch("https://github.com/a/b/blob/main/README.md"); m == nil || m[1] != "a/b" || m[2] != "main/README.md" {
		t.Fatal(m)
	}
}

func TestWebFetch(t *testing.T) {
	ctx := context.Background()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/page":
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.Write([]byte(`<html><head><title>Q3 results</title><style>p{}</style><script>steal()</script></head>
				<body><h1>Revenue</h1><p>Revenue grew 12%.</p><noscript>enable js</noscript><p>Margins held &amp; improved.</p></body></html>`))
		case "/data":
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"price": 101.5}`))
		case "/long":
			w.Header().Set("Content-Type", "text/plain")
			w.Write([]byte(strings.Repeat("a", 3000)))
		case "/image":
			w.Header().Set("Content-Type", "image/png")
			w.Write([]byte("\x89PNG"))
		case "/redirect":
			http.Redirect(w, r, "/page", http.StatusFound)
		case "/ua":
			w.Write([]byte(r.UserAgent()))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	t.Setenv("ARIA_WEB_FETCH_ALLOW_PRIVATE", "")
	if r := webFetch(ctx, map[string]any{"url": srv.URL + "/page"}); r.Success || !strings.Contains(r.Error, "private") {
		t.Fatal("a local server must be refused by default", r)
	}
	t.Setenv("ARIA_WEB_FETCH_ALLOW_PRIVATE", "1")
	r := webFetch(ctx, map[string]any{"url": srv.URL + "/redirect"})
	text, _ := r.Data["text"].(string)
	if !r.Success || r.Data["title"] != "Q3 results" || !strings.Contains(text, "Revenue grew 12%.") || !strings.Contains(text, "Margins held & improved.") {
		t.Fatal(r)
	}
	if strings.Contains(text, "steal") || strings.Contains(text, "enable js") || strings.Contains(text, "p{}") {
		t.Fatal("scripts, styles and noscript are not text", text)
	}
	if !strings.HasSuffix(r.Data["url"].(string), "/page") {
		t.Fatal("the final URL after redirects is reported", r.Data["url"])
	}
	if r := webFetch(ctx, map[string]any{"url": srv.URL + "/data"}); !r.Success || r.Data["text"] != `{"price": 101.5}` {
		t.Fatal(r)
	}
	if r := webFetch(ctx, map[string]any{"url": srv.URL + "/long", "max_chars": 1000}); r.Data["truncated"] != true || len(r.Data["text"].(string)) != 1000 || r.Data["length"] != 3000 {
		t.Fatal(r.Data["truncated"], r.Data["length"])
	}
	if r := webFetch(ctx, map[string]any{"url": srv.URL + "/image"}); r.Success || !strings.Contains(r.Error, "image/png") {
		t.Fatal(r)
	}
	if r := webFetch(ctx, map[string]any{"url": srv.URL + "/missing"}); r.Success || !strings.Contains(r.Error, "HTTP 404") {
		t.Fatal(r)
	}
	if r := webFetch(ctx, map[string]any{"url": srv.URL + "/ua"}); !strings.Contains(r.Data["text"].(string), "Aria/") {
		t.Fatal("a declarative user agent", r.Data["text"])
	}

	// Every redirect is checked again.
	t.Setenv("ARIA_WEB_FETCH_ALLOW_PRIVATE", "")
	req, _ := http.NewRequest(http.MethodGet, "http://127.0.0.1/x", nil)
	if webClient(0).CheckRedirect(req, []*http.Request{req}) == nil {
		t.Fatal("a redirect to a private address must be refused")
	}
	file, _ := http.NewRequest(http.MethodGet, "file:///etc/passwd", nil)
	if webClient(0).CheckRedirect(file, nil) == nil {
		t.Fatal("a redirect off http(s) must be refused")
	}
}

func TestWebSearchProviders(t *testing.T) {
	ctx := context.Background()
	t.Setenv("ARIA_WEB_FETCH_ALLOW_PRIVATE", "1")
	var seen []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.URL.Path)
		switch r.URL.Path {
		case "/brave":
			if r.Header.Get("X-Subscription-Token") == "bad" {
				w.WriteHeader(401)
				w.Write([]byte(`{"error":"bad token bad"}`))
				return
			}
			if r.URL.Query().Get("search_lang") != "zh-hans" || r.URL.Query().Get("count") != "3" {
				w.WriteHeader(422)
				return
			}
			json.NewEncoder(w).Encode(map[string]any{"web": map[string]any{"results": []any{
				map[string]any{"title": "<strong>茅台</strong> 年报", "url": "https://example.com/a", "description": "净利润 <b>增长</b>"},
			}}})
		case "/tavily":
			if r.Header.Get("Authorization") != "Bearer tv" {
				w.WriteHeader(401)
				return
			}
			json.NewEncoder(w).Encode(map[string]any{"results": []any{map[string]any{"title": "T", "url": "https://example.org/t", "content": "tavily text"}}})
		case "/duck":
			r.ParseForm()
			if r.FormValue("q") != "golang release" {
				w.WriteHeader(400)
				return
			}
			w.Write([]byte(`<html><body>
				<div class="result result--ad"><a class="result__a" href="https://ads.example/x">Ad</a></div>
				<div class="result results_links"><h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgo.dev%2Fdoc%2Fgo1.26&amp;rut=x">Go 1.26 <b>Release</b> Notes</a></h2>
				<a class="result__snippet" href="#">The latest Go release, version 1.26.</a></div>
				<div class="result"><a class="result__a" href="https://go.dev/blog">The Go Blog</a></div>
			</body></html>`))
		}
	}))
	defer srv.Close()
	saved := []string{braveSearchURL, tavilySearchURL, duckSearchURL}
	braveSearchURL, tavilySearchURL, duckSearchURL = srv.URL+"/brave", srv.URL+"/tavily", srv.URL+"/duck"
	defer func() { braveSearchURL, tavilySearchURL, duckSearchURL = saved[0], saved[1], saved[2] }()

	t.Setenv("BRAVE_SEARCH_API_KEY", "good")
	t.Setenv("TAVILY_API_KEY", "")
	r := webSearch(ctx, map[string]any{"query": "茅台年报", "num_results": 3})
	items, _ := r.Data["items"].([]any)
	if !r.Success || r.Data["provider"] != "brave" || len(items) != 1 {
		t.Fatal(r)
	}
	if first := items[0].(map[string]any); first["title"] != "茅台 年报" || first["snippet"] != "净利润 增长" {
		t.Fatal("markup is stripped from titles and snippets", first)
	}

	// A refused key falls through to the next provider, and is never echoed.
	t.Setenv("BRAVE_SEARCH_API_KEY", "bad")
	t.Setenv("TAVILY_API_KEY", "tv")
	if r := webSearch(ctx, map[string]any{"query": "x"}); r.Data["provider"] != "tavily" {
		t.Fatal(r)
	}
	t.Setenv("TAVILY_API_KEY", "wrong")
	t.Setenv("BRAVE_SEARCH_API_KEY", "")
	r = webSearch(ctx, map[string]any{"query": "golang release"})
	items, _ = r.Data["items"].([]any)
	if r.Data["provider"] != "duckduckgo" || len(items) != 2 {
		t.Fatal("without a working key, DuckDuckGo answers", r)
	}
	if first := items[0].(map[string]any); first["url"] != "https://go.dev/doc/go1.26" || first["title"] != "Go 1.26 Release Notes" || first["snippet"] != "The latest Go release, version 1.26." {
		t.Fatal("the redirect is unwrapped and ads skipped", first)
	}
	t.Setenv("BRAVE_SEARCH_API_KEY", "bad")
	duckSearchURL = srv.URL + "/missing"
	r = webSearch(ctx, map[string]any{"query": "x"})
	if r.Success || !strings.Contains(r.Error, "brave: HTTP 401") || !strings.Contains(r.Error, "tavily: HTTP 401") || !strings.Contains(r.Error, "duckduckgo: no results") {
		t.Fatal("each provider's failure is named", r)
	}
	if strings.Contains(r.Error, "bad token") || !strings.Contains(r.Error, "<key> token <key>") {
		t.Fatal("the key is masked wherever a provider echoes it", r.Error)
	}
	if r := webSearch(ctx, map[string]any{"query": "  "}); r.Success {
		t.Fatal("an empty query")
	}
}

func TestWebToolsAreApprovedPerHost(t *testing.T) {
	s := approvalServer(t, config{"approval_mode": "manual"}, appOptions{})
	ctx := context.Background()
	fetch := func(u string) (bool, string) {
		ok, reason, err := s.approve(ctx, "web_fetch", map[string]any{"url": u}, fileChange{})
		if err != nil {
			t.Fatal(err)
		}
		return ok, reason
	}
	if ok, reason := fetch("https://example.com/a"); ok || !strings.Contains(reason, "approval required") {
		t.Fatal("a headless run does not fetch without a grant", ok, reason)
	}
	s.grants["web_fetch:example.com"] = true
	if ok, _ := fetch("https://EXAMPLE.com/b?q=1"); !ok {
		t.Fatal("the grant covers the host")
	}
	if ok, _ := fetch("https://other.example/x?secret=1"); ok {
		t.Fatal("another host needs its own approval")
	}
	risk := approvalServer(t, config{"approval_mode": "risk", "auto_approve_level": 2}, appOptions{})
	if ok, reason, _ := risk.approve(ctx, "web_search", map[string]any{"query": "x"}, fileChange{}); !ok || !strings.Contains(reason, "auto-approved") {
		t.Fatal("risk mode at level 2 runs network reads", ok, reason)
	}
	allowed := approvalServer(t, config{}, appOptions{allowedTools: []string{"web_fetch"}})
	if ok, _, _ := allowed.approve(ctx, "web_fetch", map[string]any{"url": "https://a.example"}, fileChange{}); !ok {
		t.Fatal("--allowed-tools web_fetch allows every host")
	}
	if host := webHost("web_search", nil); host != "html.duckduckgo.com" && host != "api.search.brave.com" && host != "api.tavily.com" {
		t.Fatal(host)
	}
}
