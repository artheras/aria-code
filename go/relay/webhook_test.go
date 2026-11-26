package main

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func signPayload(secret string, body []byte) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}

func TestWebhookBotSignatureAndApprovalFlow(t *testing.T) {
	store, err := openSQLite(":memory:")
	if err != nil {
		t.Fatalf("openSQLite: %v", err)
	}

	r := newRelay(config{webhookSecret: "whsec-test"}, store)

	// Missing signature must be rejected (401).
	req := httptest.NewRequest(http.MethodPost, "/webhook/bot", bytes.NewBufferString(`{"channel":"grok","actor":"u1","message":"ping"}`))
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 without signature, got %d", w.Code)
	}

	// Signed approval decision via /webhook/bot updates approval state.
	body := []byte(`{"channel":"telegram","actor":"ops-admin","approval_id":"apr-42","approved":true,"reason":"verified"}`)
	req = httptest.NewRequest(http.MethodPost, "/webhook/bot", bytes.NewReader(body))
	req.Header.Set("X-Aria-Signature", signPayload("whsec-test", body))
	w = httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}

	// Query /approvals/status?id=apr-42
	req = httptest.NewRequest(http.MethodGet, "/approvals/status?id=apr-42", nil)
	w = httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200 on status, got %d", w.Code)
	}
	var resp struct {
		OK       bool           `json:"ok"`
		Approval approvalRecord `json:"approval"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if !resp.OK || resp.Approval.Status != "approved" || resp.Approval.Actor != "ops-admin" {
		t.Fatalf("unexpected approval record: %+v", resp)
	}
}
