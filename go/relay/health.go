package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Probe the real storage-backed status endpoint. Works in distroless without
// curl or a shell, and bounds startup checks even if Firestore is unavailable.
func healthcheck(url string) error {
	client := &http.Client{Timeout: 3 * time.Second}
	response, err := client.Get(url)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("relay status: HTTP %d", response.StatusCode)
	}
	var status map[string]any
	if err := json.NewDecoder(io.LimitReader(response.Body, 65536)).Decode(&status); err != nil {
		return err
	}
	count, countOK := status["total_bindings"].(float64)
	clients, clientsOK := status["connected_clients"].(float64)
	store, storeOK := status["store"].(string)
	if !countOK || count < 0 || !clientsOK || clients < 0 || !storeOK || (store != "sqlite" && store != "firestore") {
		return fmt.Errorf("invalid relay status")
	}
	return nil
}
