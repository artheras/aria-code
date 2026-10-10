package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"time"
	"unicode/utf8"

	"github.com/coder/websocket"
)

type frame struct {
	Type     string          `json:"type"`
	ID       string          `json:"id"`
	ClientID string          `json:"client_id"`
	Token    string          `json:"token"`
	BindCode string          `json:"bind_code"`
	Secret   string          `json:"secret"`
	Result   json.RawMessage `json:"result"`
	// send requests
	Op            string          `json:"op"`
	Target        any             `json:"target"`
	MsgType       any             `json:"msg_type"`
	Content       json.RawMessage `json:"content"`
	ReceiveIDType *string         `json:"receive_id_type"`
}

func (r *relay) websocket(w http.ResponseWriter, req *http.Request) {
	// Any origin, no subprotocol: the clients are Aria installations, not browsers.
	conn, err := websocket.Accept(w, req, &websocket.AcceptOptions{InsecureSkipVerify: true})
	if err != nil {
		return
	}
	conn.SetReadLimit(maxFrame)
	ctx := context.Background()
	client := ""
	defer func() {
		// A reconnect registers a new socket under the same client_id before
		// the old one is noticed as gone, and a refused registration never
		// registered at all: neither may remove the live connection.
		if client != "" {
			r.mu.Lock()
			if r.connections[client] == conn {
				delete(r.connections, client)
				r.log.Printf("client disconnected: %s", client)
			}
			r.mu.Unlock()
		}
		conn.CloseNow()
	}()

	registerCtx, cancel := context.WithTimeout(ctx, registerTimeout)
	kind, data, err := conn.Read(registerCtx)
	cancel()
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			r.log.Printf("register timeout for new connection")
		}
		return
	}
	var hello frame
	if kind != websocket.MessageText || json.Unmarshal(data, &hello) != nil {
		return
	}
	refuse := func(reason string) {
		r.send(ctx, conn, map[string]any{"ok": false, "reason": reason})
		conn.Close(websocket.StatusNormalClosure, "")
	}
	switch {
	case hello.Type != "register":
		refuse("first message must be register")
		return
	case hello.ClientID == "":
		refuse("client_id required")
		return
	case r.cfg.relaySecret != "" && !equal(hello.Secret, r.cfg.relaySecret):
		refuse("invalid secret")
		return
	}
	if reason, err := r.admit(ctx, hello.ClientID, hello.Token, hello.BindCode); err != nil || reason != "" {
		if err != nil {
			r.log.Printf("admitting %s: %v", hello.ClientID, err)
			reason = "relay store unavailable"
		}
		r.log.Printf("refused registration for %s: %s", hello.ClientID, reason)
		refuse(reason)
		return
	}
	client = hello.ClientID
	r.mu.Lock()
	r.connections[client] = conn
	r.mu.Unlock()
	r.send(ctx, conn, map[string]any{"ok": true, "client_id": client})
	r.log.Printf("client connected: %s", client)

	for {
		kind, data, err := conn.Read(ctx)
		if err != nil {
			return
		}
		var message frame
		if kind != websocket.MessageText || json.Unmarshal(data, &message) != nil {
			continue
		}
		switch message.Type {
		case "send":
			go func(request frame) {
				result := r.sendForClient(ctx, client, request)
				r.send(ctx, conn, map[string]any{"type": "send_result", "id": request.ID, "result": result})
			}(message)
		case "response":
			r.resolveResponse(client, message.ID, message.Result)
		}
	}
}

func (r *relay) send(ctx context.Context, conn *websocket.Conn, value any) {
	data, err := marshal(value)
	if err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if err := conn.Write(ctx, websocket.MessageText, data); err != nil {
		r.log.Printf("websocket write: %v", err)
	}
}

// admit returns "" when this registration may proceed, otherwise why not.
// Trust on first use: the first registration of a client_id records hashes
// of its token and bind code; later ones must present the same token.
func (r *relay) admit(ctx context.Context, client, token, bindCode string) (string, error) {
	if utf8.RuneCountInString(token) < 32 {
		return "this Aria version is too old for the relay; upgrade aria-code", nil
	}
	normalised := normaliseBindCode(bindCode)
	if utf8.RuneCountInString(normalised) < 10 {
		return "bind code missing; upgrade aria-code", nil
	}
	codeHash, tokenHash := sha256Hex(normalised), sha256Hex(token)
	known, err := r.store.Credentials(ctx, client)
	if err != nil {
		return "", err
	}
	if known == nil {
		claimed, err := r.store.ClaimCredentials(ctx, client, tokenHash, codeHash)
		if err != nil || claimed {
			return "", err
		}
		if known, err = r.store.Credentials(ctx, client); err != nil { // lost a race for the same id
			return "", err
		}
	}
	if known == nil || !equal(known.TokenHash, tokenHash) {
		return "client_id is registered to another installation", nil
	}
	if known.BindCodeHash != codeHash { // the machine rotated its bind code
		return "", r.store.SetBindCode(ctx, client, codeHash)
	}
	return "", nil
}

// ── sending on a client's behalf ───────────────────────────────────────────
//
// A send is accepted only where the client has standing: a reply to a message
// the relay forwarded to it within the hour, or a send to a chat its bound
// user spoke to the bot in; at most sendLimit a minute; text or interactive,
// content at most 30,000 characters.

func (r *relay) maySend(ctx context.Context, client string, request frame) (string, error) {
	msgType, _ := request.MsgType.(string)
	if msgType != "text" && msgType != "interactive" {
		return "msg_type must be text or interactive", nil
	}
	if utf8.RuneCountInString(contentText(request.Content)) > 30_000 {
		return "content too large", nil
	}
	target := targetText(request.Target)
	switch request.Op {
	case "reply":
		r.mu.Lock()
		owner, ok := r.forwarded[target]
		r.mu.Unlock()
		if !ok || owner.client != client || owner.expires.Before(time.Now()) {
			return "not a message forwarded to this client", nil
		}
	case "send":
		if request.ReceiveIDType != nil && *request.ReceiveIDType != "chat_id" {
			return "send is only to chats", nil
		}
		has, err := r.store.HasChat(ctx, client, target)
		if err != nil {
			return "", err
		}
		if !has {
			return "this client has no conversation in that chat", nil
		}
	default:
		return "op must be reply or send", nil
	}
	now := time.Now()
	r.mu.Lock()
	defer r.mu.Unlock()
	// Forget clients with no send in the last minute, so the table holds only
	// recent senders rather than every client that ever sent.
	for id, times := range r.sendTimes {
		if len(times) == 0 || !times[len(times)-1].After(now.Add(-time.Minute)) {
			delete(r.sendTimes, id)
		}
	}
	recent := r.sendTimes[client][:0:0]
	for _, at := range r.sendTimes[client] {
		if at.After(now.Add(-time.Minute)) {
			recent = append(recent, at)
		}
	}
	if len(recent) >= sendLimit {
		r.sendTimes[client] = recent
		return "rate limit", nil
	}
	r.sendTimes[client] = append(recent, now)
	return "", nil
}

// contentText is Python's str(content): a JSON string as itself, anything
// else as its JSON text, nothing as "".
func contentText(raw json.RawMessage) string {
	if len(raw) == 0 || string(raw) == "null" {
		return ""
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	return string(raw)
}

func targetText(target any) string {
	switch value := target.(type) {
	case nil:
		return ""
	case string:
		return value
	default:
		data, _ := json.Marshal(value)
		return string(data)
	}
}

func (r *relay) sendForClient(ctx context.Context, client string, request frame) map[string]any {
	refused, err := r.maySend(ctx, client, request)
	if err != nil {
		r.log.Printf("send check for %s: %v", client, err)
		refused = "relay store unavailable"
	}
	if refused != "" {
		r.log.Printf("refused send for %s: %s", client, refused)
		return map[string]any{"code": -1, "msg": "relay refused: " + refused}
	}
	target := targetText(request.Target)
	body := map[string]any{"msg_type": request.MsgType, "content": request.Content}
	path := "/im/v1/messages/" + target + "/reply"
	if request.Op == "send" {
		path = "/im/v1/messages?" + url.Values{"receive_id_type": {"chat_id"}}.Encode()
		body["receive_id"] = request.Target
	}
	var answer struct {
		Code any    `json:"code"`
		Msg  string `json:"msg"`
		Data struct {
			MessageID string `json:"message_id"`
		} `json:"data"`
	}
	token, err := r.feishu.tenantToken(ctx)
	if err == nil {
		_, err = r.feishu.call(ctx, path, token, body, 15*time.Second, &answer)
	}
	if err != nil {
		r.log.Printf("send for %s failed: %v", client, err)
		return map[string]any{"code": -1, "msg": "relay could not reach Feishu"}
	}
	data := map[string]any{}
	if answer.Data.MessageID != "" {
		data["message_id"] = answer.Data.MessageID
	}
	if code, ok := answer.Code.(float64); ok && code == 0 && request.MsgType == "interactive" && answer.Data.MessageID != "" {
		if err := r.store.RememberCard(ctx, answer.Data.MessageID, client); err != nil {
			r.log.Printf("remember card: %v", err)
		}
	}
	return map[string]any{"code": answer.Code, "msg": answer.Msg, "data": data}
}
