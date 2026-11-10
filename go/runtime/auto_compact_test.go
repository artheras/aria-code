package main

import (
	"context"
	"strings"
	"testing"

	"google.golang.org/genai"
)

func longHistory() []*genai.Content {
	blob := "KEEP-GOAL " + strings.Repeat("NOISE ", 4000)
	contents := []*genai.Content{
		genai.NewContentFromText(blob, genai.RoleUser),
		genai.NewContentFromText("noted the goal", genai.RoleModel),
	}
	for i := 0; i < 4; i++ {
		contents = append(contents,
			genai.NewContentFromText("short question", genai.RoleUser),
			genai.NewContentFromText("short answer", genai.RoleModel),
		)
	}
	contents = append(contents, genai.NewContentFromText("LATEST-QUESTION", genai.RoleUser))
	return contents
}

func TestAutoCompactShrinksAHotHistoryBeforeTheModelSeesIt(t *testing.T) {
	s := approvalServer(t, config{"context_window": 500.0}, appOptions{})
	s.contents = longHistory()
	s.done = make(chan struct{})
	var sent string
	s.provider = providerFunc(func(_ context.Context, history []*genai.Content, _ string, _ []*genai.Tool, _ func(string)) (*genai.Content, error) {
		for _, content := range history {
			sent += contentText(content)
		}
		return genai.NewContentFromText("continued", genai.RoleModel), nil
	})
	s.runTurn(context.Background(), "t", "continue please")
	if !strings.Contains(sent, "KEEP-GOAL") || !strings.Contains(sent, "LATEST-QUESTION") {
		t.Fatal("the summary or the recent turn was dropped", sent[:min(200, len(sent))])
	}
	if strings.Count(sent, "NOISE") > 80 {
		t.Fatal("the model still received the full earlier turn")
	}
	if s.snap.Metadata["auto_compact_count"] != 1 {
		t.Fatal(s.snap.Metadata["auto_compact_count"])
	}
}

func TestAutoCompactStaysOffWhenDisabledOrTheHistoryIsShort(t *testing.T) {
	disabled := approvalServer(t, config{"context_window": 500.0, "auto_compact_context": false}, appOptions{})
	disabled.contents = longHistory()
	if disabled.maybeAutoCompact("t", "continue") {
		t.Fatal("disabled compact ran")
	}
	short := approvalServer(t, config{"context_window": 50.0}, appOptions{})
	short.contents = []*genai.Content{genai.NewContentFromText(strings.Repeat("NOISE ", 4000), genai.RoleUser)}
	if short.maybeAutoCompact("t", "continue") {
		t.Fatal("a short history was compacted")
	}
}
