package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHealthcheckRequiresHealthyStatus(t *testing.T) {
	for _, test := range []struct {
		body    string
		code    int
		healthy bool
	}{
		{`{"total_bindings":0,"connected_clients":0,"store":"sqlite"}`, 200, true},
		{`{"status":"stopped"}`, 200, false},
		{`{"detail":"database unavailable"}`, 500, false},
		{`invalid JSON`, 200, false},
	} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/status" {
				t.Errorf("path %s", r.URL.Path)
			}
			w.WriteHeader(test.code)
			w.Write([]byte(test.body))
		}))
		err := healthcheck(server.URL + "/status")
		server.Close()
		if (err == nil) != test.healthy {
			t.Errorf("%s: %v", test.body, err)
		}
	}
}
