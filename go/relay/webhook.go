package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"
)

type approvalRecord struct {
	ID        string    `json:"id"`
	Status    string    `json:"status"` // "pending" | "approved" | "denied"
	Actor     string    `json:"actor,omitempty"`
	Channel   string    `json:"channel,omitempty"`
	Reason    string    `json:"reason,omitempty"`
	UpdatedAt time.Time `json:"updated_at"`
}

type webhookBotPayload struct {
	Channel    string `json:"channel"`
	Actor      string `json:"actor"`
	Message    string `json:"message"`
	ClientID   string `json:"client_id"`
	ApprovalID string `json:"approval_id"`
	Approved   *bool  `json:"approved"`
	Reason     string `json:"reason"`
}

func verifyWebhookSignature(secret, sigHeader string, body []byte) bool {
	secret = strings.TrimSpace(secret)
	if secret == "" {
		return false
	}
	sig := strings.TrimSpace(sigHeader)
	sig = strings.TrimPrefix(sig, "sha256=")
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	expected := hex.EncodeToString(mac.Sum(nil))
	return subtle.ConstantTimeCompare([]byte(strings.ToLower(sig)), []byte(expected)) == 1
}

func (r *relay) verifyBotWebhookRequest(w http.ResponseWriter, req *http.Request) ([]byte, bool) {
	body, err := io.ReadAll(io.LimitReader(req.Body, maxEventBody))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "Invalid request body"})
		return nil, false
	}
	if strings.TrimSpace(r.cfg.webhookSecret) == "" {
		if !r.cfg.allowUnverified {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "Webhook secret not configured"})
			return nil, false
		}
		return body, true
	}
	sig := req.Header.Get("X-Aria-Signature")
	if !verifyWebhookSignature(r.cfg.webhookSecret, sig, body) {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "Invalid webhook signature"})
		return nil, false
	}
	return body, true
}

func (r *relay) recordApproval(id, status, actor, channel, reason string) approvalRecord {
	rec := approvalRecord{
		ID:        id,
		Status:    status,
		Actor:     actor,
		Channel:   channel,
		Reason:    reason,
		UpdatedAt: time.Now().UTC(),
	}
	r.mu.Lock()
	if r.approvals == nil {
		r.approvals = map[string]approvalRecord{}
	}
	r.approvals[id] = rec
	r.mu.Unlock()
	return rec
}

func (r *relay) webhookBot(w http.ResponseWriter, req *http.Request) {
	body, ok := r.verifyBotWebhookRequest(w, req)
	if !ok {
		return
	}
	var payload webhookBotPayload
	if err := json.Unmarshal(body, &payload); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "Invalid JSON payload"})
		return
	}
	if strings.TrimSpace(payload.ApprovalID) != "" && payload.Approved != nil {
		status := "denied"
		if *payload.Approved {
			status = "approved"
		}
		rec := r.recordApproval(
			strings.TrimSpace(payload.ApprovalID),
			status,
			strings.TrimSpace(payload.Actor),
			strings.TrimSpace(payload.Channel),
			strings.TrimSpace(payload.Reason),
		)
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "approval": rec})
		return
	}
	if strings.TrimSpace(payload.Message) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "Missing message"})
		return
	}
	r.mu.Lock()
	_, connected := r.connections[payload.ClientID]
	r.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":        true,
		"channel":   payload.Channel,
		"actor":     payload.Actor,
		"client_id": payload.ClientID,
		"connected": connected,
	})
}

func (r *relay) approvalDecision(w http.ResponseWriter, req *http.Request) {
	body, ok := r.verifyBotWebhookRequest(w, req)
	if !ok {
		return
	}
	var payload struct {
		ID      string `json:"id"`
		Status  string `json:"status"`
		Actor   string `json:"actor"`
		Channel string `json:"channel"`
		Reason  string `json:"reason"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "Invalid JSON payload"})
		return
	}
	id := strings.TrimSpace(payload.ID)
	if id == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "Missing approval id"})
		return
	}
	status := strings.ToLower(strings.TrimSpace(payload.Status))
	if status != "pending" && status != "approved" && status != "denied" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "Invalid status"})
		return
	}
	rec := r.recordApproval(id, status, payload.Actor, payload.Channel, payload.Reason)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "approval": rec})
}

func (r *relay) approvalStatus(w http.ResponseWriter, req *http.Request) {
	id := strings.TrimSpace(req.URL.Query().Get("id"))
	if id == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "Missing id query param"})
		return
	}
	r.mu.Lock()
	rec, ok := r.approvals[id]
	r.mu.Unlock()
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"detail": "Approval not found"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "approval": rec})
}
