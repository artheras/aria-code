package main

// Conversation commands ported from the Python worker (session_ux_cmds.py):
// /compact, /btw, /recap, /history, /rename, /fork, /regen and the /save and
// /load aliases.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"google.golang.org/genai"
)

const sideSystem = "You are Aria, Arthera's assistant. Answer in the user's language. Answer from the conversation only; do not use tools."

// askAside asks the model about the conversation without adding anything to
// it: the question and the answer are not part of the next turn's history.
func (s *server) askAside(ctx context.Context, question string) (string, error) {
	history := append(settlePending(append([]*genai.Content(nil), s.contents...)), genai.NewContentFromText(question, genai.RoleUser))
	reply, err := s.provider.Generate(ctx, history, sideSystem, nil, func(string) {})
	if err != nil {
		return "", err
	}
	text := strings.TrimSpace(contentText(reply))
	if text == "" {
		return "", errors.New("the model returned no text")
	}
	return text, nil
}

func contentText(content *genai.Content) string {
	if content == nil {
		return ""
	}
	var b strings.Builder
	for _, part := range content.Parts {
		if part != nil && !part.Thought {
			b.WriteString(part.Text)
		}
	}
	return b.String()
}

func (s *server) btwCommand(ctx context.Context, question string) (string, error) {
	if question == "" {
		return "", errors.New("usage: /btw <question> — a quick question that stays out of the conversation")
	}
	if len(s.contents) == 0 {
		return "", errors.New("no conversation yet: ask normally instead")
	}
	return s.askAside(ctx, "[Side question: answer briefly; it will not be kept in the conversation]\n"+question)
}

func (s *server) recapCommand(ctx context.Context) (string, error) {
	if len(s.contents) == 0 {
		return "Nothing to recap yet.", nil
	}
	return s.askAside(ctx, "Recap this conversation in at most five short bullet points: what was asked, what was done (files changed, checks run) and what is still open. Do not invent anything that is not in the conversation.")
}

// compactCommand replaces the model's history with a summary of it. The
// transcript the user sees is kept; only what is sent to the model shrinks.
func (s *server) compactCommand(ctx context.Context, focus string) (string, error) {
	if len(s.contents) < 4 {
		return "Context is small enough; nothing to compact.", nil
	}
	before := estimateTokens(s.contents)
	instruction := "Summarise this conversation so that it can continue from the summary alone. Keep: the user's goals and constraints, decisions made, files read or changed (with paths), commands run and their results, and open questions. Be concise and factual."
	if focus != "" {
		instruction += " Pay particular attention to: " + focus
	}
	summary, err := s.askAside(ctx, instruction)
	if err != nil {
		return "", fmt.Errorf("compact failed, conversation unchanged: %w", err)
	}
	s.contents = []*genai.Content{
		genai.NewContentFromText("Summary of the conversation so far:\n\n"+summary, genai.RoleUser),
		genai.NewContentFromText("Understood. I will continue from this summary.", genai.RoleModel),
	}
	if s.snap.Metadata == nil {
		s.snap.Metadata = map[string]any{}
	}
	s.snap.Metadata["compacted_at"] = time.Now().UTC().Format(time.RFC3339)
	return fmt.Sprintf("Compacted: ~%d → ~%d tokens. The transcript above is kept; the model continues from a summary.", before, estimateTokens(s.contents)), nil
}

func (s *server) historyCommand(args []string) (string, error) {
	limit := 20
	if len(args) == 1 {
		if _, err := fmt.Sscan(args[0], &limit); err != nil || limit < 1 {
			return "", errors.New("usage: /history [count]")
		}
	} else if len(args) > 1 {
		return "", errors.New("usage: /history [count]")
	}
	prompts := []string{}
	for _, m := range s.snap.Messages {
		if m["role"] == "user" {
			text, _ := m["content"].(string)
			prompts = append(prompts, text)
		}
	}
	if len(prompts) == 0 {
		return "No prompts yet.", nil
	}
	start := 0
	if len(prompts) > limit {
		start = len(prompts) - limit
	}
	var b strings.Builder
	b.WriteString("History")
	for i := start; i < len(prompts); i++ {
		line := strings.Join(strings.Fields(prompts[i]), " ")
		if r := []rune(line); len(r) > 100 {
			line = string(r[:99]) + "…"
		}
		fmt.Fprintf(&b, "\n  %3d  %s", i+1, line)
	}
	return b.String(), nil
}

func (s *server) renameCommand(title string) (string, error) {
	title = strings.Join(strings.Fields(title), " ")
	if title == "" {
		return "", errors.New("usage: /rename <title>")
	}
	if r := []rune(title); len(r) > 80 {
		title = string(r[:80])
	}
	if s.snap.Metadata == nil {
		s.snap.Metadata = map[string]any{}
	}
	s.snap.Metadata["title"] = title
	return "Session renamed: " + title, nil
}

// forkCommand saves a copy of this session under a new ID. This session stays
// open; the copy is resumed like any other saved session.
func (s *server) forkCommand(title string) (string, error) {
	id, err := newID()
	if err != nil {
		return "", err
	}
	contents, err := json.Marshal(s.contents)
	if err != nil {
		return "", err
	}
	metadata := map[string]any{"forked_from": s.snap.ID}
	for key, value := range s.snap.Metadata {
		if key != "created_at" && key != "title" && key != "forked_from" {
			metadata[key] = value
		}
	}
	title = strings.Join(strings.Fields(title), " ")
	if title == "" {
		base, _ := s.snap.Metadata["title"].(string)
		if base == "" {
			base = "session"
		}
		title = base + " (fork)"
	}
	if r := []rune(title); len(r) > 80 {
		title = string(r[:80])
	}
	metadata["title"] = title
	messages := make([]message, len(s.snap.Messages))
	copy(messages, s.snap.Messages)
	fork := snapshot{ID: id, Messages: messages, Metadata: metadata, ProviderContents: contents}
	if err := saveSession(s.opts.root, fork); err != nil {
		return "", err
	}
	return fmt.Sprintf("Forked to %s (%q). This session stays open; /resume opens the fork.", id, title), nil
}

// prepareRegen removes the last exchange and returns its prompt, which the
// turn then sends again.
func (s *server) prepareRegen() (string, error) {
	prompt := ""
	for i := len(s.snap.Messages) - 1; i >= 0; i-- {
		if s.snap.Messages[i]["role"] == "user" {
			prompt, _ = s.snap.Messages[i]["content"].(string)
			break
		}
	}
	if strings.TrimSpace(prompt) == "" {
		return "", errors.New("nothing to regenerate")
	}
	if _, err := s.undoConversation(); err != nil {
		return "", err
	}
	return prompt, nil
}
