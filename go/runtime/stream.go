package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
)

func hostBarrier(ctx context.Context, in io.Reader) error {
	wake := context.AfterFunc(ctx, func() {
		if closer, ok := in.(io.Closer); ok {
			closer.Close()
		}
	})
	defer wake()
	var byte [1]byte
	_, err := io.ReadFull(in, byte[:])
	if err != nil {
		return err
	}
	// A host writing a text-mode pipe on Windows acknowledges with "\r\n".
	if byte[0] == '\r' {
		if _, err := io.ReadFull(in, byte[:]); err != nil {
			return err
		}
	}
	if byte[0] != '\n' {
		return errors.New("native start requires host containment acknowledgement")
	}
	return ctx.Err()
}

// Translate the persistent application protocol into the existing single-turn
// renderer contract. This adapter holds no approval authority and emits no
// session snapshots after the final completion record.
type streamEvents struct {
	out                   io.Writer
	prompt, model, answer string
	completed, success    bool
}

func (w *streamEvents) Write(body []byte) (int, error) {
	var event map[string]any
	if err := json.Unmarshal(body, &event); err != nil {
		return 0, err
	}
	kind, _ := event["type"].(string)
	switch kind {
	case "turn.started":
		event["prompt"], event["model"] = w.prompt, w.model
	case "answer.delta", "tool.started", "tool.completed", "turn.status":
	case "answer.replace":
		w.answer, _ = event["text"].(string)
		return len(body), nil
	case "turn.completed":
		// A turn whose checks are red did not succeed, however it ended.
		verified := asMap(event["acceptance"])["verified"]
		event["response"], event["success"] = w.answer, event["status"] == "ok" && verified != false
	case "session.state":
		return len(body), nil
	default:
		return 0, errors.New("unexpected event in headless execution")
	}
	encoded, err := json.Marshal(event)
	if err != nil {
		return 0, err
	}
	if len(encoded)+1 > maxMessage {
		return 0, errors.New("stream event exceeds 1 MiB")
	}
	encoded = append(encoded, '\n')
	n, err := w.out.Write(encoded)
	if err == nil && n != len(encoded) {
		err = io.ErrShortWrite
	}
	if err != nil {
		return 0, err
	}
	if kind == "turn.completed" {
		w.completed, w.success = true, event["success"] == true
	}
	return len(body), nil
}

func (s *server) runStream(ctx context.Context, prompt string) error {
	if strings.TrimSpace(prompt) == "" || len(prompt) > 65536 {
		return errors.New("prompt must contain between 1 and 65536 bytes")
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	w := &streamEvents{out: s.out, prompt: prompt, model: "google/" + strings.TrimPrefix(s.cfg.text("model", "gemini-3.5-flash"), "google/")}
	s.out = w
	s.stop = cancel
	s.opts.noninteractive = true
	s.busy, s.turn, s.done = true, "headless", make(chan struct{})
	s.runTurn(ctx, "headless", prompt)
	if !w.completed {
		return errors.New("runtime did not complete the headless turn")
	}
	if !w.success {
		return errors.New("headless turn failed")
	}
	return nil
}
