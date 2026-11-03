package main

// web_fetch and web_search for the Go agent, as the Python engine had them
// (system_tools.tool_web_fetch, local_finance_tools._web_search).
//
// A fetch can carry data out of the machine in its URL, so both tools are
// approved per host (see server.approve): the first call to a host asks,
// the session grant then covers that host. Fetches never reach loopback,
// private or link-local addresses (checked on the name, every redirect and
// the address actually dialled) unless ARIA_WEB_FETCH_ALLOW_PRIVATE=1.
// Search tries Brave (BRAVE_SEARCH_API_KEY), then Tavily (TAVILY_API_KEY),
// then DuckDuckGo's HTML page, which needs no key.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	"golang.org/x/net/html"
)

const (
	maxFetchBytes   = 5 << 20
	defaultFetchOut = 8000
	maxFetchOut     = 20000
)

var (
	braveSearchURL  = "https://api.search.brave.com/res/v1/web/search"
	tavilySearchURL = "https://api.tavily.com/search"
	duckSearchURL   = "https://html.duckduckgo.com/html/"
)

var webDefinitions = []definition{
	{"web_fetch", "Fetch a public web page, JSON document or PDF over http(s) and return its readable text. The first request to each host asks for approval.", map[string]any{
		"url":       textParam("http(s) URL"),
		"max_chars": map[string]any{"type": "integer", "minimum": 500, "maximum": maxFetchOut, "description": "Most characters to return (default 8000)."},
	}, []string{"url"}, false},
	{"web_search", "Search the web; returns titles, URLs and snippets to fetch with web_fetch. Cite what you use.", map[string]any{
		"query":       textParam("Search query"),
		"num_results": map[string]any{"type": "integer", "minimum": 1, "maximum": 10, "description": "Default 5."},
	}, []string{"query"}, false},
}

func isWebTool(name string) bool { return name == "web_fetch" || name == "web_search" }

// webHost is the host a web tool call reaches, for its per-host approval.
func webHost(name string, params map[string]any) string {
	if name == "web_search" {
		switch {
		case envKey("BRAVE_SEARCH_API_KEY") != "":
			return "api.search.brave.com"
		case envKey("TAVILY_API_KEY") != "":
			return "api.tavily.com"
		}
		return "html.duckduckgo.com"
	}
	raw, _ := params["url"].(string)
	if u, err := fetchURL(raw); err == nil {
		return strings.ToLower(u.Hostname())
	}
	return "?"
}

func fetchURL(raw string) (*url.URL, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, errors.New("url is required")
	}
	if !strings.Contains(raw, "://") {
		raw = "https://" + raw
	}
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" {
		return nil, errors.New("url must be an http(s) address")
	}
	if u.User != nil {
		return nil, errors.New("url must not carry a user name or password")
	}
	return u, nil
}

func privateIP(ip net.IP) bool {
	return ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() ||
		ip.IsUnspecified() || ip.IsMulticast() || ip.IsInterfaceLocalMulticast() ||
		// 100.64.0.0/10 carrier-grade NAT and the cloud metadata range are private in practice.
		(ip.To4() != nil && ip.To4()[0] == 100 && ip.To4()[1]&0xC0 == 64)
}

func allowPrivate() bool { return os.Getenv("ARIA_WEB_FETCH_ALLOW_PRIVATE") == "1" }

// checkHost refuses a name that resolves to a private address.
func checkHost(ctx context.Context, host string) error {
	if allowPrivate() {
		return nil
	}
	if ip := net.ParseIP(host); ip != nil {
		if privateIP(ip) {
			return fmt.Errorf("%s is a private address; set ARIA_WEB_FETCH_ALLOW_PRIVATE=1 to allow local fetches", host)
		}
		return nil
	}
	if strings.EqualFold(host, "localhost") || strings.HasSuffix(strings.ToLower(host), ".localhost") {
		return fmt.Errorf("%s is this machine; set ARIA_WEB_FETCH_ALLOW_PRIVATE=1 to allow local fetches", host)
	}
	addrs, err := net.DefaultResolver.LookupIPAddr(ctx, host)
	if err != nil {
		// Behind a proxy the name may only resolve there; the dial check still applies.
		return nil
	}
	for _, a := range addrs {
		if privateIP(a.IP) {
			return fmt.Errorf("%s resolves to a private address (%s)", host, a.IP)
		}
	}
	return nil
}

// proxyAddrs are the configured proxies, which may legitimately be local.
func proxyAddrs() map[string]bool {
	out := map[string]bool{}
	for _, key := range []string{"HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"} {
		if v := os.Getenv(key); v != "" {
			if u, err := url.Parse(v); err == nil && u.Host != "" {
				host, port := u.Hostname(), u.Port()
				if port == "" {
					port = map[string]string{"https": "443"}[u.Scheme]
					if port == "" {
						port = "80"
					}
				}
				if addrs, err := net.LookupIP(host); err == nil {
					for _, ip := range addrs {
						out[net.JoinHostPort(ip.String(), port)] = true
					}
				}
				out[net.JoinHostPort(host, port)] = true
			}
		}
	}
	return out
}

// webClient refuses private addresses at dial time too, except the proxy.
func webClient(timeout time.Duration) *http.Client {
	proxies := proxyAddrs()
	dialer := &net.Dialer{Timeout: 10 * time.Second, Control: func(_, address string, _ syscall.RawConn) error {
		if allowPrivate() || proxies[address] {
			return nil
		}
		host, _, _ := net.SplitHostPort(address)
		if ip := net.ParseIP(host); ip != nil && privateIP(ip) {
			return fmt.Errorf("refusing to connect to private address %s", host)
		}
		return nil
	}}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DialContext = dialer.DialContext
	return &http.Client{Timeout: timeout, Transport: transport, CheckRedirect: func(req *http.Request, via []*http.Request) error {
		if len(via) >= 5 {
			return errors.New("too many redirects")
		}
		if req.URL.Scheme != "http" && req.URL.Scheme != "https" {
			return errors.New("redirect to a non-http(s) address")
		}
		return checkHost(req.Context(), req.URL.Hostname())
	}}
}

var githubBlob = regexp.MustCompile(`^https://github\.com/([^/]+/[^/]+)/blob/([^?#]+)`)

func userAgent(host string) string {
	if host == "sec.gov" || strings.HasSuffix(host, ".sec.gov") {
		// SEC asks scripts for a declarative agent with a contact.
		if contact := envKey("ARIA_SEC_CONTACT"); contact != "" {
			return "aria-code research client (" + contact + ")"
		}
		return "aria-code research client"
	}
	return "Mozilla/5.0 (compatible; Aria/" + version + "; +https://github.com/artheras/aria-code)"
}

// htmlText is the visible text of a page, without scripts, styles or markup.
func htmlText(body []byte) (title, text string) {
	z := html.NewTokenizer(bytes.NewReader(body))
	var b strings.Builder
	skip := 0
	inTitle := false
	for {
		switch z.Next() {
		case html.ErrorToken:
			out := regexp.MustCompile(`[ \t\r\f\v]+`).ReplaceAllString(b.String(), " ")
			out = regexp.MustCompile(`\s*\n\s*(\n\s*)+`).ReplaceAllString(out, "\n\n")
			return strings.TrimSpace(title), strings.TrimSpace(out)
		case html.StartTagToken, html.SelfClosingTagToken:
			name, _ := z.TagName()
			switch string(name) {
			case "script", "style", "noscript", "svg", "template":
				skip++
			case "title":
				inTitle = true
			case "br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "table":
				b.WriteString("\n")
			}
		case html.EndTagToken:
			name, _ := z.TagName()
			switch string(name) {
			case "script", "style", "noscript", "svg", "template":
				if skip > 0 {
					skip--
				}
			case "title":
				inTitle = false
			case "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6":
				b.WriteString("\n")
			}
		case html.TextToken:
			if skip > 0 {
				continue
			}
			t := string(z.Text())
			if inTitle {
				title += t
				continue
			}
			b.WriteString(t)
		}
	}
}

func clipRunes(text string, limit int) (string, bool) {
	if utf8.RuneCountInString(text) <= limit {
		return text, false
	}
	return string([]rune(text)[:limit]), true
}

func webFetch(ctx context.Context, args map[string]any) toolResult {
	raw, _ := args["url"].(string)
	u, err := fetchURL(raw)
	if err != nil {
		return failed(err)
	}
	if m := githubBlob.FindStringSubmatch(u.String()); m != nil {
		u, _ = url.Parse("https://raw.githubusercontent.com/" + m[1] + "/" + m[2])
	}
	limit := defaultFetchOut
	if v, ok := chartNumber(args["max_chars"]); ok && v >= 500 {
		limit = min(int(v), maxFetchOut)
	}
	if err := checkHost(ctx, u.Hostname()); err != nil {
		return failed(err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return failed(err)
	}
	req.Header.Set("User-Agent", userAgent(strings.ToLower(u.Hostname())))
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,application/pdf;q=0.8,*/*;q=0.5")
	resp, err := webClient(30 * time.Second).Do(req)
	if err != nil {
		return failed(fmt.Errorf("web_fetch: %w", err))
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxFetchBytes+1))
	if err != nil {
		return failed(fmt.Errorf("web_fetch: %w", err))
	}
	truncatedBody := len(body) > maxFetchBytes
	if truncatedBody {
		body = body[:maxFetchBytes]
	}
	final := resp.Request.URL.String()
	if resp.StatusCode >= 400 {
		snippet, _ := clipRunes(strings.TrimSpace(string(body)), 300)
		return failed(fmt.Errorf("web_fetch: %s returned HTTP %d: %s", final, resp.StatusCode, snippet))
	}
	contentType := resp.Header.Get("Content-Type")
	kind := strings.ToLower(contentType)
	data := map[string]any{"url": final, "status": resp.StatusCode, "content_type": contentType}
	var text string
	switch {
	case strings.Contains(kind, "pdf") || bytes.HasPrefix(body, []byte("%PDF-")):
		if truncatedBody {
			return failed(errors.New("web_fetch: the PDF is larger than 5 MiB"))
		}
		extracted, err := pdfText(ctx, body)
		if err != nil {
			return failed(fmt.Errorf("web_fetch: could not read the PDF: %v", err))
		}
		text = string(extracted)
	case strings.Contains(kind, "html") || strings.Contains(kind, "xml") && bytes.Contains(bytes.ToLower(body[:min(len(body), 512)]), []byte("<html")):
		title, t := htmlText(body)
		if title != "" {
			data["title"] = title
		}
		text = t
	case strings.HasPrefix(kind, "text/") || strings.Contains(kind, "json") || strings.Contains(kind, "xml") || kind == "" && utf8.Valid(body):
		text = string(body)
	default:
		return failed(fmt.Errorf("web_fetch: %s is %s, not text, HTML, JSON or PDF", final, contentType))
	}
	if !utf8.ValidString(text) {
		text = strings.ToValidUTF8(text, "�")
	}
	out, cut := clipRunes(text, limit)
	data["text"], data["length"] = out, utf8.RuneCountInString(text)
	if cut || truncatedBody {
		data["truncated"] = true
	}
	return toolResult{Success: true, Data: data}
}

type searchHit struct {
	Title   string `json:"title"`
	URL     string `json:"url"`
	Snippet string `json:"snippet"`
}

func webSearch(ctx context.Context, args map[string]any) toolResult {
	query, _ := args["query"].(string)
	query = strings.TrimSpace(query)
	if query == "" {
		return failed(errors.New("query is required"))
	}
	num := 5
	if v, ok := chartNumber(args["num_results"]); ok && v >= 1 {
		num = min(int(v), 10)
	}
	client := webClient(20 * time.Second)
	var failures []string
	type provider struct {
		name string
		key  string
		run  func(string) ([]searchHit, error)
	}
	for _, p := range []provider{
		{"brave", envKey("BRAVE_SEARCH_API_KEY"), func(key string) ([]searchHit, error) { return braveSearch(ctx, client, key, query, num) }},
		{"tavily", envKey("TAVILY_API_KEY"), func(key string) ([]searchHit, error) { return tavilySearch(ctx, client, key, query, num) }},
		{"duckduckgo", "-", func(string) ([]searchHit, error) { return duckSearch(ctx, client, query, num) }},
	} {
		if p.key == "" {
			continue
		}
		hits, err := p.run(p.key)
		if err != nil {
			failures = append(failures, p.name+": "+strings.ReplaceAll(err.Error(), p.key, "<key>"))
			continue
		}
		if len(hits) == 0 {
			failures = append(failures, p.name+": no results")
			continue
		}
		items := make([]any, 0, len(hits))
		for _, h := range hits {
			items = append(items, map[string]any{"title": h.Title, "url": h.URL, "snippet": h.Snippet})
		}
		return toolResult{Success: true, Data: map[string]any{"query": query, "provider": p.name, "items": items}}
	}
	return failed(fmt.Errorf("web_search found nothing (%s)", strings.Join(failures, "; ")))
}

func getJSON(client *http.Client, req *http.Request, out any) error {
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if resp.StatusCode >= 400 {
		snippet, _ := clipRunes(strings.TrimSpace(string(body)), 200)
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, snippet)
	}
	return json.Unmarshal(body, out)
}

func braveSearch(ctx context.Context, client *http.Client, key, query string, num int) ([]searchHit, error) {
	q := url.Values{"q": {query}, "count": {fmt.Sprint(num)}, "safesearch": {"moderate"}}
	for _, r := range query {
		if r >= 0x4E00 && r <= 0x9FFF {
			q.Set("search_lang", "zh-hans") // "zh" is rejected (HTTP 422)
			break
		}
	}
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, braveSearchURL+"?"+q.Encode(), nil)
	req.Header.Set("Accept", "application/json")
	req.Header.Set("X-Subscription-Token", key)
	var reply struct {
		Web struct {
			Results []struct {
				Title       string `json:"title"`
				URL         string `json:"url"`
				Description string `json:"description"`
			} `json:"results"`
		} `json:"web"`
	}
	if err := getJSON(client, req, &reply); err != nil {
		return nil, err
	}
	var hits []searchHit
	for _, r := range reply.Web.Results {
		_, title := htmlText([]byte(r.Title))
		_, snippet := htmlText([]byte(r.Description))
		hits = append(hits, searchHit{title, r.URL, snippet})
	}
	return hits[:min(len(hits), num)], nil
}

func tavilySearch(ctx context.Context, client *http.Client, key, query string, num int) ([]searchHit, error) {
	body, _ := json.Marshal(map[string]any{"query": query, "max_results": num, "search_depth": "basic"})
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, tavilySearchURL, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+key)
	var reply struct {
		Results []struct {
			Title   string `json:"title"`
			URL     string `json:"url"`
			Content string `json:"content"`
		} `json:"results"`
	}
	if err := getJSON(client, req, &reply); err != nil {
		return nil, err
	}
	var hits []searchHit
	for _, r := range reply.Results {
		snippet, _ := clipRunes(r.Content, 400)
		hits = append(hits, searchHit{r.Title, r.URL, snippet})
	}
	return hits[:min(len(hits), num)], nil
}

// duckSearch reads DuckDuckGo's no-JavaScript results page.
func duckSearch(ctx context.Context, client *http.Client, query string, num int) ([]searchHit, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, duckSearchURL, strings.NewReader(url.Values{"q": {query}}.Encode()))
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("User-Agent", userAgent(""))
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	doc, err := html.Parse(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	var hits []searchHit
	var walk func(*html.Node)
	hasClass := func(n *html.Node, class string) bool {
		for _, a := range n.Attr {
			if a.Key == "class" && strings.Contains(" "+a.Val+" ", " "+class+" ") {
				return true
			}
		}
		return false
	}
	textOf := func(n *html.Node) string {
		var b strings.Builder
		var rec func(*html.Node)
		rec = func(n *html.Node) {
			if n.Type == html.TextNode {
				b.WriteString(n.Data)
			}
			for c := n.FirstChild; c != nil; c = c.NextSibling {
				rec(c)
			}
		}
		rec(n)
		return strings.Join(strings.Fields(b.String()), " ")
	}
	walk = func(n *html.Node) {
		if len(hits) >= num {
			return
		}
		if n.Type == html.ElementNode && n.Data == "div" && hasClass(n, "result") && !hasClass(n, "result--ad") {
			var hit searchHit
			var find func(*html.Node)
			find = func(m *html.Node) {
				if m.Type == html.ElementNode && m.Data == "a" && hasClass(m, "result__a") {
					hit.Title = textOf(m)
					for _, a := range m.Attr {
						if a.Key == "href" {
							hit.URL = duckTarget(a.Val)
						}
					}
				}
				if m.Type == html.ElementNode && hasClass(m, "result__snippet") {
					hit.Snippet = textOf(m)
				}
				for c := m.FirstChild; c != nil; c = c.NextSibling {
					find(c)
				}
			}
			find(n)
			if hit.URL != "" && hit.Title != "" {
				hits = append(hits, hit)
			}
			return
		}
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			walk(c)
		}
	}
	walk(doc)
	return hits, nil
}

// duckTarget unwraps DuckDuckGo's /l/?uddg=<url> redirect links.
func duckTarget(href string) string {
	if strings.HasPrefix(href, "//") {
		href = "https:" + href
	}
	u, err := url.Parse(href)
	if err != nil {
		return ""
	}
	if target := u.Query().Get("uddg"); target != "" {
		return target
	}
	if u.Scheme == "http" || u.Scheme == "https" {
		return u.String()
	}
	return ""
}
