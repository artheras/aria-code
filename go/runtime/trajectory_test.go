package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestMaskTextMatchesTheEventMasking(t *testing.T) {
	for input, want := range map[string]string{
		"export API_KEY=sk-live-1234567890abc":      "export API_KEY=***",
		"password: hunter2 and more":                "password: *** and more",
		"Authorization: Bearer abc.def-ghi":         "Authorization: Bearer ***",
		"key sk-abcdefgh12 in text":                 "key *** in text",
		"ghp_abcdefghijklmnop pushed":               "*** pushed",
		"AIzaSyA1234567890abcdefghijk":              "***",
		"ya29.a0AfH6SMB-token":                      "***",
		"keep\n  indentation\n\tand token = x1\n":   "keep\n  indentation\n\tand token = ***\n",
		"nothing secret here; tokenizer=fine words": "nothing secret here; tokenizer=fine words",
	} {
		if got := maskText(input); got != want {
			t.Errorf("%q: %q, want %q", input, got, want)
		}
	}
	masked := maskValue(map[string]any{"content": "token=abc", "_workspace": "/private", "items": []any{"password=x", 3.0}})
	body, _ := json.Marshal(masked)
	if string(body) != `{"content":"token=***","items":["password=***",3]}` {
		t.Fatal(string(body))
	}
}

func TestFullResultsAreCut(t *testing.T) {
	result := toolResult{Success: true, Data: map[string]any{"content": strings.Repeat("界", 30_000)}}
	cut, ok := fullResult(result).(string)
	if !ok || !strings.HasSuffix(cut, "…") || len([]rune(cut)) != fullResultChars+1 {
		t.Fatal("results over 20,000 characters are cut, with a marker")
	}
	if small, ok := fullResult(toolResult{Success: true}).(map[string]any); !ok || small["success"] != true {
		t.Fatal(small)
	}
}

func TestHeadlessSuccessNeedsGreenChecks(t *testing.T) {
	for _, tc := range []struct {
		acceptance any
		want       bool
	}{
		{nil, true},
		{map[string]any{"verified": true}, true},
		{map[string]any{"verified": nil}, true},
		{map[string]any{"verified": false}, false},
	} {
		var out bytes.Buffer
		w := &streamEvents{out: &out}
		event, _ := json.Marshal(map[string]any{"type": "turn.completed", "status": "ok", "acceptance": tc.acceptance})
		if _, err := w.Write(event); err != nil {
			t.Fatal(err)
		}
		if w.success != tc.want {
			t.Errorf("%v: success %v", tc.acceptance, w.success)
		}
	}
}
