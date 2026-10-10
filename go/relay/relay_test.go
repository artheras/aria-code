package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func testRelay(t *testing.T, cfg config) *relay {
	store, err := openSQLite(filepath.Join(t.TempDir(), "relay.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.db.Close() })
	if cfg.messageTimeout == 0 {
		cfg.messageTimeout = time.Second
	}
	return newRelay(cfg, store)
}

func TestOnlyTheClientARequestWasSentToMayAnswer(t *testing.T) {
	r := testRelay(t, config{})
	answer := make(chan json.RawMessage, 1)
	r.pending["req_1"] = pending{client: "aria-a", answer: answer}
	if r.resolveResponse("aria-b", "req_1", json.RawMessage(`{"toast":"forged"}`)) {
		t.Fatal("another client answered")
	}
	if !r.resolveResponse("aria-a", "req_1", json.RawMessage(`{"ok":true}`)) {
		t.Fatal("the owner could not answer")
	}
	if got := string(<-answer); got != `{"ok":true}` {
		t.Fatalf("answer = %s", got)
	}
	if r.resolveResponse("aria-a", "req_1", json.RawMessage(`{}`)) {
		t.Fatal("answered twice")
	}
}

func send(op, target, msgType string) frame {
	return frame{Op: op, Target: target, MsgType: msgType, Content: json.RawMessage(`"{}"`)}
}

func TestWhatAClientMaySend(t *testing.T) {
	ctx := context.Background()
	r := testRelay(t, config{})
	if err := r.rememberForward(ctx, "aria-a", "om_1", "oc_team"); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		client  string
		request frame
		refused string
	}{
		{"aria-a", send("reply", "om_1", "text"), ""},
		{"aria-b", send("reply", "om_1", "text"), "not a message forwarded to this client"},
		{"aria-a", send("send", "oc_team", "interactive"), ""},
		{"aria-a", send("send", "oc_other", "text"), "this client has no conversation in that chat"},
		{"aria-a", send("reply", "om_1", "file"), "msg_type must be text or interactive"},
		{"aria-a", send("delete", "om_1", "text"), "op must be reply or send"},
	}
	for _, c := range cases {
		got, err := r.maySend(ctx, c.client, c.request)
		if err != nil || got != c.refused {
			t.Errorf("%s %+v: got %q (%v), want %q", c.client, c.request, got, err, c.refused)
		}
	}
	openID := "open_id"
	toUser := send("send", "oc_team", "text")
	toUser.ReceiveIDType = &openID
	if got, _ := r.maySend(ctx, "aria-a", toUser); got != "send is only to chats" {
		t.Errorf("send to a user: %q", got)
	}
	big := send("reply", "om_1", "text")
	big.Content, _ = json.Marshal(strings.Repeat("字", 30_001))
	if got, _ := r.maySend(ctx, "aria-a", big); got != "content too large" {
		t.Errorf("large content: %q", got)
	}
}

func TestSendsAreRateLimitedAndOnlyAllowedOnesCount(t *testing.T) {
	ctx := context.Background()
	r := testRelay(t, config{})
	r.rememberForward(ctx, "aria-a", "om_1", "")
	for i := 0; i < 5; i++ {
		r.maySend(ctx, "aria-a", send("reply", "om_never", "text")) // refused, not counted
	}
	for i := 0; i < sendLimit; i++ {
		if got, _ := r.maySend(ctx, "aria-a", send("reply", "om_1", "text")); got != "" {
			t.Fatalf("send %d refused: %s", i, got)
		}
	}
	if got, _ := r.maySend(ctx, "aria-a", send("reply", "om_1", "text")); got != "rate limit" {
		t.Fatalf("send over the limit: %q", got)
	}
}

func TestEventVerification(t *testing.T) {
	body := []byte(`{"challenge":"c"}`)
	payload := map[string]any{"challenge": "c", "header": map[string]any{"token": "from-header"}}
	signed := func(key string) http.Header {
		sum := sha256.Sum256(append([]byte("1700000000nonce"+key), body...))
		return http.Header{"X-Lark-Request-Timestamp": {"1700000000"}, "X-Lark-Request-Nonce": {"nonce"},
			"X-Lark-Signature": {hex.EncodeToString(sum[:])}}
	}
	cases := []struct {
		name   string
		cfg    config
		header http.Header
		ok     bool
	}{
		{"signature", config{encryptKey: "ek"}, signed("ek"), true},
		{"wrong signature", config{encryptKey: "ek"}, signed("other"), false},
		{"missing signature", config{encryptKey: "ek", verificationToken: "from-header"}, http.Header{}, false},
		{"token in header", config{verificationToken: "from-header"}, http.Header{}, true},
		{"wrong token", config{verificationToken: "tok"}, http.Header{}, false},
		{"unverified allowed", config{allowUnverified: true}, http.Header{}, true},
		{"nothing configured", config{}, http.Header{}, false},
	}
	for _, c := range cases {
		r := testRelay(t, c.cfg)
		if ok, reason := r.verify(c.header, body, payload); ok != c.ok {
			t.Errorf("%s: ok=%v (%s)", c.name, ok, reason)
		}
	}
}

func TestBindCodesAreNormalised(t *testing.T) {
	for in, want := range map[string]string{
		"abcd-efgh-jklm": "ABCDEFGHJKLM",
		" ab cd ":        "ABCD",
		"aria_bind.123":  "ARIABIND123",
	} {
		if got := normaliseBindCode(in); got != want {
			t.Errorf("%q → %q, want %q", in, got, want)
		}
	}
}

func TestRetriedEventsAreSeenOnce(t *testing.T) {
	r := testRelay(t, config{})
	v2 := map[string]any{"header": map[string]any{"event_id": "ev_1"}}
	v1 := map[string]any{"uuid": "u_1"}
	none := map[string]any{}
	if r.seenEvent(v2) || !r.seenEvent(v2) || r.seenEvent(v1) || !r.seenEvent(v1) {
		t.Fatal("event ids not de-duplicated")
	}
	if r.seenEvent(none) || r.seenEvent(none) {
		t.Fatal("an event without an id was dropped")
	}
}

func TestAdmissionIsTrustOnFirstUse(t *testing.T) {
	ctx := context.Background()
	r := testRelay(t, config{})
	token, other := strings.Repeat("a", 43), strings.Repeat("b", 43)
	steps := []struct {
		token, code, refused string
	}{
		{"short", "ABCDEFGHJKLM", "this Aria version is too old for the relay; upgrade aria-code"},
		{token, "ABC", "bind code missing; upgrade aria-code"},
		{token, "ABCDEFGHJKLM", ""},
		{token, "ABCDEFGHJKLM", ""}, // the same machine again
		{other, "ABCDEFGHJKLM", "client_id is registered to another installation"}, // someone else
		{token, "NPQRSTUVWXYZ", ""}, // a rotated bind code
	}
	for i, s := range steps {
		got, err := r.admit(ctx, "aria-a", s.token, s.code)
		if err != nil || got != s.refused {
			t.Fatalf("step %d: %q (%v), want %q", i, got, err, s.refused)
		}
	}
	if client, _ := r.clientForBindCode(ctx, "NPQR-STUV-WXYZ"); client != "aria-a" {
		t.Fatalf("rotated code binds %q", client)
	}
	if client, _ := r.clientForBindCode(ctx, "ABCDEFGHJKLM"); client != "" {
		t.Fatalf("old code still binds %q", client)
	}
}

func TestConcurrentForwardsAndAnswersAreSafe(t *testing.T) {
	ctx := context.Background()
	r := testRelay(t, config{})
	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(2)
		go func(i int) {
			defer wg.Done()
			r.rememberForward(ctx, "aria-a", "om_"+string(rune('a'+i%26)), "oc_1")
			r.maySend(ctx, "aria-a", send("reply", "om_a", "text"))
		}(i)
		go func(i int) {
			defer wg.Done()
			r.seenEvent(map[string]any{"uuid": string(rune('a' + i%26))})
			r.resolveResponse("aria-a", "req_missing", nil)
		}(i)
	}
	wg.Wait()
}

func TestClientsIdleForAMinuteAreForgotten(t *testing.T) {
	ctx := context.Background()
	r := testRelay(t, config{})
	r.rememberForward(ctx, "aria-a", "om_1", "")
	r.sendTimes["aria-gone"] = []time.Time{time.Now().Add(-2 * time.Minute)}
	r.sendTimes["aria-recent"] = []time.Time{time.Now().Add(-5 * time.Second)}
	if got, _ := r.maySend(ctx, "aria-a", send("reply", "om_1", "text")); got != "" {
		t.Fatalf("refused: %s", got)
	}
	if _, kept := r.sendTimes["aria-gone"]; kept || len(r.sendTimes) != 2 {
		t.Fatalf("send records: %v", r.sendTimes)
	}
}
