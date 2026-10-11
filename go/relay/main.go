// Command aria-relay is the Go implementation of Aria's Feishu relay
// (src/aria_code/aria_relay_server.py). Same endpoints, environment and
// store layout; tests/test_relay_contract.py runs against both:
//
//	ARIA_RELAY_COMMAND=go/relay/aria-relay pytest tests/test_relay_contract.py
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func env(name, fallback string) string {
	if value, ok := os.LookupEnv(name); ok {
		return value
	}
	return fallback
}

func loadConfig() config {
	timeout, err := strconv.Atoi(env("MESSAGE_TIMEOUT", "90"))
	if err != nil || timeout <= 0 { // the Python relay treats 0 as the default too
		timeout = 90
	}
	allow := strings.ToLower(strings.TrimSpace(os.Getenv("RELAY_ALLOW_UNVERIFIED_EVENTS")))
	return config{
		appID:             os.Getenv("FEISHU_APP_ID"),
		appSecret:         os.Getenv("FEISHU_APP_SECRET"),
		relaySecret:       os.Getenv("RELAY_SECRET"),
		verificationToken: os.Getenv("FEISHU_VERIFICATION_TOKEN"),
		encryptKey:        os.Getenv("FEISHU_ENCRYPT_KEY"),
		allowUnverified:   allow == "1" || allow == "true" || allow == "yes" || allow == "on",
		messageTimeout:    time.Duration(timeout) * time.Second,
		feishuBase:        strings.TrimRight(env("FEISHU_API_BASE", "https://open.feishu.cn/open-apis"), "/"),
	}
}

func openStore(ctx context.Context) (Store, error) {
	switch kind := strings.ToLower(strings.TrimSpace(env("RELAY_STORE", "sqlite"))); kind {
	case "sqlite":
		return openSQLite(env("DB_PATH", "./relay.db"))
	case "firestore":
		return openFirestore(ctx, os.Getenv("RELAY_FIRESTORE_PROJECT"), os.Getenv("RELAY_FIRESTORE_DATABASE"))
	default:
		return nil, fmt.Errorf("RELAY_STORE must be sqlite or firestore, not %q", kind)
	}
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--healthcheck" {
		if err := healthcheck("http://127.0.0.1:" + env("PORT", "8765") + "/status"); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	store, err := openStore(ctx)
	if err != nil {
		log.Fatalf("[relay] %v", err)
	}
	relay := newRelay(loadConfig(), store)
	relay.log.Printf("Aria Relay Server (Go) started  db=%s  store=%s", env("DB_PATH", "./relay.db"), store.Kind())
	if os.Getenv("K_SERVICE") != "" && store.Kind() == "sqlite" {
		relay.log.Printf("Running on Cloud Run with SQLite: bindings and machine credentials are " +
			"lost on every deploy. Set RELAY_STORE=firestore.")
	}

	server := &http.Server{Addr: ":" + env("PORT", "8765"), Handler: relay, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		server.Shutdown(shutdown)
	}()
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("[relay] %v", err)
	}
	relay.log.Printf("Relay Server shutting down")
}
