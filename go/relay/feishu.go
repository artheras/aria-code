package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sync"
	"time"
)

// feishu calls the Feishu Open API with the app's tenant token.
type feishu struct {
	base, appID, appSecret string
	http                   *http.Client

	mu      sync.Mutex
	token   string
	expires time.Time
}

func newFeishu(base, appID, appSecret string) *feishu {
	return &feishu{base: base, appID: appID, appSecret: appSecret, http: &http.Client{}}
}

// tenantToken is cached until a minute before it expires. A failed fetch is
// an error and is not cached: an empty token kept for two hours made every
// send fail until it expired.
func (f *feishu) tenantToken(ctx context.Context) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.token != "" && time.Until(f.expires) > time.Minute {
		return f.token, nil
	}
	var data struct {
		Code   *int   `json:"code"`
		Token  string `json:"tenant_access_token"`
		Expire *int   `json:"expire"`
	}
	status, err := f.call(ctx, "/auth/v3/tenant_access_token/internal", "",
		map[string]string{"app_id": f.appID, "app_secret": f.appSecret}, 10*time.Second, &data)
	if err != nil {
		return "", err
	}
	if status != http.StatusOK || (data.Code != nil && *data.Code != 0) || data.Token == "" {
		code := "?"
		if data.Code != nil {
			code = fmt.Sprint(*data.Code)
		}
		return "", fmt.Errorf("feishu tenant token: HTTP %d, code %s", status, code)
	}
	expire := 7200
	if data.Expire != nil {
		expire = *data.Expire
	}
	f.token, f.expires = data.Token, time.Now().Add(time.Duration(expire)*time.Second)
	return f.token, nil
}

// call POSTs JSON and decodes the JSON answer into out (when not nil).
func (f *feishu) call(ctx context.Context, path, token string, body any, timeout time.Duration, out any) (int, error) {
	payload, err := marshal(body)
	if err != nil {
		return 0, err
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, f.base+path, bytes.NewReader(payload))
	if err != nil {
		return 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := f.http.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if out != nil {
		if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
			return resp.StatusCode, err
		}
	}
	return resp.StatusCode, nil
}

// replyCard answers a message with a one-block card (the offline notice).
func (f *feishu) replyCard(ctx context.Context, messageID, content, color string) error {
	token, err := f.tenantToken(ctx)
	if err != nil {
		return err
	}
	card := map[string]any{
		"msg_type": "interactive",
		"card": map[string]any{
			"header": map[string]any{
				"title":    map[string]any{"tag": "plain_text", "content": "Aria"},
				"template": color,
			},
			"elements": []any{map[string]any{"tag": "div",
				"text": map[string]any{"tag": "lark_md", "content": content}}},
		},
	}
	_, err = f.call(ctx, "/im/v1/messages/"+messageID+"/reply", token, card, 15*time.Second, nil)
	return err
}

// sendText sends a text message to a Feishu user.
func (f *feishu) sendText(ctx context.Context, openID, text string) error {
	token, err := f.tenantToken(ctx)
	if err != nil {
		return err
	}
	content, err := marshal(map[string]string{"text": text})
	if err != nil {
		return err
	}
	body := map[string]any{"receive_id": openID, "msg_type": "text", "content": string(content)}
	_, err = f.call(ctx, "/im/v1/messages?"+url.Values{"receive_id_type": {"open_id"}}.Encode(),
		token, body, 15*time.Second, nil)
	return err
}

// marshal is json.Marshal without HTML escaping: payloads pass through as written.
func marshal(value any) ([]byte, error) {
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}
