package main

// The relay: Feishu events in over HTTP, users' machines on WebSockets.
// Behaviour follows src/aria_code/aria_relay_server.py; tests/test_relay_contract.py
// holds both implementations to it.

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/coder/websocket"
)

type config struct {
	appID, appSecret  string
	relaySecret       string
	verificationToken string
	encryptKey        string
	allowUnverified   bool
	messageTimeout    time.Duration
	feishuBase        string
}

const (
	forwardTTL       = time.Hour
	sendLimit        = 60
	eventTTL         = 8 * time.Hour // Feishu's last retry comes about six hours after the first try
	cardPressTimeout = 2500 * time.Millisecond
	registerTimeout  = 15 * time.Second
	maxFrame         = 16 << 20
	maxEventBody     = 16 << 20
)

type forward struct {
	client  string
	expires time.Time
}

type pending struct {
	client string
	answer chan json.RawMessage
}

type relay struct {
	cfg    config
	store  Store
	feishu *feishu
	log    *log.Logger

	mu          sync.Mutex
	connections map[string]*websocket.Conn // client_id → its registered socket
	pending     map[string]pending         // request id → who it was sent to
	forwarded   map[string]forward         // message_id → client it was forwarded to
	sendTimes   map[string][]time.Time     // client_id → recent allowed sends
	seenEvents  map[string]time.Time       // Feishu event id → when it may be forgotten
}

func newRelay(cfg config, store Store) *relay {
	return &relay{
		cfg: cfg, store: store,
		feishu:      newFeishu(cfg.feishuBase, cfg.appID, cfg.appSecret),
		log:         log.New(os.Stderr, "[relay] ", log.LstdFlags),
		connections: map[string]*websocket.Conn{},
		pending:     map[string]pending{},
		forwarded:   map[string]forward{},
		sendTimes:   map[string][]time.Time{},
		seenEvents:  map[string]time.Time{},
	}
}

// ── HTTP ───────────────────────────────────────────────────────────────────

func (r *relay) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	route := map[string]struct {
		method  string
		handler http.HandlerFunc
	}{
		"/status":       {http.MethodGet, r.status},
		"/feishu/event": {http.MethodPost, r.feishuEvent},
		"/ws":           {http.MethodGet, r.websocket},
	}[req.URL.Path]
	switch {
	case route.handler == nil:
		writeJSON(w, http.StatusNotFound, map[string]string{"detail": "Not Found"})
	case req.Method != route.method:
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"detail": "Method Not Allowed"})
	default:
		route.handler(w, req)
	}
}

func writeJSON(w http.ResponseWriter, code int, value any) {
	body, err := marshal(value)
	if err != nil {
		code, body = http.StatusInternalServerError, []byte(`{"detail":"Internal Server Error"}`)
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	w.Write(body)
}

func equal(a, b string) bool { return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1 }

func (r *relay) status(w http.ResponseWriter, req *http.Request) {
	ctx := req.Context()
	bindings, err := r.store.BindingCount(ctx)
	if err != nil {
		r.log.Printf("binding count: %v", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"detail": "Internal Server Error"})
		return
	}
	r.mu.Lock()
	ids := make([]string, 0, len(r.connections))
	for id := range r.connections {
		ids = append(ids, id)
	}
	r.mu.Unlock()
	body := map[string]any{
		"connected_clients":     len(ids),
		"total_bindings":        bindings,
		"store":                 r.store.Kind(),
		"feishu_app_configured": r.cfg.appID != "",
		"event_verification":    r.verificationMode(),
	}
	if os.Getenv("K_SERVICE") != "" && r.store.Kind() == "sqlite" {
		body["store_warning"] = "sqlite on Cloud Run — bindings are lost on every deploy; set RELAY_STORE=firestore"
	}
	if r.cfg.relaySecret != "" && equal(req.Header.Get("X-Relay-Secret"), r.cfg.relaySecret) {
		body["client_ids"] = ids
	}
	writeJSON(w, http.StatusOK, body)
}

func (r *relay) verificationMode() string {
	switch {
	case r.cfg.encryptKey != "":
		return "encrypt_key"
	case r.cfg.verificationToken != "":
		return "verification_token"
	case r.cfg.allowUnverified:
		return "DISABLED (unverified events allowed)"
	default:
		return "enforced (events rejected: no key configured)"
	}
}

// verify decides whether an event comes from Feishu: the request signature
// when an Encrypt Key is configured, else the payload's verification token.
// Encrypted push ({"encrypt": ...}) is not supported, as in the Python relay.
func (r *relay) verify(header http.Header, body []byte, payload map[string]any) (bool, string) {
	if r.cfg.encryptKey != "" {
		signature := header.Get("X-Lark-Signature")
		if signature == "" {
			return false, "missing X-Lark-Signature"
		}
		sum := sha256.Sum256(append([]byte(header.Get("X-Lark-Request-Timestamp")+
			header.Get("X-Lark-Request-Nonce")+r.cfg.encryptKey), body...))
		if !equal(hex.EncodeToString(sum[:]), signature) {
			return false, "signature mismatch"
		}
		return true, ""
	}
	if r.cfg.verificationToken != "" {
		token, _ := payload["token"].(string)
		if token == "" {
			token, _ = object(payload, "header")["token"].(string)
		}
		if !equal(token, r.cfg.verificationToken) {
			return false, "verification token mismatch"
		}
		return true, ""
	}
	if r.cfg.allowUnverified {
		return true, ""
	}
	return false, "relay 未配置 FEISHU_ENCRYPT_KEY 或 FEISHU_VERIFICATION_TOKEN；" +
		"未验签的事件一律拒绝（本地联调可设 RELAY_ALLOW_UNVERIFIED_EVENTS=1）"
}

func object(m map[string]any, key string) map[string]any {
	value, _ := m[key].(map[string]any)
	if value == nil {
		return map[string]any{}
	}
	return value
}

func text(m map[string]any, key string) string {
	value, _ := m[key].(string)
	return value
}

func (r *relay) feishuEvent(w http.ResponseWriter, req *http.Request) {
	body, err := io.ReadAll(io.LimitReader(req.Body, maxEventBody))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "Invalid JSON"})
		return
	}
	var payload map[string]any
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	if err := decoder.Decode(&payload); err != nil || payload == nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "Invalid JSON"})
		return
	}
	// Verify before anything else, the URL challenge included.
	if ok, reason := r.verify(req.Header, body, payload); !ok {
		r.log.Printf("rejected an unverified /feishu/event: %s", reason)
		writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "Unverified Feishu event: " + reason})
		return
	}
	if challenge, ok := payload["challenge"]; ok {
		writeJSON(w, http.StatusOK, map[string]any{"challenge": challenge})
		return
	}
	header := object(payload, "header")
	// A card button press is answered synchronously: Feishu shows the
	// response's toast and card to the presser and waits at most 3 s.
	if text(header, "event_type") == "card.action.trigger" {
		r.cardPress(w, req.Context(), payload, body)
		return
	}
	// Message events are answered at once and handled after the answer;
	// a retry of an event already taken is acknowledged and dropped.
	if !r.seenEvent(payload) {
		go r.handleMessage(payload, body)
	}
	writeJSON(w, http.StatusOK, map[string]int{"code": 0})
}

func (r *relay) seenEvent(payload map[string]any) bool {
	id := text(object(payload, "header"), "event_id")
	if id == "" {
		id = text(payload, "uuid")
	}
	if id == "" {
		return false
	}
	now := time.Now()
	r.mu.Lock()
	defer r.mu.Unlock()
	for key, expires := range r.seenEvents {
		if expires.Before(now) {
			delete(r.seenEvents, key)
		}
	}
	if _, seen := r.seenEvents[id]; seen {
		return true
	}
	r.seenEvents[id] = now.Add(eventTTL)
	return false
}

func (r *relay) cardPress(w http.ResponseWriter, ctx context.Context, payload map[string]any, raw []byte) {
	cardContext := object(object(payload, "event"), "context")
	messageID := text(cardContext, "open_message_id")
	origin := ""
	if messageID != "" {
		var err error
		if origin, err = r.store.CardOrigin(ctx, messageID); err != nil {
			r.log.Printf("card origin: %v", err)
		}
	}
	if origin == "" {
		writeJSON(w, http.StatusOK, map[string]any{"toast": map[string]string{"type": "error", "content": "这张卡片已失效。"}})
		return
	}
	if result, routed := r.routeToClient(origin, raw, cardPressTimeout); routed {
		var answer map[string]json.RawMessage
		if json.Unmarshal(result, &answer) == nil {
			if _, toast := answer["toast"]; toast {
				writeRaw(w, result)
				return
			}
			if _, card := answer["card"]; card {
				writeRaw(w, result)
				return
			}
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"toast": map[string]string{"type": "error", "content": "Aria 本机未及时响应，请稍后再试。"}})
}

func writeRaw(w http.ResponseWriter, body []byte) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	w.Write(body)
}

// ── message events ─────────────────────────────────────────────────────────

func (r *relay) handleMessage(payload map[string]any, raw []byte) {
	defer func() {
		if failure := recover(); failure != nil {
			r.log.Printf("handling a Feishu event failed: %v", failure)
		}
	}()
	if err := r.deliver(context.Background(), payload, raw); err != nil {
		r.log.Printf("handling a Feishu event failed: %v", err)
	}
}

func (r *relay) deliver(ctx context.Context, payload map[string]any, raw []byte) error {
	event := object(payload, "event")
	message := object(event, "message")
	user := text(object(object(event, "sender"), "sender_id"), "open_id")
	messageID := text(message, "message_id")
	if user == "" {
		return nil
	}
	if text(message, "message_type") == "text" {
		var content struct {
			Text string `json:"text"`
		}
		_ = json.Unmarshal([]byte(text(message, "content")), &content)
		said := strings.TrimSpace(content.Text)
		upper := strings.ToUpper(said)
		if strings.HasPrefix(upper, "/BIND ") || strings.HasPrefix(upper, "ARIA-BIND-") {
			code := strings.ReplaceAll(strings.TrimSpace(strings.ReplaceAll(upper, "/BIND ", "")), "ARIA-BIND-", "")
			// Only the code generated and shown on the machine binds it.
			client, err := r.clientForBindCode(ctx, code)
			if err != nil {
				return err
			}
			if client == "" {
				return r.feishu.sendText(ctx, user, "❌ 绑定码无效。请在你电脑上运行 aria-code 的配置向导，使用它显示的绑定码。")
			}
			if err := r.store.BindUser(ctx, user, client); err != nil {
				return err
			}
			return r.feishu.sendText(ctx, user, "✅ 绑定成功！你的 Aria 实例已连接。\n现在可以直接发消息与你的 Aria 交互了。")
		}
	}
	client, err := r.store.ClientForUser(ctx, user)
	if err != nil {
		return err
	}
	routed := false
	if client != "" {
		if err := r.rememberForward(ctx, client, messageID, text(message, "chat_id")); err != nil {
			return err
		}
		_, routed = r.routeToClient(client, raw, r.cfg.messageTimeout)
	}
	if routed {
		return nil
	}
	if client == "" {
		return r.feishu.sendText(ctx, user, "👋 你好！要开始使用 Aria，请：\n"+
			"1. 在你的电脑上安装 Aria Code\n"+
			"2. 运行 `python3 setup_wizard.py` 完成配置\n"+
			"3. 发送绑定码绑定你的账户")
	}
	return r.feishu.replyCard(ctx, messageID,
		"⚠️ Aria 本机未连接。请确保你的电脑上 `aria_relay_client.py` 正在运行。", "yellow")
}

func normaliseBindCode(code string) string {
	var out strings.Builder
	for _, ch := range strings.ToUpper(code) {
		if unicode.IsLetter(ch) || unicode.IsNumber(ch) {
			out.WriteRune(ch)
		}
	}
	return out.String()
}

func (r *relay) clientForBindCode(ctx context.Context, code string) (string, error) {
	normalised := normaliseBindCode(code)
	if utf8.RuneCountInString(normalised) < 10 {
		return "", nil
	}
	return r.store.ClientForBindCode(ctx, sha256Hex(normalised))
}

// routeToClient sends an event to one connected client and waits for its
// answer. routed is false when the client is not connected; a timeout is
// routed, with the error the Python relay answers.
func (r *relay) routeToClient(client string, payload []byte, timeout time.Duration) (json.RawMessage, bool) {
	r.mu.Lock()
	conn := r.connections[client]
	if conn == nil {
		r.mu.Unlock()
		return nil, false
	}
	id := requestID()
	answer := make(chan json.RawMessage, 1)
	r.pending[id] = pending{client: client, answer: answer}
	r.mu.Unlock()
	defer func() {
		r.mu.Lock()
		delete(r.pending, id)
		r.mu.Unlock()
	}()

	frame, err := marshal(map[string]any{"type": "message", "id": id, "payload": json.RawMessage(payload)})
	if err != nil {
		return nil, false
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	if err := conn.Write(ctx, websocket.MessageText, frame); err != nil {
		r.log.Printf("forwarding to %s failed: %v", client, err)
		return nil, false
	}
	select {
	case result := <-answer:
		return result, true
	case <-ctx.Done():
		r.log.Printf("timeout waiting for response from client_id=%s", client)
		timedOut, _ := marshal(map[string]string{"error": "Aria 本机响应超时，请检查 aria_relay_client 是否在线"})
		return timedOut, true
	}
}

// resolveResponse hands a client's answer to the request waiting for it, if
// the request was sent to that client.
func (r *relay) resolveResponse(client, id string, result json.RawMessage) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	waiting, ok := r.pending[id]
	if !ok || waiting.client != client {
		return false
	}
	delete(r.pending, id)
	if result == nil {
		result = json.RawMessage("null")
	}
	waiting.answer <- result
	return true
}

func requestID() string {
	var b [5]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	return "req_" + hex.EncodeToString(b[:])
}

func (r *relay) rememberForward(ctx context.Context, client, messageID, chatID string) error {
	now := time.Now()
	r.mu.Lock()
	for id, entry := range r.forwarded {
		if entry.expires.Before(now) {
			delete(r.forwarded, id)
		}
	}
	if messageID != "" {
		r.forwarded[messageID] = forward{client: client, expires: now.Add(forwardTTL)}
	}
	r.mu.Unlock()
	if chatID != "" {
		return r.store.RememberChat(ctx, client, chatID)
	}
	return nil
}
